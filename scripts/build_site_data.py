"""
Bygger alle datafiler til Fantasy Premier League HQ (den rigtige side, ikke mockup'et):

  site-data.json       - stilling, rank-historik, GW-resuméer, sæson-highlights, alerts, bænk-tendens
  powerranking.json    - top 25 spillere, formel-baseret score + AI-argumenter
  management.json      - din aktuelle startopstilling, bænk, kampprogram, ombytningsforslag

  (draft-rankings.json genereres IKKE længere - Draft-fanen er midlertidigt deaktiveret
   i UI'en indtil næste redraft, feb 2027. Se main() for hvor blokken er kommenteret ud.)

Køres af .github/workflows/site-data.yml. Data gemmes og genindlæses mellem kørsler
(rank-history.json), så vi kan bygge historik op over tid uden at have en database.

Bruger MIN_ENTRY_ID til at identificere DIG specifikt (Management-fanen er personlig,
ikke delt mellem alle i ligaen).
"""
import json
import os
import sys
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from fpl_common import (
    FPL_BASE, DRAFT_BASE, LEAGUE_ID, TEAM_NAMES,
    fetch_json, load_json_file, save_json_file,
    get_player_full_name, get_player_positions, get_player_names, get_player_clubs,
    get_league_entries, find_latest_finished_event, find_next_event,
    get_team_fixture_difficulty, get_live_points_map, get_corrected_element_status,
    VAN_OEVELEN_ID, TZOLIS_ID,
)

MY_ENTRY_ID = 1510  # Rasmus / "HaCunha Mateta" - Management-fanen er bygget til dig specifikt

RANK_HISTORY_FILE = "rank-history.json"


# ---------------------------------------------------------------------------
# Stilling, rank-historik, alerts, bænk-tendens
# ---------------------------------------------------------------------------

def build_standings_block(league_details, entry_name_map):
    standings = sorted(league_details["standings"], key=lambda s: -(s["total"] or 0))
    rows = []
    current_ranks = {}
    for i, s in enumerate(standings, start=1):
        eid = s["league_entry"]
        current_ranks[str(eid)] = i
        rows.append({
            "league_entry": eid,
            "name": entry_name_map.get(eid, f"Entry {eid}"),
            "total": s["total"] or 0,
            "rank": i,
        })
    point_gap = (rows[0]["total"] - rows[-1]["total"]) if len(rows) >= 2 else 0
    return rows, current_ranks, point_gap


def update_rank_history(rank_history, gw, current_ranks, standings_rows):
    """Gemmer stilling for denne GW i en løbende log, så vi kan tegne kurven + regne trends."""
    gw_key = f"GW{gw}"
    entry_totals = {str(r["league_entry"]): r["total"] for r in standings_rows}
    rank_history[gw_key] = {"ranks": current_ranks, "totals": entry_totals}
    return rank_history


def compute_trend(rank_history, current_ranks, this_gw):
    """Sammenligner mod forrige loggede GW. Returnerer {league_entry: delta} (+ = rykket op)."""
    prev_gw_key = f"GW{this_gw - 1}"
    prev = rank_history.get(prev_gw_key, {}).get("ranks", {})
    trend = {}
    for eid, rank in current_ranks.items():
        if eid in prev:
            trend[eid] = prev[eid] - rank  # positivt = rykket op
    return trend


def build_alerts(bootstrap, element_status, entry_name_map):
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    owner_by_element = {es["element"]: es["owner"] for es in element_status if es.get("owner")}
    alerts = []
    for element_id, owner_entry_id in owner_by_element.items():
        p = by_id.get(element_id)
        if not p:
            continue
        # Kun akutte ting: helt ude, eller under 50% spilchance
        chance = p.get("chance_of_playing_next_round")
        is_acute = p["status"] in ("i", "s", "u") or (chance is not None and chance < 50)
        if not is_acute or not p.get("news"):
            continue
        alerts.append({
            "player": get_player_full_name(p),
            "club": TEAM_NAMES.get(p["team"], "?"),
            "owner_entry_id": owner_entry_id,
            "owner": entry_name_map.get(owner_entry_id, "?"),
            "news": p["news"],
            "chance": chance,
        })
    return alerts


