"""
Poster en automatisk gameweek-opsummering til Discord for FPL Draft-ligaen.

Kilde til data: FPL Draft's officielle API (draft.premierleague.com/api).
AI-tekst: Gemini API (kort, morsom opsummering af runden).
Destination: Discord webhook.

Køres af .github/workflows/league-update.yml på en cron. Er idempotent —
poster kun én gang pr. gameweek, styret af league-state.json i repoet.
"""
import json
import os
import sys
import urllib.request
import urllib.error

LEAGUE_ID = 668
STATE_FILE = "league-state.json"
PICKS_HISTORY_FILE = "picks-history.json"

FPL_BASE = "https://fantasy.premierleague.com/api"
DRAFT_BASE = "https://draft.premierleague.com/api"

TEAM_NAMES = {
    1: 'Arsenal', 2: 'Aston Villa', 3: 'Bournemouth', 4: 'Brentford', 5: 'Brighton',
    6: 'Chelsea', 7: 'Coventry', 8: 'Crystal Palace', 9: 'Everton', 10: 'Fulham',
    11: 'Hull', 12: 'Ipswich', 13: 'Leeds', 14: 'Liverpool', 15: 'Man City',
    16: 'Man Utd', 17: 'Newcastle', 18: "Nott'm Forest", 19: 'Spurs', 20: 'Sunderland',
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "drafthq-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_posted_event": 0, "last_ranks": {}, "last_transaction_count": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_picks_history():
    if os.path.exists(PICKS_HISTORY_FILE):
        with open(PICKS_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_picks_history(history):
    with open(PICKS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_frozen_squad(entry_id, gw, picks_history):
    """
    FPL Draft's /entry/{id}/event/{gw} viste sig IKKE at være et pålideligt
    historisk øjebliksbillede - det kan reflektere en senere ændret trup, hvis
    nogen bytter spillere på en måde der ikke går gennem den sporede
    waiver/trade-log (bekræftet med et konkret, uforklaret tilfælde). Løsningen:
    første gang vi ser en gameweek, fryser vi picks-dataen permanent i
    picks-history.json. Alle senere kald genbruger den frosne kopi i stedet for
    at spørge FPL igen, så data aldrig kan "drifte" efter at være gemt.
    """
    gw_key = f"GW{gw}"
    entry_key = str(entry_id)
    if gw_key in picks_history and entry_key in picks_history[gw_key]:
        return picks_history[gw_key][entry_key]

    picks = get_entry_gw_squad(entry_id, gw)
    if picks is None:
        return None
    picks_history.setdefault(gw_key, {})[entry_key] = picks
    return picks


def find_latest_finished_event(bootstrap):
    """Finder seneste gameweek der er færdigspillet OG har låste bonuspoint."""
    candidates = [e for e in bootstrap["events"] if e["finished"] and e["data_checked"]]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["id"])


DA_WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
DA_MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
             "august", "september", "oktober", "november", "december"]


def format_deadline_da(iso_ts):
    """Formatterer en UTC-ISO-timestamp til dansk, fx 'lørdag 29. august kl. 15:00'."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    weekday = DA_WEEKDAYS[dt.weekday()]
    month = DA_MONTHS[dt.month - 1]
    return f"{weekday} {dt.day}. {month} kl. {dt.strftime('%H:%M')} UTC"


def find_next_deadline(bootstrap):
    """
    Finder næste RELEVANTE deadline - dvs. tidligste gameweek hvis deadline
    stadig ligger i fremtiden. 'finished' bliver ikke True før alle kampe +
    bonuspoint er bekræftet, hvilket kan tage dage efter deadline er passeret,
    så vi kan IKKE bare filtrere på 'not finished'.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in bootstrap["events"]:
        deadline = datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))
        if deadline > now:
            upcoming.append(e)
    if not upcoming:
        return None
    nxt = min(upcoming, key=lambda e: e["id"])
    return nxt["id"], nxt["deadline_time"]


