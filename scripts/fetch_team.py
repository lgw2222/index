"""
Pulls Knee-ver Over the Hill from ESPN Fantasy (private league, using your cookies),
writes data/team.json for the website, and sends phone alerts through ntfy.sh
when a player's points jump.

Works with no setup if the league is viewable to the public on ESPN.
Optional repo secrets ESPN_S2 / SWID let it read a private league instead.
Phone alerts go to the ntfy topic below (subscribe to it in the ntfy app).
"""
import json, os, sys, pathlib, datetime, urllib.request, urllib.error

LEAGUE_ID, TEAM_ID, SEASON = 332193, 1, 2026
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "kneever-garrott-332193-alerts"
MIN_GAIN = float(os.environ.get("MIN_GAIN", "4"))   # alert when a player gains this many points between checks
MIN_LOSS = float(os.environ.get("MIN_LOSS", "2"))   # alert when a player loses this many (INT, fumble)

BASE = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON}"
        f"/segments/0/leagues/{LEAGUE_ID}")
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "team.json"

PRO_TEAM = {1:'ATL',2:'BUF',3:'CHI',4:'CIN',5:'CLE',6:'DAL',7:'DEN',8:'DET',9:'GB',10:'TEN',11:'IND',12:'KC',
            13:'LV',14:'LAR',15:'MIA',16:'MIN',17:'NE',18:'NO',19:'NYG',20:'NYJ',21:'PHI',22:'ARI',23:'PIT',
            24:'LAC',25:'SF',26:'SEA',27:'TB',28:'WSH',29:'CAR',30:'JAX',33:'BAL',34:'HOU'}
POS = {1:'QB',2:'RB',3:'WR',4:'TE',5:'K',16:'D/ST'}
SLOT = {0:'QB',2:'RB',4:'WR',6:'TE',16:'D/ST',17:'K',20:'Bench',21:'IR',23:'FLEX'}
SLOT_ORDER = ['QB','RB','WR','TE','FLEX','D/ST','K','Bench','IR']


def get(url):
    s2, swid = os.environ.get("ESPN_S2", ""), os.environ.get("SWID", "")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if s2 and swid:
        headers["Cookie"] = f"espn_s2={s2}; SWID={swid}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise PermissionError("ESPN blocked access. In ESPN: LM Tools > League Settings > Basic Settings > "
                                  "turn on 'Make League Viewable to Public'.")
        raise


def team_name(t):
    return (t.get("name") or f"{t.get('location','')} {t.get('nickname','')}").strip() if t else ""


def player_points(player, period):
    actual = proj = None
    for st in player.get("stats", []):
        if st.get("scoringPeriodId") != period:
            continue
        if st.get("statSourceId") == 0:
            actual = st.get("appliedTotal", actual)
        elif st.get("statSourceId") == 1:
            proj = st.get("appliedTotal", proj)
    return actual, proj


def ntfy(title, body):
    topic = NTFY_TOPIC.strip()
    if not topic:
        return
    headers = {"Title": title.encode("utf-8"), "Tags": "football"}
    click = os.environ.get("SITE_URL", "").strip()
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("ntfy failed:", e)


def main():
    status = get(f"{BASE}?view=mStatus")
    period = status.get("scoringPeriodId") or status["status"]["latestScoringPeriod"]
    d = get(f"{BASE}?view=mTeam&view=mRoster&view=mMatchupScore&view=mStatus&scoringPeriodId={period}")

    teams = {t["id"]: t for t in d.get("teams", [])}
    me = teams.get(TEAM_ID)
    if not me:
        sys.exit(f"Team {TEAM_ID} not found in league {LEAGUE_ID}.")

    roster = []
    for en in me.get("roster", {}).get("entries", []):
        pl = en["playerPoolEntry"]["player"]
        pos = POS.get(pl.get("defaultPositionId"), "WR")
        team = PRO_TEAM.get(pl.get("proTeamId"), "")
        name = pl.get("fullName", "")
        if pos == "D/ST":
            name = name.replace(" D/ST", "").strip()
        actual, proj = player_points(pl, period)
        if actual is None:
            actual = en["playerPoolEntry"].get("appliedStatTotal")
        roster.append({
            "slot": SLOT.get(en.get("lineupSlotId"), "Bench"), "name": name, "team": team, "pos": pos,
            "espnId": pl.get("id"), "pts": round(actual, 2) if actual is not None else None,
            "proj": round(proj, 2) if proj is not None else None,
            "injury": pl.get("injuryStatus") or "ACTIVE",
        })
    roster.sort(key=lambda p: SLOT_ORDER.index(p["slot"]) if p["slot"] in SLOT_ORDER else 99)

    matchup = None
    cur = d.get("status", {}).get("currentMatchupPeriod")
    for m in d.get("schedule", []):
        if m.get("matchupPeriodId") != cur:
            continue
        h, a = m.get("home") or {}, m.get("away") or {}
        if TEAM_ID not in (h.get("teamId"), a.get("teamId")):
            continue
        mine, opp = (h, a) if h.get("teamId") == TEAM_ID else (a, h)
        score = lambda s: s.get("totalPointsLive", s.get("totalPoints", 0)) or 0
        matchup = {"me": round(score(mine), 2), "opp": round(score(opp), 2),
                   "meName": team_name(me), "oppName": team_name(teams.get(opp.get("teamId")))}
        break

    rec = me.get("record", {}).get("overall", {})
    data = {
        "season": SEASON, "week": period,
        "team": {"name": team_name(me), "record": f"{rec.get('wins',0)}-{rec.get('losses',0)}-{rec.get('ties',0)}"},
        "matchup": matchup, "roster": roster,
    }

    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    prev_cmp = {k: v for k, v in prev.items() if k not in ("updated", "error")}
    if "error" in prev:
        prev_cmp = None
    if prev_cmp == data:
        print("No changes.")
        return

    # Phone alerts for big point swings since the last check (same week only)
    if prev.get("week") == period:
        old = {p.get("espnId"): p.get("pts") or 0 for p in prev.get("roster", [])}
        lines = []
        for p in roster:
            if p["espnId"] not in old or p["pts"] is None:
                continue
            delta = p["pts"] - old[p["espnId"]]
            if delta >= MIN_GAIN or delta <= -MIN_LOSS:
                label = p["name"] + (" D/ST" if p["pos"] == "D/ST" else "")
                bench = "" if p["slot"] not in ("Bench", "IR") else " (bench)"
                lines.append(f"{label}{bench} {delta:+.1f}, now {p['pts']:.1f}")
        if lines:
            title = (f"{matchup['meName']} {matchup['me']:.1f} - {matchup['opp']:.1f}" if matchup else "Fantasy update")
            ntfy(title, "\n".join(lines))
            print("Alert sent:", lines)

    data["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    print("Wrote", OUT)


def write_error(msg):
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    if prev.get("error") == msg:
        return
    prev["error"] = msg
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(prev, indent=1))
    print(msg)


if __name__ == "__main__":
    try:
        main()
    except PermissionError as e:
        write_error(str(e))