TEAM_SNAPSHOT_FILE = "team-snapshot.json"


BIG_TRANSFER_THRESHOLD = 100  # sidste sæsons point - grænsen for at tælle som "stor nok" uden at være ejet


def build_league_transfer_news(bootstrap, element_status, entry_name_map, kicked_off):
    """
    Sporer ALLE Premier League-spillere (ikke kun ejede) for tre ting, kun ud
    fra FPL's egen bekræftede data - ikke AI, ikke søgning:
      1) Helt nye spillere i ligaen (dukker op i bootstrap-static for første gang)
      2) Klubskifte for eksisterende spillere - nævnes KUN hvis enten ejet i
         vores liga, eller en 'stor' spiller (>= BIG_TRANSFER_THRESHOLD point
         sidste sæson) - undgår at oversvømme listen med hver eneste ubetydelige
         reserve-spillers klubskifte.
      3) Spillere der FORLADER ligaen - enten FPL's eget 'removed'-flag slår til,
         eller spilleren helt forsvinder fra bootstrap-static. Altid nævnt hvis
         ejet af os (akut relevant - vedkommende er dødvægt i din trup), ellers
         samme 'stor spiller'-tærskel som klubskifter.
    """
    owner_by_element = {es["element"]: es["owner"] for es in element_status if es.get("owner")}

    snapshot = load_json_file(TEAM_SNAPSHOT_FILE, {})
    is_first_ever_run = not snapshot  # tomt snapshot = intet at sammenligne mod, undgå at kalde alle 590 "nye"
    new_snapshot = {}
    news_items = []
    seen_ids = set()

    for p in bootstrap["elements"]:
        pid = p["id"]
        seen_ids.add(pid)
        current_team = p["team"]
        is_removed_now = bool(p.get("removed"))
        new_snapshot[str(pid)] = {"team": current_team, "removed": is_removed_now, "name": get_player_full_name(p)}
        prev = snapshot.get(str(pid))

        if prev is None:
            if not is_first_ever_run and not is_removed_now:
                news_items.append({
                    "type": "new_to_league",
                    "player": get_player_full_name(p),
                    "club": TEAM_NAMES.get(current_team, "?"),
                })
            continue

        previous_team = prev.get("team") if isinstance(prev, dict) else prev  # bagudkompatibel med gammelt format
        was_removed = prev.get("removed", False) if isinstance(prev, dict) else False
        owner_entry_id = owner_by_element.get(pid)
        is_owned = owner_entry_id is not None

        if is_removed_now and not was_removed:
            if not _is_notable(p, kicked_off, is_owned):
                continue
            news_items.append({
                "type": "left_league",
                "player": get_player_full_name(p),
                "owner": entry_name_map.get(owner_entry_id) if is_owned else None,
                "old_club": TEAM_NAMES.get(previous_team, "?"),
            })
        elif previous_team != current_team and not is_removed_now:
            if not _is_notable(p, kicked_off, is_owned):
                continue
            news_items.append({
                "type": "transfer",
                "player": get_player_full_name(p),
                "owner": entry_name_map.get(owner_entry_id) if is_owned else None,
                "old_club": TEAM_NAMES.get(previous_team, "?"),
                "new_club": TEAM_NAMES.get(current_team, "?"),
            })

    # Spillere der er helt forsvundet fra bootstrap-static (ikke bare markeret removed)
    for pid_str, prev in snapshot.items():
        pid = int(pid_str)
        if pid in seen_ids:
            continue
        if isinstance(prev, dict) and prev.get("removed"):
            continue  # allerede rapporteret dengang han blev markeret removed
        owner_entry_id = owner_by_element.get(pid)
        is_owned = owner_entry_id is not None
        news_items.append({
            "type": "left_league",
            "player": prev.get("name", "Ukendt spiller") if isinstance(prev, dict) else "Ukendt spiller",
            "owner": entry_name_map.get(owner_entry_id) if is_owned else None,
            "old_club": TEAM_NAMES.get(prev.get("team"), "?") if isinstance(prev, dict) else "?",
        })

    save_json_file(TEAM_SNAPSHOT_FILE, new_snapshot)
    return news_items