def get_owned_player_news(bootstrap, element_status, entry_name_map):
    """Finder skader/status-flag KUN for spillere ejet i vores liga — relevant, ikke generisk støj."""
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    owner_by_element = {es["element"]: es["owner"] for es in element_status if es.get("owner")}
    lines = []
    for element_id, owner_entry_id in owner_by_element.items():
        p = by_id.get(element_id)
        if not p or p["status"] == "a" or not p.get("news"):
            continue
        fn = (p.get("first_name") or "").strip()
        sn = (p.get("second_name") or "").strip()
        name = (fn + " " + sn).strip() or p["web_name"]
        owner_name = entry_name_map.get(owner_entry_id, f"Entry {owner_entry_id}")
        lines.append(f"{name} ({owner_name}): {p['news']}")
    return lines


def get_live_elements_normalized(live):
    """
    FPL's /event/{gw}/live returnerer 'elements' som et dict (nøglet på spiller-ID
    som streng) MENS en gameweek er i gang, men som en LISTE (hvert element med sit
    eget 'id'-felt) når gameweeken er markeret helt færdig. Bekræftet begge dele
    direkte - denne normaliserer til en liste af (player_id, stats_dict).
    """
    elements = live.get("elements", {})
    if isinstance(elements, dict):
        return [(int(pid), d) for pid, d in elements.items()]
    return [(item["id"], item) for item in elements]


def get_top_gw_performers(bootstrap, live, top_n=3):
    """Top-scorere i HELE Premier League denne gameweek (ikke kun i vores liga) — ægte data, ikke gæt."""
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    scored = []
    for pid, pdata in get_live_elements_normalized(live):
        pts = pdata["stats"]["total_points"]
        if pts <= 0:
            continue
        p = by_id.get(pid)
        if not p:
            continue
        fn = (p.get("first_name") or "").strip()
        sn = (p.get("second_name") or "").strip()
        name = (fn + " " + sn).strip() or p["web_name"]
        scored.append((pts, name))
    scored.sort(reverse=True)
    return scored[:top_n]


POS_MAP = {'GKP': 'GK', 'DEF': 'DEF', 'MID': 'MID', 'FWD': 'FWD'}


def get_player_positions(bootstrap):
    et = {e["id"]: POS_MAP[e["singular_name_short"]] for e in bootstrap["element_types"]}
    return {p["id"]: et[p["element_type"]] for p in bootstrap["elements"]}


def get_tottenham_result(bootstrap, live):
    """Tjekker om Tottenham (team_id 19) tabte eller spillede uafgjort denne gameweek."""
    SPURS_ID = 19
    for fx in live.get("fixtures", []):
        if not fx.get("finished"):
            continue
        if fx["team_h"] == SPURS_ID or fx["team_a"] == SPURS_ID:
            spurs_home = fx["team_h"] == SPURS_ID
            spurs_score = fx["team_h_score"] if spurs_home else fx["team_a_score"]
            opp_score = fx["team_a_score"] if spurs_home else fx["team_h_score"]
            if spurs_score is None or opp_score is None:
                return None
            if spurs_score < opp_score:
                return f"Tottenham TABTE {spurs_score}-{opp_score}"
            if spurs_score == opp_score:
                return f"Tottenham spillede uafgjort {spurs_score}-{opp_score}"
            return None  # de vandt, ingen særlig svining påkrævet
    return None


def get_player_names(bootstrap):
    # Bruger FPL's eget 'web_name' - det navn der reelt bruges (fx "Raya", "Thiago"),
    # ikke det fulde juridiske navn (fx "David Raya Martín").
    return {p["id"]: p["web_name"] for p in bootstrap["elements"]}


def get_entry_gw_squad(entry_id, event_id):
    """Henter en managers picks for en given gameweek. Returnerer None hvis ingen data."""
    url = f"{DRAFT_BASE}/entry/{entry_id}/event/{event_id}"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError:
        return None
    if not isinstance(data, dict) or "picks" not in data:
        return None
    return data["picks"]


