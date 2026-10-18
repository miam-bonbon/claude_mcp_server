"""
ESPN read path for the fantasy MCP server (MORPHEUS lives on ESPN).

Tools:
  espn_matchup(league_id, team_id, week=0) -> my roster AND this week's opponent,
                                              lineup slots, injuries, ESPN projections, live points
  espn_roster(league_id, team_id, week=0)  -> one team, same detail

Env vars (Render dashboard):
  ESPN_S2     cookie "espn_s2"   (required for private leagues)
  ESPN_SWID   cookie "SWID"      (keep the curly braces)
  ESPN_SEASON optional, default 2026

Stdlib only (urllib), same as the rest of the server: no new requirements.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ESPN_BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
             "seasons/{season}/segments/0/leagues/{league_id}")
DEFAULT_SEASON = int(os.environ.get("ESPN_SEASON", "2026"))
DEFAULT_ESPN_LEAGUE = os.environ.get("ESPN_LEAGUE_ID", "167814011")
DEFAULT_ESPN_TEAM = int(os.environ.get("ESPN_TEAM_ID", "4"))

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
SLOT = {0: "QB", 2: "RB", 3: "RB/WR", 4: "WR", 6: "TE", 16: "D/ST", 17: "K",
        20: "BENCH", 21: "IR", 23: "FLEX"}
PRO = {0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
       8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
       16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
       24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX",
       33: "BAL", 34: "HOU"}
SLOT_ORDER = ["QB", "RB", "RB/WR", "WR", "TE", "FLEX", "D/ST", "K", "BENCH", "IR"]

_cache: dict = {}
CACHE_SECONDS = 120


def _fetch(league_id: str, season: int, week: int) -> dict:
    key = (league_id, season, week)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    params = [("view", "mTeam"), ("view", "mRoster"),
              ("view", "mMatchup"), ("view", "mSettings")]
    if week:
        params.append(("scoringPeriodId", str(week)))
    url = ESPN_BASE.format(season=season, league_id=league_id) + "?" + \
        urllib.parse.urlencode(params)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    s2, swid = os.environ.get("ESPN_S2", ""), os.environ.get("ESPN_SWID", "")
    if s2 and swid:
        headers["Cookie"] = f"espn_s2={s2}; SWID={swid}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("ESPN 401: private league. Set ESPN_S2 and ESPN_SWID "
                               "on Render (cookies from a logged-in espn.com tab).")
        raise
    _cache[key] = (time.time(), data)
    return data


def _team_name(t: dict) -> str:
    return (t.get("name")
            or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
            or f"Team {t['id']}")


def _record(t: dict) -> str:
    o = (t.get("record") or {}).get("overall") or {}
    rec = f"{o.get('wins', 0)}-{o.get('losses', 0)}"
    return rec + (f"-{o['ties']}" if o.get("ties") else "")


def _stat(player: dict, week: int, source: int):
    """source 1 = ESPN projection, 0 = actual points."""
    for s in player.get("stats") or []:
        if s.get("statSourceId") == source and s.get("scoringPeriodId") == week:
            return round(s.get("appliedTotal", 0.0), 1)
    return None


def _render_team(t: dict, week: int) -> str:
    rows = []
    for e in (t.get("roster") or {}).get("entries", []):
        p = e["playerPoolEntry"]["player"]
        rows.append({
            "slot": SLOT.get(e.get("lineupSlotId"), str(e.get("lineupSlotId"))),
            "name": p.get("fullName"),
            "pos": POS.get(p.get("defaultPositionId"), "?"),
            "team": PRO.get(p.get("proTeamId"), str(p.get("proTeamId"))),
            "inj": p.get("injuryStatus") or ("OUT" if p.get("injured") else "ACTIVE"),
            "proj": _stat(p, week, 1),
            "pts": _stat(p, week, 0),
        })
    rows.sort(key=lambda r: SLOT_ORDER.index(r["slot"]) if r["slot"] in SLOT_ORDER else 99)
    lines = [f"## {_team_name(t)} (team {t['id']}, {_record(t)})"]
    for r in rows:
        inj = "" if r["inj"] in ("ACTIVE", "NORMAL") else f"  [{r['inj']}]"
        proj = f"  proj {r['proj']}" if r["proj"] is not None else ""
        pts = f"  pts {r['pts']}" if r["pts"] is not None else ""
        lines.append(f"{r['slot']:<6} {r['name']} ({r['pos']}, {r['team']}){inj}{proj}{pts}")
    return "\n".join(lines)


def register_espn_tools(mcp):

    @mcp.tool()
    def espn_roster(team_id: int = 0, league_id: str = "", week: int = 0,
                    season: int = 0) -> str:
        """One ESPN team's roster: lineup slot, position, NFL team, injury status,
        ESPN projection and live points. Defaults: MORPHEUS league 167814011,
        my team 4, current week. Pass team_id for any other team."""
        lid = league_id or DEFAULT_ESPN_LEAGUE
        data = _fetch(lid, season or DEFAULT_SEASON, week)
        wk = week or data.get("scoringPeriodId")
        tid = team_id or DEFAULT_ESPN_TEAM
        team = next((t for t in data.get("teams", []) if t["id"] == tid), None)
        if not team:
            return f"No team {tid} in ESPN league {lid}."
        return f"ESPN league {lid} · week {wk}\n" + _render_team(team, wk)

    @mcp.tool()
    def espn_matchup(team_id: int = 0, league_id: str = "", week: int = 0,
                     season: int = 0) -> str:
        """THE WEEKLY TOOL FOR ESPN LEAGUES. Finds this week's opponent and returns
        BOTH full rosters with slots, injuries, ESPN projections and live points.
        Defaults: MORPHEUS league 167814011, my team 4, current week."""
        lid = league_id or DEFAULT_ESPN_LEAGUE
        data = _fetch(lid, season or DEFAULT_SEASON, week)
        wk = week or data.get("scoringPeriodId")
        tid = team_id or DEFAULT_ESPN_TEAM
        teams = {t["id"]: t for t in data.get("teams", [])}
        if tid not in teams:
            return f"No team {tid} in ESPN league {lid}."
        opp = None
        for m in data.get("schedule", []):
            if m.get("matchupPeriodId") != wk:
                continue
            h = (m.get("home") or {}).get("teamId")
            a = (m.get("away") or {}).get("teamId")
            if h == tid:
                opp = a
            elif a == tid:
                opp = h
        out = [f"ESPN league {lid} · week {wk}", _render_team(teams[tid], wk)]
        out.append(_render_team(teams[opp], wk) if opp in teams
                   else "Opponent not found for this week (bye or schedule not loaded).")
        return "\n\n".join(out)
