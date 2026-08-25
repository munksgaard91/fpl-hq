"""
Fælles hjælpefunktioner og konstanter, genbrugt af alle backend-scripts
(build_data_json.py, league_update.py, build_site_data.py, build_news.py).

Ingen af funktionerne her laver netværkskald ved import - kun fetch_json gør,
og kun når den kaldes eksplicit.
"""
import json
import urllib.request
from datetime import datetime, timezone

FPL_BASE = "https://fantasy.premierleague.com/api"
DRAFT_BASE = "https://draft.premierleague.com/api"
LEAGUE_ID = 668

TEAM_NAMES = {
    1: 'Arsenal', 2: 'Aston Villa', 3: 'Bournemouth', 4: 'Brentford', 5: 'Brighton',
    6: 'Chelsea', 7: 'Coventry', 8: 'Crystal Palace', 9: 'Everton', 10: 'Fulham',
    11: 'Hull', 12: 'Ipswich', 13: 'Leeds', 14: 'Liverpool', 15: 'Man City',
    16: 'Man Utd', 17: 'Newcastle', 18: "Nott'm Forest", 19: 'Spurs', 20: 'Sunderland',
}
POS_MAP = {'GKP': 'GK', 'DEF': 'DEF', 'MID': 'MID', 'FWD': 'FWD'}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fplhq-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_player_full_name(p):
    """
    Bruger FPL's eget 'web_name'-felt - det navn de rent faktisk viser overalt i
    virkeligheden (fx "Raya", "Thiago", "Bruno G."), IKKE det fulde juridiske navn
    (fx "David Raya Martín"). Beholder funktionsnavnet af hensyn til alle steder
    der allerede kalder den, men den bygger ikke længere et fuldt navn sammen.
    """
    return p.get("web_name") or (p.get("first_name", "") + " " + p.get("second_name", "")).strip()


def get_player_positions(bootstrap):
    et = {e["id"]: POS_MAP[e["singular_name_short"]] for e in bootstrap["element_types"]}
    return {p["id"]: et[p["element_type"]] for p in bootstrap["elements"]}


def get_player_names(bootstrap):
    return {p["id"]: get_player_full_name(p) for p in bootstrap["elements"]}


def get_player_clubs(bootstrap):
    return {p["id"]: TEAM_NAMES.get(p["team"], "?") for p in bootstrap["elements"]}


def get_league_entries(league_details):
    """
    Returnerer (entry_name_map, entry_id_by_league_id, real_entry_ids).
    FPL Draft bruger to forskellige ID'er pr. manager der IKKE altid matcher:
    league_entries[].entry_id (global konto-ID, bruges i /entry/{id}/... kald) og
    league_entries[].id (liga-scoped ID, det standings[].league_entry faktisk peger på).
    Vi bygger ét navne-opslag der matcher begge ID-typer som nøgler.
    """
    entry_name_map = {}
    entry_id_by_league_id = {}
    real_entry_ids = []
    for e in league_details["league_entries"]:
        entry_name_map[e["id"]] = e["entry_name"]
        entry_name_map[e["entry_id"]] = e["entry_name"]
        entry_id_by_league_id[e["id"]] = e["entry_id"]
        real_entry_ids.append(e["entry_id"])
    return entry_name_map, entry_id_by_league_id, real_entry_ids


def find_latest_finished_event(bootstrap):
    candidates = [e for e in bootstrap["events"] if e["finished"] and e["data_checked"]]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["id"])


def find_next_event(bootstrap):
    """
    Finder næste RELEVANTE deadline - dvs. den tidligste gameweek hvis deadline
    stadig ligger i fremtiden. Vigtigt: 'finished' bliver ikke True i FPL's data
    før ALLE kampe + bonuspoint er bekræftet, hvilket kan tage dage efter selve
    deadline er passeret - så vi kan IKKE bare filtrere på 'not finished'.
    """
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in bootstrap["events"]:
        deadline = datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00"))
        if deadline > now:
            upcoming.append(e)
    if not upcoming:
        return None
    return min(upcoming, key=lambda e: e["id"])


def get_team_fixture_difficulty(fixtures, num_gws=5):
    """
    Returnerer {team_id: [difficulty_gw1, difficulty_gw2, ...]} for de næste `num_gws`
    IKKE-spillede kampe pr. hold, ét tal pr. kamp (1=let, 5=svært).
    """
    upcoming = [f for f in fixtures if not f["finished"]]
    upcoming.sort(key=lambda f: f["event"] or 999)
    by_team = {}
    for f in upcoming:
        for side, opp_diff_key in (("team_h", "team_h_difficulty"), ("team_a", "team_a_difficulty")):
            tid = f[side]
            by_team.setdefault(tid, [])
            if len(by_team[tid]) < num_gws:
                by_team[tid].append(f[opp_diff_key])
    return by_team


def get_live_points_map(live):
    """
    FPL's /event/{gw}/live returnerer 'elements' som et dict (nøglet på spiller-ID
    som streng) MENS en gameweek er i gang, men som en LISTE (hvert element med sit
    eget 'id'-felt) når gameweeken er markeret helt færdig. Bekræftet begge dele
    direkte - denne normaliserer til {player_id: total_points} uanset hvilken form
    der kommer.
    """
    elements = live.get("elements", {})
    if isinstance(elements, dict):
        return {int(pid): d["stats"]["total_points"] for pid, d in elements.items()}
    return {item["id"]: item["stats"]["total_points"] for item in elements}