def run_season_kickoff():
    """
    Engangs-besked til sæsonstart: ønsker held og lykke, opsummerer de waivers/trades
    folk har lavet i preseason. Bruger samme humor/tone som de normale GW-opdateringer,
    men er ikke en gameweek-rapport - ingen stilling/bedste-spiller-data findes endnu.
    Trigges kun manuelt (SEASON_KICKOFF=true), påvirker ikke last_posted_event-state.
    """
    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    next_deadline = find_next_deadline(bootstrap)
    player_names = get_player_names(bootstrap)

    league = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")
    entry_name_map = {}
    for e in league["league_entries"]:
        entry_name_map[e["id"]] = e["entry_name"]
        entry_name_map[e["entry_id"]] = e["entry_name"]

    trans_data = fetch_json(f"{DRAFT_BASE}/draft/league/{LEAGUE_ID}/transactions")
    accepted = [t for t in trans_data.get("transactions", []) if t.get("result") == "a"]
    kind_labels = {"w": "Waiver", "f": "Free agent", "t": "Trade"}
    trans_lines = []
    for t in accepted:
        kind = kind_labels.get(t.get("kind"), t.get("kind", "transaction"))
        entry_id = t.get("entry")
        in_name = player_names.get(t.get("element_in"), f"spiller {t.get('element_in')}")
        out_name = player_names.get(t.get("element_out"))
        ename = entry_name_map.get(entry_id, f"Entry {entry_id}")
        desc = f"{in_name} ind, {out_name} ud" if out_name else f"{in_name} ind"
        trans_lines.append(f"{ename}: {desc} ({kind})")

    deadline_line = "Ukendt — tjek draft.premierleague.com"
    if next_deadline:
        next_gw, deadline_ts = next_deadline
        deadline_line = f"GW{next_gw}: {format_deadline_da(deadline_ts)}"

    context = f"""I dag starter Premier League-sæsonen 2026/27, og dermed også vores FPL Draft-liga
"{league['league']['name']}" for alvor - GW1-deadline er {deadline_line}.

TRANSFERS LAVET I PRESEASON (waivers/trades siden draften):
{chr(10).join(trans_lines) if trans_lines else "Ingen waivers eller trades er lavet endnu."}
"""

    prompt = (
        "Du skriver en kort kickoff-besked (180-200 ord, på dansk) til en lille FPL Draft-liga "
        "mellem venner, til at poste i Discord - sæsonen starter i dag. Skriv i Discord-markdown "
        "(**fed** på navne), del op i korte afsnit, gerne et par relevante emojis, ikke overdrevet.\n\n"
        "Ønsk holdene held og lykke for sæsonen, og opsummér/kommentér på de transfers/waivers folk "
        "har lavet i preseason - vær gerne drillende og letsindig omkring valgene (kammeratligt, ikke "
        "ondskabsfuldt), præcis samme stil som vores almindelige gameweek-opdateringer. Brug KUN "
        "dataen herunder, opfind ALDRIG noget som helst der ikke er givet - ingen spillerpræstationer, "
        "skader, klubskifter, kampresultater eller andre nyheder du ikke kender. Brug ALDRIG egen "
        "baggrundsviden om spillere eller klubber (dette er en fiktiv liga-sæson).\n\n"
        f"Data:\n{context}"
    )
    summary_text = call_gemini_raw(prompt)

    if summary_text is None:
        print("Springer sæsonstart-besked over pga. Gemini-fejl - kør igen senere.", file=sys.stderr)
        return

    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    embed = {
        "title": "⚽ Sæsonstart!",
        "description": summary_text,
        "color": 2926465,
        "footer": {"text": f"Første deadline: {deadline_line}"},
    }
    discord_body = json.dumps({
        "username": "Update Bot", "content": "@everyone",
        "embeds": [embed], "allowed_mentions": {"parse": ["everyone"]},
    }).encode("utf-8")

    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("=== DRY_RUN: intet sendt til Discord, dette er hvad der VILLE være sendt ===")
        print(json.dumps(json.loads(discord_body), ensure_ascii=False, indent=2))
        print("=== DRY_RUN slut ===")
        return

    discord_req = urllib.request.Request(
        webhook, data=discord_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; drafthq-bot/1.0; +https://github.com/munksgaard91/fpl-hq)",
        },
        method="POST",
    )
    with urllib.request.urlopen(discord_req, timeout=20) as resp:
        print("Sæsonstart-besked postet, Discord response:", resp.status)