def _is_notable(p, kicked_off, is_owned):
    """Ejet = altid relevant. Ikke ejet = kun hvis en 'stor' spiller (sidste sæsons point)."""
    if is_owned:
        return True
    if kicked_off:
        stats = fetch_last_season_stats(p["id"])
        last_points = stats["total_points"] if stats else 0
    else:
        last_points = p.get("total_points", 0)
    return last_points >= BIG_TRANSFER_THRESHOLD

    save_json_file(TEAM_SNAPSHOT_FILE, new_snapshot)
    return news_items


PICKS_HISTORY_FILE = "picks-history.json"


def get_frozen_squad(entry_id, gw, picks_history):
    """
    FPL Draft's /entry/{id}/event/{gw} viste sig IKKE at være et pålideligt
    historisk øjebliksbillede - det kan reflektere en senere ændret trup, hvis
    nogen bytter spillere på en måde der ikke går gennem den sporede
    waiver/trade-log (bekræftet med et konkret, uforklaret tilfælde for GW1).
    Løsningen: første gang vi ser en gameweek, fryser vi picks-dataen permanent
    i picks-history.json (delt med league_update.py). Alle senere kald genbruger
    den frosne kopi i stedet for at spørge FPL igen, så data aldrig kan
    "drifte" efter at være gemt.

    BEKRÆFTET (15. sep 2026): Tzolis' rigtige trup-placeringer bliver fejlagtigt
    logget under Van Oevelens ID (554) i FPL's rå picks-data, samme mønster som
    i element-status. Substituerer derfor 554->557 FØR fastfrysning.
    """
    gw_key = f"GW{gw}"
    entry_key = str(entry_id)
    if gw_key in picks_history and entry_key in picks_history[gw_key]:
        return picks_history[gw_key][entry_key]

    picks = get_entry_gw_picks(entry_id, gw)
    if picks is None:
        return None
    for p in picks:
        if p.get("element") == VAN_OEVELEN_ID:
            p["element"] = TZOLIS_ID
    picks_history.setdefault(gw_key, {})[entry_key] = picks
    return picks


def build_bench_trend(bootstrap, entry_ids, live_points_by_gw, picks_history):
    """Kumuleret sæson-bænkpoint pr. manager, på tværs af alle spillede gameweeks."""
    totals = {str(eid): 0 for eid in entry_ids}
    for gw, live_points in live_points_by_gw.items():
        for eid in entry_ids:
            picks = get_frozen_squad(eid, gw, picks_history)
            if not picks:
                continue
            bench = [p for p in picks if p.get("position", 0) > 11]
            totals[str(eid)] += sum(live_points.get(p["element"], 0) for p in bench)
    return totals


def get_entry_gw_picks(entry_id, event_id):
    url = f"{DRAFT_BASE}/entry/{entry_id}/event/{event_id}"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError:
        return None
    if not isinstance(data, dict) or "picks" not in data:
        return None
    return data["picks"]


# ---------------------------------------------------------------------------
# Gameweek-resumé (AI, seriøs tone, ~150 ord)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Powerranking / Draft-rankings (formel + AI-argumenter)
# ---------------------------------------------------------------------------

def find_current_playing_gw(bootstrap):
    """
    Den gameweek der reelt spilles lige nu (eller senest er startet) - den højeste
    event-id hvis deadline er passeret. Bruges til at hente MANAGEMENT-picks, som
    findes så snart deadline passerer, uanset om FPL selv har markeret gameweeken
    som 'finished' (det sker først når alle kampe + bonuspoint er bekræftet).
    """
    now = datetime.now(timezone.utc)
    passed = []
    for e in bootstrap["events"]:
        deadline = datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))
        if deadline <= now:
            passed.append(e)
    if not passed:
        return None
    return max(passed, key=lambda e: e["id"])["id"]


