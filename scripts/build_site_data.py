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
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from fpl_common import (
    FPL_BASE, DRAFT_BASE, LEAGUE_ID, TEAM_NAMES,
    fetch_json, load_json_file, save_json_file,
    get_player_full_name, get_player_positions, get_player_names, get_player_clubs,
    get_league_entries, find_latest_finished_event, find_next_event,
    get_team_fixture_difficulty, get_live_points_map,
)

MY_ENTRY_ID = 1510  # Rasmus / "HaCunha Mateta" - Management-fanen er bygget til dig specifikt

RANK_HISTORY_FILE = "rank-history.json"
GW_SUMMARIES_FILE = "gw-summaries.json"


def gemini_call(prompt, expect_json=False):
    api_key = os.environ["GEMINI_API_KEY"]
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "Mozilla/5.0 (compatible; fplhq-bot/1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def safe_gemini_json(prompt, fallback):
    try:
        text = gemini_call(prompt, expect_json=True)
        return json.loads(text)
    except Exception as e:
        print(f"Gemini JSON-kald fejlede ({e}), bruger fallback.", file=sys.stderr)
        return fallback


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


def build_bench_trend(bootstrap, entry_ids, live_points_by_gw):
    """Kumuleret sæson-bænkpoint pr. manager, på tværs af alle spillede gameweeks."""
    totals = {str(eid): 0 for eid in entry_ids}
    for gw, live_points in live_points_by_gw.items():
        for eid in entry_ids:
            picks = get_entry_gw_picks(eid, gw)
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

def build_gw_summary(gw, standings_rows, entry_name_map, best_line, worst_line):
    prompt = (
        "Skriv et sagligt, analytisk resumé (omkring 150 ord, på dansk) af en gameweek i en lille "
        "Fantasy Premier League Draft-liga mellem venner. Seriøs, journalistisk tone - IKKE drillende "
        "eller morsom, det er en anden kanal end vores Discord-bot. Brug holdenes navne. Del op i "
        "korte afsnit. Nævn kort hvem der lå bedst og dårligst, og en generel observation om ugen.\n\n"
        f"Stilling efter GW{gw}:\n"
        + "\n".join(f"{r['rank']}. {r['name']} — {r['total']} point" for r in standings_rows)
        + f"\n\nBedste enkeltspiller: {best_line}\nDårligste enkeltspiller: {worst_line}\n"
    )
    try:
        return gemini_call(prompt)
    except Exception as e:
        print(f"GW-resumé fejlede ({e}), springer over.", file=sys.stderr)
        return None


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


def compute_power_score(p, fixture_by_team, kicked_off, last_season_ppg=None):
    """
    Vægtet Power Score:
      55% form (FPL's egen 'form'-stat; falder tilbage til points_per_game før sæsonstart)
      25% fixture-sværhedsgrad (næste kamp, omvendt skala - let fixture = højere score)
      15% nylig trend (form vs. sæsongennemsnit - kun meningsfuldt når sæsonen er i gang)
      5%  nettotransfers ind denne uge (momentum)
    Returnerer et 0-100-agtigt tal, ikke en eksakt procent.
    """
    form = float(p.get("form") or 0)
    ppg = float(p.get("points_per_game") or 0)
    season_minutes = float(p.get("minutes") or 0)
    reference_ppg = last_season_ppg if last_season_ppg is not None else ppg
    if form > 0:
        w = min(season_minutes / 900, 1.0)
        effective_form = w * form + (1 - w) * reference_ppg
    else:
        effective_form = reference_ppg

    fixtures = fixture_by_team.get(p["team"], [])
    next_diff = fixtures[0] if fixtures else 3
    fixture_score = (5 - next_diff) / 4 * 10  # 1=let->10, 5=svært->0

    if kicked_off and form > 0:
        trend = form - ppg  # er formen bedre end sæson-snittet lige nu?
    else:
        trend = 0

    net_transfers = (p.get("transfers_in_event") or 0) - (p.get("transfers_out_event") or 0)
    transfer_score = max(-5, min(5, net_transfers / 20000))  # dæmpet, undgår at ét viralt navn dominerer

    raw = (effective_form * 0.55) + (fixture_score * 0.25) + (trend * 0.15) + (transfer_score * 0.05)
    return round(raw * 10, 1)  # skaleret til en mere "point-agtig" 0-100ish størrelse