def main():
    if os.environ.get("SEASON_KICKOFF", "").lower() == "true":
        run_season_kickoff()
        return

    state = load_state()
    picks_history = load_picks_history()
    test_mode = os.environ.get("FORCE_TEST", "").lower() == "true"

    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    latest_event = find_latest_finished_event(bootstrap)

    if latest_event is None and not test_mode:
        print("Ingen færdigspillet gameweek endnu — intet at poste.")
        return

    if latest_event is None and test_mode:
        gw = 0  # ingen rigtig gameweek er færdig endnu
    else:
        gw = latest_event["id"]
        if gw <= state["last_posted_event"] and not test_mode:
            print(f"GW{gw} er allerede postet. Intet nyt.")
            return

    player_names = get_player_names(bootstrap)
    player_positions = get_player_positions(bootstrap)

    league = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")

    # IMPORTANT: FPL Draft uses two different IDs per manager that do NOT always match:
    #   - league_entries[].entry_id  -> the manager's global FPL account ID (needed for
    #     per-entry endpoints like /entry/{id}/event/{gw})
    #   - league_entries[].id        -> the league-scoped membership ID (this is what
    #     standings[].league_entry and matches[] actually reference)
    # We build one name lookup keyed by BOTH ids, and a separate map from league-scoped id
    # to the real entry_id for calls that need it.
    entry_name_map = {}
    entry_id_by_league_id = {}
    for e in league["league_entries"]:
        entry_name_map[e["id"]] = e["entry_name"]
        entry_name_map[e["entry_id"]] = e["entry_name"]
        entry_id_by_league_id[e["id"]] = e["entry_id"]

    real_entry_ids = [e["entry_id"] for e in league["league_entries"]]

    # -------- live points for the gameweek (hentes én gang, bruges flere steder) --------
    if gw > 0:
        live = fetch_json(f"{FPL_BASE}/event/{gw}/live")
        live_points = dict(
            (pid, pdata["stats"]["total_points"]) for pid, pdata in get_live_elements_normalized(live)
        )
    else:
        live = {"elements": {}}
        live_points = {}

    # -------- injury/news for owned players (altid aktuel, kræver ikke en spillet gameweek) --------
    element_status = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/element-status")["element_status"]
    owned_injury_lines = get_owned_player_news(bootstrap, element_status, entry_name_map)

    # -------- top overall PL performers + Tottenham-resultat (kræver reelt spillede kampe) --------
    if gw > 0:
        top_performers = get_top_gw_performers(bootstrap, live)
        tottenham_result = get_tottenham_result(bootstrap, live)
    else:
        top_performers = []
        tottenham_result = None

    # -------- per-entry squad + gw score (keyed by real entry_id) --------
    entry_gw_points = {}
    biggest_bench_regret = None  # (diff, entry_id, bench_name, bench_pts, starter_name, starter_pts)
    entry_best_player = {}   # entry_id -> (player_id, points)
    entry_worst_player = {}  # entry_id -> (player_id, points)
    league_best = None   # (points, player_id, entry_id)
    league_worst = None  # (points, player_id, entry_id)

    if gw > 0:
        for entry_id in real_entry_ids:
            picks = get_frozen_squad(entry_id, gw, picks_history)
            if not picks:
                continue
            starters = [p for p in picks if p.get("position", 0) <= 11]
            bench = [p for p in picks if p.get("position", 0) > 11]
            total = 0
            best = None
            worst = None
            for pick in starters:
                pid = pick["element"]
                pts = live_points.get(pid, 0)
                total += pts
                if best is None or pts > best[1]:
                    best = (pid, pts)
                if worst is None or pts < worst[1]:
                    worst = (pid, pts)
                if league_best is None or pts > league_best[0]:
                    league_best = (pts, pid, entry_id)
                if league_worst is None or pts < league_worst[0]:
                    league_worst = (pts, pid, entry_id)
            bench_total = sum(live_points.get(p["element"], 0) for p in bench)
            entry_gw_points[entry_id] = total
            if best:
                entry_best_player[entry_id] = best
            if worst:
                entry_worst_player[entry_id] = worst

            # Kun "regret" hvis en bænkspiller reelt slog den svageste starter på samme position.
            for bp in bench:
                bpid = bp["element"]
                bpts = live_points.get(bpid, 0)
                bpos = player_positions.get(bpid)
                for sp in starters:
                    spid = sp["element"]
                    spts = live_points.get(spid, 0)
                    spos = player_positions.get(spid)
                    if bpos != spos or bpts <= spts:
                        continue
                    diff = bpts - spts
                    if biggest_bench_regret is None or diff > biggest_bench_regret[0]:
                        biggest_bench_regret = (
                            diff, entry_id,
                            player_names.get(bpid, bpid), bpts,
                            player_names.get(spid, spid), spts,
                        )

    # -------- standings (cumulative, from league details) --------
    standings = sorted(league["standings"], key=lambda s: -(s["total"] or 0))
    current_ranks = {}
    standings_lines = []
    for i, s in enumerate(standings, start=1):
        eid = s["league_entry"]
        current_ranks[str(eid)] = i
        name = entry_name_map.get(eid, f"Entry {eid}")
        real_id = entry_id_by_league_id.get(eid)
        gw_pts = entry_gw_points.get(real_id)
        gw_suffix = f" *(+{gw_pts} denne uge)*" if gw_pts is not None else ""
        prefix = "🏆" if i == 1 else f"{i}."
        standings_lines.append(f"{prefix} **{name}** — {s['total']} point{gw_suffix}")

    point_gap = None
    if len(standings) >= 2:
        point_gap = (standings[0]["total"] or 0) - (standings[-1]["total"] or 0)

    last_place_name = None
    if standings:
        last_eid = standings[-1]["league_entry"]
        last_place_name = entry_name_map.get(last_eid, f"Entry {last_eid}")

    # -------- rank movement vs last posted gw --------
    movers = []
    for eid_str, rank_now in current_ranks.items():
        prev_rank = state["last_ranks"].get(eid_str)
        if prev_rank is not None:
            movers.append((entry_name_map.get(int(eid_str), eid_str), prev_rank, rank_now, prev_rank - rank_now))
    biggest_mover = max(movers, key=lambda m: abs(m[3])) if movers else None

    # -------- transactions since last post --------
    trans_data = fetch_json(f"{DRAFT_BASE}/draft/league/{LEAGUE_ID}/transactions")
    all_trans = [t for t in trans_data.get("transactions", []) if t.get("result") == "a"]
    new_trans = all_trans[state["last_transaction_count"]:]
    kind_labels = {"w": "Waiver", "f": "Free agent", "t": "Trade"}
    trans_lines = []
    for t in new_trans:
        kind = kind_labels.get(t.get("kind"), t.get("kind", "transaction"))
        entry_id = t.get("entry")
        in_name = player_names.get(t.get("element_in"), f"spiller {t.get('element_in')}")
        out_name = player_names.get(t.get("element_out"))
        ename = entry_name_map.get(entry_id, f"Entry {entry_id}")
        desc = f"{in_name} ind, {out_name} ud" if out_name else f"{in_name} ind"
        trans_lines.append(f"{ename}: {desc} ({kind})")

    # -------- assemble context for Gemini --------
    best_line = "Ingen data"
    if league_best:
        pts, pid, eid = league_best
        best_line = f"{player_names.get(pid, pid)} ({entry_name_map.get(eid,'?')}) — {pts} point"
    worst_line = "Ingen data"
    if league_worst:
        pts, pid, eid = league_worst
        worst_line = f"{player_names.get(pid, pid)} ({entry_name_map.get(eid,'?')}) — {pts} point"

    mover_line = "Ingen ændring"
    if biggest_mover:
        name, prev_r, now_r, delta = biggest_mover
        retning = "op" if delta > 0 else "ned"
        mover_line = f"{name}: {prev_r}. → {now_r}. plads ({abs(delta)} pladser {retning})"

    bench_line = None
    if biggest_bench_regret:
        diff, eid, bname, bpts, sname, spts = biggest_bench_regret
        bench_line = (
            f"🤥 {entry_name_map.get(eid, '?')} lod **{bname}** ({bpts} point) sidde på bænken "
            f"i stedet for **{sname}** ({spts} point) — {diff} point forærede væk"
        )

    context = f"""Gameweek {gw if gw > 0 else '(TEST — ingen rigtig gameweek endnu)'} i vores FPL Draft-liga "{league['league']['name']}".

STILLING:
{chr(10).join(standings_lines)}

Pointforskel mellem 1. og sidsteplads: {point_gap} point.

BEDSTE ENKELTSPILLER DENNE UGE: {best_line}
DÅRLIGSTE ENKELTSPILLER DENNE UGE (blandt startere): {worst_line}
STØRSTE PLADS-BEVÆGELSE: {mover_line}
STØRSTE "OUCH" PÅ BÆNKEN: {bench_line if bench_line else "Ingen — ingen bænkspiller ville reelt have gjort en forskel denne uge."}

TRANSAKTIONER SIDEN SIDST:
{chr(10).join(trans_lines) if trans_lines else "Ingen waivers eller trades siden sidst."}

SKADER/STATUS PÅ EJEDE SPILLERE (fra FPL's officielle data, kun nævn hvis relevant):
{chr(10).join(owned_injury_lines) if owned_injury_lines else "Ingen kendte skader på ejede spillere lige nu."}

TOP PRÆSTATIONER I HELE PREMIER LEAGUE DENNE UGE (uafhængigt af vores liga, til generel PL-flavor):
{chr(10).join(f"{name} — {pts} point" for pts, name in top_performers) if top_performers else "Ingen data."}

TOTTENHAM-REGEL: {tottenham_result if tottenham_result else "Tottenham vandt eller spillede ikke denne uge — ingen særlig omtale nødvendig."}
"""
    if gw == 0:
        context += "\n(Dette er 'Gameweek 0' — en forhåndsvisning før sæsonstart, så ligaen kan se hvordan beskeden ser ud. Alle tal er 0/tomme, det er forventet. Skriv opsummeringen som en sjov, uskyldig forsmag, ikke som en rigtig gameweek med resultater.)"

    manual_roast = os.environ.get("ROAST_TARGET", "").strip() or None
    # Uden manuel override: roast automatisk sidstepladsen + ejeren af ugens dårligste spiller —
    # ingen grund til at nogen skal bede om det, tallene peger jo allerede på hvem.
    roast_target = manual_roast or last_place_name
    summary_text = call_gemini(context, roast_target, forced=bool(manual_roast))

    if summary_text is None:
        # Begge Gemini-forsøg fejlede. Springer HELE posten over i stedet for at
        # sende en synligt degraderet besked til @everyone - state opdateres IKKE,
        # så næste planlagte kørsel (om få timer) prøver hele gameweeken igen fra
        # bunden, formentlig når Google's midlertidige problem er væk.
        print("Springer post over pga. Gemini-fejl - prøves igen ved næste kørsel.", file=sys.stderr)
        return

    next_deadline = find_next_deadline(bootstrap)
    deadline_line = "Ukendt — tjek draft.premierleague.com"
    if next_deadline:
        next_gw, deadline_ts = next_deadline
        deadline_line = f"GW{next_gw}: {format_deadline_da(deadline_ts)}"

    post_to_discord(gw, standings_lines, best_line, worst_line, mover_line, bench_line, trans_lines, summary_text, point_gap, deadline_line, test_mode)

    # picks-history er et rent arkiv (ikke en duplikat-spærre som last_posted_event),
    # så den gemmes altid, også under test - jo før en gameweeks picks bliver frosset,
    # jo mindre risiko for at data allerede har nået at drifte.
    save_picks_history(picks_history)

    # -------- persist state --------
    if gw > 0 and not test_mode:
        state["last_posted_event"] = gw
        state["last_ranks"] = current_ranks
        state["last_transaction_count"] = len(all_trans)
        save_state(state)
        print(f"GW{gw} postet og state gemt.")
    elif test_mode:
        print(f"Test-tilstand: besked postet, men state IKKE gemt (for ikke at blokere en ægte fremtidig post).")
    else:
        print("Test postet — league-state.json IKKE ændret (ingen rigtig gameweek).")