def has_season_kicked_off(bootstrap):
    """
    Adskilt fra season_started (som kræver finished+data_checked og bruges til
    historik-logning). Denne tjekker kun om GW1's deadline er passeret - dvs. om
    kampe reelt bliver spillet og friske form/minutter-tal findes, UANSET om FPL
    selv har markeret gameweeken som 'finished' endnu (det tager ofte dage efter
    deadline, mens bonuspoint bekræftes). Uden dette skel ville powerranking-
    formlen fejlagtigt prøve at bruge sidste sæsons 900-minutters-krav på et
    datasæt hvor minutter allerede er nulstillet til den nye sæsons friske (lave) tal.
    """
    now = datetime.now(timezone.utc)
    gw1 = next((e for e in bootstrap["events"] if e["id"] == 1), None)
    if not gw1:
        return False
    deadline = datetime.fromisoformat(gw1["deadline_time"].replace("Z", "+00:00"))
    return now > deadline


def fetch_last_season_stats(player_id):
    """
    Bootstrap-static's points_per_game OG total_points nulstiller begge til DENNE
    sæson med det samme sæsonen starter (bekræftet: efter 1 spillet GW viste
    points_per_game samme tal som 'form', og total_points var kun ét GW's spæde
    sum - ikke sidste sæsons rigtige total som antaget). Ægte sidste-sæson-data
    findes kun via dette per-spiller endpoint - for dyrt at kalde for alle ~590
    spillere, så det bruges kun til en lille kandidat-pulje i build_ranked_list.
    Returnerer {"ppg": float, "total_points": int} eller None hvis util.
    """
    try:
        data = fetch_json(f"{FPL_BASE}/element-summary/{player_id}/")
        history_past = data.get("history_past", [])
        if not history_past:
            return None
        last = history_past[-1]
        minutes = last.get("minutes", 0)
        total_points = last.get("total_points", 0)
        if minutes < 900:
            return None
        games = minutes / 90.0
        ppg = total_points / games if games > 0 else 0.0
        return {"ppg": ppg, "total_points": total_points}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Management (kun for MY_ENTRY_ID): startopstilling, bænk, ombytninger
# ---------------------------------------------------------------------------

