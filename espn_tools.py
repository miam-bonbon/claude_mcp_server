"""
ESPN read path for your_own_fantasy_connector.

Adds two MCP tools:
  espn_matchup(league_id, team_id, week=0)  -> both full rosters for the matchup,
                                               lineup slots, injury status, ESPN projections
  espn_roster(league_id, team_id, week=0)   -> one team's roster, same detail

Private leagues need two cookies, set as Render environment variables:
  ESPN_S2   (long string, cookie "espn_s2")
  ESPN_SWID (looks like {XXXXXXXX-XXXX-...}, cookie "SWID", keep the braces)

Wiring, in fantasy_mcp_http.py after `mcp` is created:
    from espn_tools import register_espn_tools
    register_espn_tools(mcp)
"""

import os
import time
import requests

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
DEFAULT_SEASON = int(os.environ.get("ESPN_SEASON", "2026"))

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
    params = [("view", "mTeam"), ("view", "mRoster"), ("view", "mMatchup"), ("view", "mSettings")]
    if week:
        params.append(("scoringPeriodId", str(week)))
    cookies = {}
    if os.environ.get("ESPN_S2") and os.environ.get("ESPN_SWID"):
        cookies = {"espn_s2": os.environ["ESPN_S2"], "SWID": os.environ["ESPN_SWID"]}
    r = requests.get(ESPN_BASE.format(season=season, league_id=league_id),
                     params=params, cookies=cookies, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code == 401:
        raise RuntimeError("ESPN 401: private league. Set ESPN_S2 and ESPN_SWID on Render.")
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def _team_name(t: dict) -> str:
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip() or f"Team {t['id']}"


def _record(t: dict) -> str:
    o = (t.get("record") or {}).get("overall") or {}
    return f"{o.get('wins', 0)}-{o.get('losses', 0)}" + (f"-{o['ties']}" if o.get("ties") else "")


def _projection(player: dict, week: int) -> float | None:
    for s in player.get("stats") or []:
        if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == week:
            return round(s.get("appliedTotal", 0.0), 1)
    return None


def _actual(player: dict, week: int) -> float | None:
    for s in player.get("stats") or []:
        if s.get("statSourceId") == 0 and s.get("scoringPeriodId") == week:
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
            "proj": _projection(p, week),
            "pts": _actual(p, week),
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
    def espn_roster(league_id: str, team_id: int, week: int = 0, season: int = 0) -> str:
        """One ESPN team's roster: lineup slot, position, NFL team, injury status,
        ESPN projection and live points for the week. week=0 means current week.
        Private leagues need ESPN_S2 and ESPN_SWID set in the environment."""
        data = _fetch(league_id, season or DEFAULT_SEASON, week)
        wk = week or data.get("scoringPeriodId")
        team = next((t for t in data.get("teams", []) if t["id"] == team_id), None)
        if not team:
            return f"No team {team_id} in league {league_id}."
        return f"Week {wk}\n" + _render_team(team, wk)

    @mcp.tool()
    def espn_matchup(league_id: str, team_id: int, week: int = 0, season: int = 0) -> str:
        """Head-to-head view for an ESPN league: finds team_id's opponent for the
        week and returns BOTH full rosters with slots, injuries, projections and
        live points. week=0 means current week. This is the weekly start/sit tool
        for ESPN leagues (MORPHEUS: league 167814011, team 4)."""
        data = _fetch(league_id, season or DEFAULT_SEASON, week)
        wk = week or data.get("scoringPeriodId")
        teams = {t["id"]: t for t in data.get("teams", [])}
        opp_id = None
        for m in data.get("schedule", []):
            if m.get("matchupPeriodId") != wk:
                continue
            h, a = (m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")
            if h == team_id:
                opp_id = a
            elif a == team_id:
                opp_id = h
        out = [f"Week {wk}", _render_team(teams[team_id], wk)]
        if opp_id is not None and opp_id in teams:
            out.append(_render_team(teams[opp_id], wk))
        else:
            out.append("Opponent not found for this week (bye or schedule not loaded).")
        return "\n\n".join(out)