def call_gemini(context, roast_target=None, forced=False):
    roast_instruction = ""
    if roast_target and forced:
        roast_instruction = (
            f"\n\nSPECIELT FOR DENNE OMGANG: Dette er en testkørsel. Skriv HELE opsummeringen som "
            f"en 100% venskabelig roast af \"{roast_target}\" specifikt — ignorer hvem der reelt "
            f"performede bedst/dårligst i dataen, det er kun til sjov denne ene gang. Gå all-in på ham."
        )
    elif roast_target:
        roast_instruction = (
            f"\n\nSørg for at navngive og drille \"{roast_target}\" direkte et sted i teksten — "
            f"de ligger sidst i ligaen lige nu, så de skal kunne mærke det, men stadig kammeratligt."
        )

    prompt = (
        "Du skriver en opsummering af en gameweek i en lille Fantasy Premier League Draft-liga "
        "mellem venner, til at poste i Discord. Skriv på dansk.\n\n"
        "Formatering — det skal IKKE føles som én lang tekstmur:\n"
        "- Brug Discord-markdown: **fed** på navne og nøgletal.\n"
        "- Del op i korte afsnit/linjer med linjeskift mellem — aldrig én sammenhængende blok.\n"
        "- 180-200 ord i alt.\n"
        "- Gerne et par relevante emojis for tempo, ikke overdrevet.\n"
        "- Ingen overskrifter, ingen punktopstilling — bare korte, punchy afsnit.\n\n"
        "Indhold: Brug holdenes navne. Vær drillende og letsindig overfor dem der ligger dårligst "
        "eller performede dårligst denne uge — men hold det kammeratligt, ikke ondskabsfuldt.\n\n"
        "Fokusér den generelle PL-del på: skader og skade-opdateringer på spillere DE SELV ejer i "
        "ligaen (fra dataen herunder), samt hvem der reelt performede bedst i Premier League denne uge "
        "(kun spillerens egen præstation — ingen omtale af andre fantasy-hold, andre ligaer, eller "
        "hvor mange andre der ejer spilleren, det er irrelevant for os).\n"
        "Nævn IKKE transfers eller markedsrygter overhovedet.\n"
        "FAST REGEL: Hvis TOTTENHAM-REGEL i dataen viser at Spurs tabte eller spillede uafgjort denne "
        "uge, SKAL du altid inkludere et drillende sidespark til Tottenham et sted i teksten — det er "
        "en fast kammeratlig tradition i vores liga, uanset om nogen ejer Spurs-spillere eller ej. "
        "Hvis Spurs vandt eller ikke spillede, spring det over.\n"
        "Opfind ALDRIG noget som helst der ikke står i dataen herunder — ingen skader, ingen "
        "transfers/lejeaftaler/klubskifter, ingen kampresultater, ingen begrundelser for hvorfor en "
        "spiller scorede 0 point. Du kender IKKE hvorfor en spiller ikke leverede point (skade, "
        "rotation, bænk i den rigtige kamp, klubskifte - hvad som helst) medmindre det STÅR i dataen. "
        "Nævn kun de bare tal og navne hvis du ikke har en given forklaring - gæt eller opfind aldrig en. "
        "KONKRET EKSEMPEL på noget du IKKE må gøre: at skrive 'det hjælper ikke at han er lejet ud til "
        "en anden klub' eller lignende forklaring på en spillers 0 point, medmindre det ordret står i "
        "dataen — selvom du tror du kender til et klubskifte fra din egen viden, er det IKKE en del af "
        "denne ligas fiktive univers medmindre det er givet dig her. Brug ALDRIG egen baggrundsviden om "
        "spillere, kun det der eksplicit er skrevet i dataen."
        f"{roast_instruction}\n\n"
        f"Data:\n{context}"
    )
    return call_gemini_raw(prompt)