def build_management(bootstrap, current_gw, fixture_by_team, element_status=None):
    positions = get_player_positions(bootstrap)
    names = get_player_names(bootstrap)
    clubs = get_player_clubs(bootstrap)
    by_id = {p["id"]: p for p in bootstrap["elements"]}

    picks = get_entry_gw_picks(MY_ENTRY_ID, current_gw) if current_gw else None

    if not picks:
        # Ingen picks sat for kommende gameweek endnu (sker typisk før første deadline er
        # passeret). Vi kan stadig vise DIN TRUP via draft-ejerskabsdata - det kræver ikke
        # at du har sat en specifik startopstilling, kun at draften er gennemført.
        if element_status is None:
            return {"available": False, "reason": "Ingen picks-data for denne gameweek endnu."}
        my_squad_ids = [es["element"] for es in element_status if es.get("owner") == MY_ENTRY_ID]
        if not my_squad_ids:
            return {"available": False, "reason": "Ingen picks-data for denne gameweek endnu."}
        squad = []
        squad_team_ids = set()
        injury_news = []
        for pid in my_squad_ids:
            p = by_id.get(pid)
            if not p:
                continue
            squad_team_ids.add(p["team"])
            if p["status"] != "a" and p.get("news"):
                injury_news.append({
                    "player": names.get(pid, "?"), "club": clubs.get(pid, "?"),
                    "news": p["news"], "status": p["status"],
                    "chance": p.get("chance_of_playing_next_round"),
                })
            squad.append({
                "id": pid, "name": names.get(pid, "?"), "club": clubs.get(pid, "?"),
                "pos": positions.get(pid, "?"), "status": p["status"],
                "chance": p.get("chance_of_playing_next_round"),
            })
        club_counts = {}
        for entry in squad:
            club_counts[entry["club"]] = club_counts.get(entry["club"], 0) + 1
        fixtures_block = []
        for team_id in squad_team_ids:
            club_name = TEAM_NAMES.get(team_id, "?")
            fixtures_block.append({
                "club": club_name, "count": club_counts.get(club_name, 0),
                "difficulty": (fixture_by_team or {}).get(team_id, []),
            })
        fixtures_block.sort(key=lambda x: x["club"])
        return {
            "available": True,
            "lineup_set": False,  # trup kendt, men ikke en specifik startopstilling endnu
            "squad": squad,
            "fixtures": fixtures_block,
            "injury_news": injury_news,
        }

    starters, bench = [], []
    squad_team_ids = set()
    injury_news = []
    for pick in picks:
        pid = pick["element"]
        p = by_id.get(pid)
        if not p:
            continue
        squad_team_ids.add(p["team"])
        if p["status"] != "a" and p.get("news"):
            injury_news.append({
                "player": names.get(pid, "?"), "club": clubs.get(pid, "?"),
                "news": p["news"], "status": p["status"],
                "chance": p.get("chance_of_playing_next_round"),
            })
        entry = {
            "id": pid, "name": names.get(pid, "?"), "club": clubs.get(pid, "?"),
            "pos": positions.get(pid, "?"), "status": p["status"],
            "chance": p.get("chance_of_playing_next_round"),
        }
        (starters if pick.get("position", 0) <= 11 else bench).append(entry)

    # Ombytningsforslag: kun starter->bænk-spillere på SAMME position, og kun hvis starteren
    # er flagget (skadet/tvivlsom). Maks 3, ingen hvis der ikke er noget reelt problem.
    suggestions = []
    for s in starters:
        if s["status"] == "a" and (s["chance"] is None or s["chance"] >= 75):
            continue  # ingen problem med denne starter
        same_pos_bench = [b for b in bench if b["pos"] == s["pos"] and b["status"] == "a"]
        if same_pos_bench:
            best_alt = same_pos_bench[0]
            suggestions.append({
                "out": s["name"], "in": best_alt["name"], "type": "Line-up",
                "reason": f"{s['name']} er flagget ({s['chance']}% spilchance) - {best_alt['name']} er tilgængelig på samme position.",
            })
        else:
            suggestions.append({
                "out": s["name"], "in": None, "type": "Formation",
                "reason": f"{s['name']} er flagget ({s['chance']}% spilchance), men ingen bænket spiller på samme position kan erstatte 1:1 - overvej et formationsskift.",
            })
    suggestions = suggestions[:3]

    # Kampprogram for de klubber der reelt er repræsenteret i truppen
    club_counts = {}
    for entry in starters + bench:
        club_counts[entry["club"]] = club_counts.get(entry["club"], 0) + 1
    fixtures_block = []
    for team_id in squad_team_ids:
        club_name = TEAM_NAMES.get(team_id, "?")
        fixtures_block.append({
            "club": club_name,
            "count": club_counts.get(club_name, 0),
            "difficulty": (fixture_by_team or {}).get(team_id, []),
        })
    fixtures_block.sort(key=lambda x: x["club"])

    return {
        "available": True,
        "lineup_set": True,
        "starters": starters,
        "bench": bench,
        "suggestions": suggestions,
        "fixtures": fixtures_block,
        "injury_news": injury_news,
    }