def build_ranked_list(bootstrap, fixture_by_team, kicked_off, position_filter=None, top_n=25):
    positions = get_player_positions(bootstrap)
    candidates = []
    for p in bootstrap["elements"]:
        if p.get("removed"):
            continue
        if p["status"] not in ("a", "d"):  # udelad langtidsskadede/suspenderede helt fra ranking
            continue
        pos = positions[p["id"]]
        if position_filter and pos != position_filter:
            continue
        minutes = float(p.get("minutes") or 0)
        if minutes == 0 and float(p.get("total_points") or 0) == 0:
            continue  # ingen reelt spillegrundlag at vurdere ud fra
        if not kicked_off and minutes < 900:
            # Reliability-filter FØR sæsonstart: her ER points_per_game reelt sidste
            # sæsons snit, så et lavt minuttal betyder et upålideligt lille sample.
            continue
        candidates.append(p)

    if not kicked_off:
        # Pre-season: bootstrap's egen points_per_game ER sidste sæsons stabile snit,
        # ingen grund til dyre ekstra-kald.
        scored = [
            {
                "id": p["id"], "name": get_player_full_name(p),
                "club": TEAM_NAMES.get(p["team"], "?"), "team_id": p["team"],
                "pos": positions[p["id"]], "score": compute_power_score(p, fixture_by_team, kicked_off),
                "last_season_points": p.get("total_points", 0), "points_this_season_so_far": 0, "form": p.get("form"),
                "status": p["status"], "chance": p.get("chance_of_playing_next_round"),
            }
            for p in candidates
        ]
        scored.sort(key=lambda x: -x["score"])
        return scored[:top_n]

    # Sæsonen er i gang: points_per_game er IKKE længere sidste sæson (den nulstiller
    # med det samme), så et groft førsteudkast (uden ægte sidste-sæson-reference) bruges
    # kun til at finde en kandidat-pulje - derefter hentes ægte sidste-sæson-data (dyrere
    # per-spiller-kald) KUN for de kandidater, ikke for alle ~590 spillere.
    rough = [(p, compute_power_score(p, fixture_by_team, kicked_off)) for p in candidates]
    rough.sort(key=lambda x: -x[1])
    pool_size = min(len(rough), max(top_n * 3, 60))
    pool = rough[:pool_size]

    scored = []
    for p, _rough_score in pool:
        last_stats = fetch_last_season_stats(p["id"])
        season_minutes = float(p.get("minutes") or 0)
        if last_stats is None and season_minutes < 180:
            # Hverken en pålidelig sidste-sæson-historik ELLER nok kampe i den nye
            # sæson til at stå alene - uden nogen bremseklods kan én god/dårlig
            # enkeltkamp fuldstændig dominere. Springes over indtil et af de to
            # kriterier er opfyldt.
            continue
        last_ppg = last_stats["ppg"] if last_stats else None
        real_score = compute_power_score(p, fixture_by_team, kicked_off, last_season_ppg=last_ppg)
        scored.append({
            "id": p["id"], "name": get_player_full_name(p),
            "club": TEAM_NAMES.get(p["team"], "?"), "team_id": p["team"],
            "pos": positions[p["id"]], "score": real_score,
            "last_season_points": last_stats["total_points"] if last_stats else None,
            "points_this_season_so_far": p.get("total_points", 0),
            "form": p.get("form"),
            "status": p["status"], "chance": p.get("chance_of_playing_next_round"),
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_n]


def add_ai_arguments(ranked_list, list_label, fixture_by_team=None):
    """Ét samlet Gemini-kald pr. liste (ikke ét pr. spiller) - langt billigere og hurtigere."""
    if not ranked_list:
        return ranked_list
    lines = []
    for i, p in enumerate(ranked_list):
        fixt = ""
        if fixture_by_team:
            diffs = fixture_by_team.get(p.get("team_id"), [])
            if diffs:
                avg = sum(diffs[:3]) / len(diffs[:3])
                fixt = f", næste 3 kampes sværhedsgrad-snit {avg:.1f}/5"
        last_season = p.get("last_season_points")
        this_season = p.get("points_this_season_so_far") or 0
        season_bits = []
        if last_season is not None:
            season_bits.append(f"{last_season} point sidste sæson (2025/26)")
        if this_season > 0:
            season_bits.append(f"{this_season} point denne sæson indtil videre")
        season_txt = ", ".join(season_bits) if season_bits else "ingen sæsondata"
        lines.append(
            f"{i+1}. {p['name']} ({p['club']}, {p['pos']}) - {season_txt}, "
            f"status {p['status']}{fixt}"
        )
    players_block = "\n".join(lines)
    prompt = (
        f"Du får en rangeret liste over {list_label} i Fantasy Premier League. Skriv ÉT kort, "
        "naturligt argument pr. spiller (maks 20 ord, på dansk) for hvorfor de er et godt/dårligt "
        "valg lige nu. Vær PRÆCIS om hvilken sæson et tal refererer til - bland ALDRIG 'sidste sæson' "
        "og 'denne sæson indtil videre' sammen. Varier formuleringen mellem spillerne - gentag IKKE "
        "samme sætningsskabelon ('med en score på X og Y point...') for hver spiller. Brug KUN de tal "
        "der er givet, opfind aldrig kampresultater, mål eller hændelser der ikke fremgår af dataen - "
        "er der ikke andet at sige end pointtal og fixtures, så sig det naturligt uden at lyde robotagtigt.\n\n"
        f"{players_block}\n\n"
        'Svar KUN som gyldig JSON: en liste af strenge, i samme rækkefølge som spillerne, '
        'fx ["argument 1", "argument 2", ...]. Ingen anden tekst.'
    )
    fallback = [
        f"{p['last_season_points']} point sidste sæson." if p.get("last_season_points") is not None
        else "Ingen sæsondata tilgængelig."
        for p in ranked_list
    ]
    args = safe_gemini_json(prompt, fallback)
    if not isinstance(args, list) or len(args) != len(ranked_list):
        args = fallback
    for p, arg in zip(ranked_list, args):
        p["argument"] = arg
    return ranked_list


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

# ---------------------------------------------------------------------------
# Transfer-forslag (waivers): sammenlign din trup mod ledige spillere
# ---------------------------------------------------------------------------

MIN_WAIVER_GAP = 12  # skal være et reelt løft, ikke 1-2 points forskel, for at foreslås

def build_unowned_pool(bootstrap, element_status, fixture_by_team, kicked_off, position, top_n=15):
    """Samme to-trins scoring som build_ranked_list, men kun blandt UEJEDE spillere."""
    owned_ids = {es["element"] for es in element_status if es.get("owner")}
    positions = get_player_positions(bootstrap)
    candidates = []
    for p in bootstrap["elements"]:
        if p["id"] in owned_ids or p.get("removed"):
            continue
        if p["status"] not in ("a", "d"):
            continue
        if positions.get(p["id"]) != position:
            continue
        minutes = float(p.get("minutes") or 0)
        if minutes == 0 and float(p.get("total_points") or 0) == 0:
            continue
        if not kicked_off and minutes < 900:
            continue
        candidates.append(p)

    rough = [(p, compute_power_score(p, fixture_by_team, kicked_off)) for p in candidates]
    rough.sort(key=lambda x: -x[1])
    pool = rough[:min(len(rough), max(top_n * 2, 20))]

    scored = []
    for p, _ in pool:
        last_stats = None
        if kicked_off:
            last_stats = fetch_last_season_stats(p["id"])
            season_minutes = float(p.get("minutes") or 0)
            if last_stats is None and season_minutes < 180:
                continue
            last_ppg = last_stats["ppg"] if last_stats else None
            score = compute_power_score(p, fixture_by_team, kicked_off, last_season_ppg=last_ppg)
        else:
            score = compute_power_score(p, fixture_by_team, kicked_off)
        scored.append({
            "id": p["id"], "name": get_player_full_name(p),
            "club": TEAM_NAMES.get(p["team"], "?"), "team_id": p["team"], "score": score,
            "last_season_points": last_stats["total_points"] if last_stats else p.get("total_points", 0),
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_n]


HARD_FIXTURE_THRESHOLD = 4       # sværhedsgrad 4-5 tæller som "svær" kamp
MAX_HARD_FIXTURES_ALLOWED = 3     # højst 3 ud af 5 svære kampe må den indkommende have
QUALITY_TIER_RATIO = 1.7          # hvis din spiller havde >1.7x så mange point sidste sæson,
                                    # er det formentlig en anden liga-klasse - byt ikke bare pga. et kort opsving


def build_waiver_suggestions(bootstrap, element_status, fixture_by_team, kicked_off, management):
    """
    Sammenligner hver af dine spillere mod den bedst-scorende LEDIGE spiller på
    samme position. Foreslår kun et bytte hvis:
      1) forspringet er reelt (MIN_WAIVER_GAP), ikke marginale forskelle
      2) den indkommende spillers næste 5 kampe ikke overvejende er svære
      3) din spiller ikke er en klart anden liga-klasse end den indkommende
         (forhindrer fx "byt Bruno Fernandes for en Brighton-reserve fordi
         han har et blidt kampprogram lige nu" - det er stadig en dårlig idé)
    Maks 5 forslag, ingen hvis intet reelt forbedrer sig.
    """
    if not management.get("available"):
        return []
    my_players = management.get("starters") or management.get("squad") or []
    positions_needed = {p["pos"] for p in my_players}
    unowned_by_pos = {pos: build_unowned_pool(bootstrap, element_status, fixture_by_team, kicked_off, pos)
                       for pos in positions_needed}

    by_id = {p["id"]: p for p in bootstrap["elements"]}
    candidates = []
    for mp in my_players:
        p = by_id.get(mp["id"])
        if not p:
            continue
        if kicked_off:
            last_stats = fetch_last_season_stats(mp["id"])
            my_last_season = last_stats["total_points"] if last_stats else p.get("total_points", 0)
            last_ppg = last_stats["ppg"] if last_stats else None
            my_score = compute_power_score(p, fixture_by_team, kicked_off, last_season_ppg=last_ppg)
        else:
            my_last_season = p.get("total_points", 0)
            my_score = compute_power_score(p, fixture_by_team, kicked_off)

        best_available = unowned_by_pos.get(mp["pos"], [])
        best_available = [a for a in best_available if a["id"] != mp["id"]]
        if not best_available:
            continue
        best = best_available[0]
        gap = best["score"] - my_score
        if gap < MIN_WAIVER_GAP:
            continue

        # Spærre-regel 1: for mange svære kampe i vente for den indkommende
        diffs = fixture_by_team.get(best.get("team_id"), [])
        hard_count = sum(1 for d in diffs[:5] if d >= HARD_FIXTURE_THRESHOLD)
        if hard_count > MAX_HARD_FIXTURES_ALLOWED:
            continue

        # Spærre-regel 2: din spiller er en klart anden liga-klasse end den indkommende
        incoming_last_season = best.get("last_season_points") or 0
        if my_last_season > 0 and incoming_last_season > 0:
            if my_last_season / max(incoming_last_season, 1) > QUALITY_TIER_RATIO:
                continue

        candidates.append({
            "out": mp["name"], "out_score": round(my_score, 1),
            "in": best["name"], "in_club": best["club"], "in_score": round(best["score"], 1),
            "gap": round(gap, 1), "pos": mp["pos"],
        })
    candidates.sort(key=lambda x: -x["gap"])
    return candidates[:5]


def add_waiver_arguments(suggestions):
    """Ét samlet Gemini-kald - argumentet skal pege på DIN fordel, ikke generisk statistik."""
    if not suggestions:
        return suggestions
    lines = [
        f"{i+1}. Drop {s['out']} (score {s['out_score']}) for {s['in']} fra {s['in_club']} "
        f"(score {s['in_score']}, {s['pos']})"
        for i, s in enumerate(suggestions)
    ]
    prompt = (
        "Du får en liste over foreslåede waiver-bytter i Fantasy Premier League Draft. Skriv ÉT kort "
        "argument pr. forslag (maks 25 ord, på dansk) der forklarer hvorfor DENNE ÆNDRING gavner "
        "brugeren specifikt - fokusér på hvad brugeren vinder, ikke generel statistik. Brug KUN tallene "
        "givet, opfind aldrig konkrete kampe eller hændelser.\n\n"
        + "\n".join(lines) +
        '\n\nSvar KUN som gyldig JSON: en liste af strenge i samme rækkefølge, fx ["argument 1", ...].'
    )
    fallback = [f"{s['in']} scorer {s['gap']} point højere end {s['out']} lige nu." for s in suggestions]
    args = safe_gemini_json(prompt, fallback)
    if not isinstance(args, list) or len(args) != len(suggestions):
        args = fallback
    for s, arg in zip(suggestions, args):
        s["reason"] = arg
    return suggestions


def main():
    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{FPL_BASE}/fixtures/")
    league_details = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")
    element_status = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/element-status")["element_status"]

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

    # ---- gw summaries (kun ved en NY færdigspillet gameweek) ----
    gw_summaries = load_json_file(GW_SUMMARIES_FILE, [])
    already_summarized = {s["gw"] for s in gw_summaries}
    if season_started and current_gw not in already_summarized:
        best_line = worst_line = "Ingen data"  # kræver picks pr. manager - se league_update.py for fuld logik
        text = build_gw_summary(current_gw, standings_rows, entry_name_map, best_line, worst_line)
        if text:
            gw_summaries.insert(0, {"gw": current_gw, "text": text})
            gw_summaries = gw_summaries[:10]  # behold kun de seneste 10
            save_json_file(GW_SUMMARIES_FILE, gw_summaries)

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
        bench_trend = build_bench_trend(bootstrap, real_entry_ids, live_points_by_gw)

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
        "gw_summaries": gw_summaries,
        "highlights": highlights,
        "bench_trend": bench_trend,
        "transactions": transactions,
    }
    save_json_file("site-data.json", site_data)
    print(f"site-data.json skrevet ({len(standings_rows)} hold, GW{current_gw}, season_started={season_started})")

    # ---- powerranking ----
    pr_list = build_ranked_list(bootstrap, fixture_by_team, kicked_off, position_filter=None, top_n=25)
    pr_list = add_ai_arguments(pr_list, "spillere i Fantasy Premier League (alle positioner)", fixture_by_team)
    save_json_file("powerranking.json", {"updated": site_data["updated"], "players": pr_list})
    print(f"powerranking.json skrevet ({len(pr_list)} spillere)")

    # ---- draft rankings: SPRINGES OVER ----
    # Draft-fanen er deaktiveret i UI'en indtil næste redraft (feb 2027) - ingen grund
    # til at bruge 4 Gemini-kald pr. kørsel på data ingen ser. Genaktivér denne blok
    # (og fjern deaktiveringen i index.html) når redraften nærmer sig.

    # ---- management (kun dig) ----
    current_playing_gw = find_current_playing_gw(bootstrap)
    management = build_management(bootstrap, current_playing_gw, fixture_by_team, element_status)
    management["updated"] = site_data["updated"]
    save_json_file("management.json", management)
    print("management.json skrevet, available=", management.get("available"))

    # ---- waiver-forslag (kræver management-data, derfor tilføjet til site-data.json bagefter) ----
    waiver_suggestions = build_waiver_suggestions(bootstrap, element_status, fixture_by_team, kicked_off, management)
    waiver_suggestions = add_waiver_arguments(waiver_suggestions)
    site_data["transfer_suggestions"] = waiver_suggestions
    save_json_file("site-data.json", site_data)
    print(f"site-data.json opdateret med {len(waiver_suggestions)} waiver-forslag")


if __name__ == "__main__":
    main()