SUSPICIOUS_FABRICATION_WORDS = [
    "lejeaftale", "udlejet", "på leje", "på lån", "skiftet til", "solgt til",
    "købt af", "transfer til", "forlader klubben", "skiftede klub",
]


def contains_likely_fabrication(text):
    """
    Har konkret, gentagen erfaring med at Gemini opfinder plausible-lydende
    transfer/leje-detaljer om spillere ud fra egen (formentlig virkelighedsnær,
    men irrelevant i dette fiktive liga-univers) baggrundsviden, SELV efter
    eksplicitte instrukser om ikke at gøre det (bekræftet i 2 ud af 3 test-
    forsøg). Prompt-instrukser alene var ikke nok, så dette er et andet
    sikkerhedslag: fanger og forkaster teksten i stedet for at poste den.
    """
    lower = text.lower()
    return any(word in lower for word in SUSPICIOUS_FABRICATION_WORDS)


def call_gemini_raw(prompt):
    """
    Delt lav-niveau Gemini-kalder, genbrugt af både call_gemini() (gameweek-
    opsummeringer) og run_season_kickoff(). Prøver primær model, og ved fejl én
    backup-model (fx hvis Google melder "high demand" på den primære) - fejler
    BEGGE, returneres None, ikke en synlig fejltekst, så den kaldende kode kan
    vælge at springe hele posten over i stedet for at sende en synligt
    "degraderet" besked ud til @everyone.
    """
    api_key = os.environ["GEMINI_API_KEY"]
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")

    for model in ("gemini-3.5-flash", "gemini-3.6-flash"):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
                "User-Agent": "Mozilla/5.0 (compatible; drafthq-bot/1.0; +https://github.com/munksgaard91/drafthq)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if contains_likely_fabrication(text):
                print(f"Gemini-svar ({model}) indeholder formentlig opfundet transfer/leje-detalje, forkastet: {text[:200]}", file=sys.stderr)
                continue
            return text
        except Exception as e:
            print(f"Gemini-kald fejlede med model {model}:", e, file=sys.stderr)
            continue

    print("Begge Gemini-modeller fejlede eller leverede upålideligt indhold - springer denne post helt over.", file=sys.stderr)
    return None