def build_transaction_history(league_details_id, entry_name_map, player_names):
    data = fetch_json(f"{DRAFT_BASE}/draft/league/{league_details_id}/transactions")
    kind_labels = {"w": "Waiver", "f": "Free agent", "t": "Trade"}
    out = []
    for t in data.get("transactions", []):
        if t.get("result") != "a":  # kun gennemførte (accepterede) transaktioner
            continue
        kind = kind_labels.get(t.get("kind"), t.get("kind", "Transaktion"))
        entry_id = t.get("entry")
        in_name = player_names.get(t.get("element_in"))
        out_name = player_names.get(t.get("element_out"))
        out.append({
            "gw": t.get("event"),
            "entry_name": entry_name_map.get(entry_id, f"Entry {entry_id}"),
            "kind": kind,
            "player_in": in_name,
            "player_out": out_name,
            "added": t.get("added"),
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{FPL_BASE}/fixtures/")
    league_details = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")
    element_status = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/element-status")["element_status"]
    raw_transactions = fetch_json(f"{DRAFT_BASE}/draft/league/{LEAGUE_ID}/transactions").get("transactions", [])
    element_status = get_corrected_element_status(element_status, raw_transactions)
    picks_history = load_json_file(PICKS_HISTORY_FILE, {})

    entry_name_map, entry_id_by_league_id, real_entry_ids = get_league_entries(league_details)

    latest_event = find_latest_finished_event(bootstrap)
    next_event = find_next_event(bootstrap)
    current_gw = latest_event["id"] if latest_event else 0
    season_started = latest_event is not None
    kicked_off = has_season_kicked_off(bootstrap)  # GW1-deadline passeret, uanset 'finished'-status

    fixture_by_team = get_team_fixture_difficulty(fixtures, num_gws=5)

    # ---- standings + rank history ----
    standings_rows, current_ranks, point_gap = build_standings_block(league_details, entry_name_map)
    rank_history = load_json_file(RANK_HISTORY_FILE, {})
    if season_started:
        rank_history = update_rank_history(rank_history, current_gw, current_ranks, standings_rows)
        save_json_file(RANK_HISTORY_FILE, rank_history)
    trend = compute_trend(rank_history, current_ranks, current_gw) if season_started else {}
    for r in standings_rows:
        r["trend"] = trend.get(str(r["league_entry"]))

    # ---- alerts ----
    alerts = build_alerts(bootstrap, element_status, entry_name_map)
    league_transfer_news = build_league_transfer_news(bootstrap, element_status, entry_name_map, kicked_off)

    # ---- season highlights ----
    highlights = {"highest_gw_score": None, "longest_streak": None}
    if len(rank_history) >= 1:
        sorted_gws = sorted(rank_history.keys(), key=lambda k: int(k[2:]))
        best_score, best_entry, best_gw = 0, None, None
        prev_totals = {}
        for gw_key in sorted_gws:
            totals = rank_history[gw_key]["totals"]
            for eid, total in totals.items():
                gw_score = total - prev_totals.get(eid, 0)  # denne uges point, ikke kumuleret total
                if gw_score > best_score:
                    best_score, best_entry, best_gw = gw_score, eid, gw_key
            prev_totals = totals
        if best_entry:
            highlights["highest_gw_score"] = {
                "name": entry_name_map.get(int(best_entry), "?"), "score": best_score, "gw": best_gw
            }

    # ---- bench trend (kun hvis sæsonen er i gang - ellers dyrt/meningsløst at hente) ----
    bench_trend = {}
    if season_started:
        live_points_by_gw = {}
        for gw in range(1, current_gw + 1):
            live = fetch_json(f"{FPL_BASE}/event/{gw}/live")
            live_points_by_gw[gw] = get_live_points_map(live)
        bench_trend = build_bench_trend(bootstrap, real_entry_ids, live_points_by_gw, picks_history)

    # ---- transaktionshistorik ----
    player_names_all = get_player_names(bootstrap)
    transactions = build_transaction_history(LEAGUE_ID, entry_name_map, player_names_all)

    site_data = {
        "updated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "current_gw": current_gw,
        "season_started": season_started,
        "next_deadline": next_event["deadline_time"] if next_event else None,
        "standings": standings_rows,
        "point_gap": point_gap,
        "alerts": alerts,
        "league_transfer_news": league_transfer_news,
        "highlights": highlights,
        "bench_trend": bench_trend,
        "transactions": transactions,
    }
    save_json_file("site-data.json", site_data)
    print(f"site-data.json skrevet ({len(standings_rows)} hold, GW{current_gw}, season_started={season_started})")

    # ---- Powerranking, GW-resumeer, waiver-/bytte-forslag og draft rankings: SPRINGES OVER ----
    # Ingen af disse vises noget sted i den nuværende, minimale side (kun Draft-
    # fanen, inaktiv indtil næste redraft) - fjernet for ikke at brænde Gemini-
    # kald af på data ingen ser. Genopbyg denne del når siden aktiveres igen.

    # ---- management (kun dig) ----
    current_playing_gw = find_current_playing_gw(bootstrap)
    management = build_management(bootstrap, current_playing_gw, fixture_by_team, element_status)
    management["updated"] = site_data["updated"]
    save_json_file("management.json", management)
    print("management.json skrevet, available=", management.get("available"))

    save_json_file(PICKS_HISTORY_FILE, picks_history)
    print(f"picks-history.json opdateret ({len(picks_history)} gameweeks frosset)")


if __name__ == "__main__":
    main()