def post_to_discord(gw, standings_lines, best_line, worst_line, mover_line, bench_line, trans_lines, summary_text, point_gap, deadline_line, test_mode=False):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    title = "⚽ Gameweek 0" if test_mode and gw == 0 else f"⚽ Gameweek {gw}"

    # Markdown-overskrifter (##) virker kun i description, IKKE i fields[].value (Discord-begrænsning) —
    # derfor bygges Stilling ind i description med tom linje over/under for luft, i stedet for som field.
    full_description = (
        f"{summary_text}\n\n"
        f"## Stilling\n"
        f"{chr(10).join(standings_lines) or '—'}\n"
    )

    embed = {
        "title": title,
        "description": full_description,
        "color": 2926465,
        "fields": [
            {"name": "🔥 Ugens bedste", "value": best_line, "inline": True},
            {"name": "🥶 Ugens værste", "value": worst_line, "inline": True},
            {"name": "📈 Størst bevægelse", "value": mover_line, "inline": False},
        ],
        "footer": {"text": f"Pointforskel fra første til sidstepladsen: {point_gap} point"},
    }
    if bench_line:
        embed["fields"].append({"name": "🤥 Dyreste bænk", "value": bench_line, "inline": False})
    if trans_lines:
        embed["fields"].append({"name": "Waivers & trades siden sidst", "value": "\n".join(trans_lines), "inline": False})

    embed["fields"].append({
        "name": "⏰ Husk at lave trades/waivers og sæt holdet",
        "value": f"Deadline: **{deadline_line}**",
        "inline": False,
    })

    body = json.dumps({
        "username": "Update Bot",
        "content": "@everyone",
        "embeds": [embed],
        "allowed_mentions": {"parse": ["everyone"]},
    }).encode("utf-8")

    if os.environ.get("DRY_RUN", "").lower() == "true":
        print("=== DRY_RUN: intet sendt til Discord, dette er hvad der VILLE være sendt ===")
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
        print("=== DRY_RUN slut ===")
        return

    req = urllib.request.Request(
        webhook,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; drafthq-bot/1.0; +https://github.com/munksgaard91/drafthq)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("Discord response:", resp.status)


if __name__ == "__main__":
    main()
