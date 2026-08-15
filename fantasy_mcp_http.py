#!/usr/bin/env python3
"""
Fantasy MCP server — remote (Streamable HTTP) build for Render.com.

Multi-league build. Every tool takes an optional league_id; omit it and you get
SLEEPER_LEAGUE_ID from the environment. FantasyPros ranking parameters are
DERIVED from the Sleeper league object (redraft vs dynasty, PPR vs half vs
standard, superflex vs 1QB) rather than hand-passed, so pointing a tool at a
different league automatically pulls the right consensus set.

Env vars (set in Render dashboard, NOT in code):
  FANTASYPROS_API_KEY   required
  MCP_SECRET            required — long random string, becomes the URL path
  MCP_BEARER            optional — additionally require this bearer token
  SLEEPER_LEAGUE_ID     default league when league_id is omitted
  SLEEPER_USERNAME      optional — your Sleeper username or user_id. Lets
                        my_roster() find you in ANY league by owner_id.
  MY_ROSTER_ID          fallback "me" for the default league only
  PORT                  supplied by Render
"""

import json
import logging
import os
import re
import secrets
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

# FastMCP defaults host="127.0.0.1", and when transport_security is None and
# host is localhost it AUTO-ENABLES DNS rebinding protection with a
# localhost-only allowlist. Behind Render the Host header is the public
# hostname, so every request 421s ("Invalid Host header") inside the SDK
# middleware — before any handler or error logging runs. Pass the real
# hostname so protection stays on but accepts legitimate traffic.
_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
_extra = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
_hosts = [h for h in ([_host] + _extra) if h]

if _hosts:
    _security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_hosts + [f"{h}:*" for h in _hosts],
        allowed_origins=[f"https://{h}" for h in _hosts],
    )
else:
    # No hostname available — fail open rather than 421 everything, but say so.
    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP("fantasy", stateless_http=True, host="0.0.0.0",
              transport_security=_security)

FP_KEY = os.environ.get("FANTASYPROS_API_KEY", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
MCP_BEARER = os.environ.get("MCP_BEARER", "")
FP_BASE = "https://api.fantasypros.com/public/v2/json"
SLEEPER_BASE = "https://api.sleeper.app/v1"
DEFAULT_LEAGUE = os.environ.get("SLEEPER_LEAGUE_ID", "1312003800977899520")
MY_USER = os.environ.get("SLEEPER_USERNAME", "").strip()
MY_ROSTER = int(os.environ.get("MY_ROSTER_ID", "5"))

CACHE_DIR = Path(os.environ.get("DYNASTY_CACHE", "/tmp/fantasy_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTL = {"players": 86400, "rankings": 3600, "injuries": 900,
       "roster": 120, "league": 3600, "draft": 30}

# In-process memo so a warm instance doesn't re-parse the 14MB map every call.
_MEMO: dict = {}


# ----------------------------------------------------------------- http + cache

def _cache_path(key: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:150] + ".json")


def _fetch(url: str, headers: dict | None = None, cache_key: str = "", ttl: int = 300):
    if cache_key:
        p = _cache_path(cache_key)
        if p.exists() and (time.time() - p.stat().st_mtime) < ttl:
            return json.loads(p.read_text())
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    if cache_key:
        _cache_path(cache_key).write_text(json.dumps(data))
    return data


def _fp(path: str, params: dict, cache_key: str, ttl: int):
    if not FP_KEY:
        raise RuntimeError("FANTASYPROS_API_KEY not set in Render environment.")
    url = f"{FP_BASE}/{path}?{urllib.parse.urlencode(params)}"
    return _fetch(url, headers={"x-api-key": FP_KEY}, cache_key=cache_key, ttl=ttl)


# ----------------------------------------------------------------- name matching

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", "").replace("-", " ")
    return "".join(p for p in re.split(r"\s+", s) if p and p not in SUFFIXES)


# ----------------------------------------------------------------- league layer

def _lid(league_id: str = "") -> str:
    """Resolve an incoming league_id argument to a concrete league."""
    return (league_id or "").strip() or DEFAULT_LEAGUE


def league_meta(league_id: str = "") -> dict:
    """The Sleeper league object: scoring_settings, roster_positions, settings."""
    lid = _lid(league_id)
    return _fetch(f"{SLEEPER_BASE}/league/{lid}",
                  cache_key=f"league_{lid}", ttl=TTL["league"])


# Sleeper settings.type -> human label
LEAGUE_TYPE = {0: "redraft", 1: "keeper", 2: "dynasty"}


def fp_profile(league_id: str = "") -> dict:
    """Derive the correct FantasyPros ranking parameters from the league itself.

    Returns type / scoring / position plus notes describing anything in this
    league's scoring that FP's consensus does NOT model. The notes matter: FP
    publishes a finite menu of ranking sets, so for an unusual league we get the
    CLOSEST set, not an exact one, and the caller should know the gap.
    """
    lg = league_meta(league_id)
    sc = lg.get("scoring_settings") or {}
    slots = lg.get("roster_positions") or []
    settings = lg.get("settings") or {}

    rec = float(sc.get("rec", 0) or 0)
    scoring = "PPR" if rec >= 1 else ("HALF" if rec >= 0.5 else "STD")

    superflex = ("SUPER_FLEX" in slots) or (slots.count("QB") > 1)

    kind = LEAGUE_TYPE.get(settings.get("type", 0), "redraft")
    if kind == "dynasty":
        rtype = "DYNASTY"
    elif lg.get("status") in ("in_season", "post_season", "complete"):
        rtype = "ROS"          # season underway: rest-of-season, not draft board
    else:
        rtype = "DRAFT"

    position = "OP" if superflex else "ALL"

    notes = []
    if sc.get("bonus_rec_te"):
        notes.append(f"TE premium +{sc['bonus_rec_te']}/rec — FP has no TE-prem set")
    if float(sc.get("pass_td", 4) or 4) != 4:
        notes.append(f"{sc.get('pass_td')}-pt pass TD (FP assumes 4)")
    for k in ("bonus_rush_yd_100", "bonus_rec_yd_100", "bonus_pass_yd_300"):
        if sc.get(k):
            notes.append(f"{k} bonus active")
    if sc.get("rush_fd") or sc.get("rec_fd"):
        notes.append("first-down bonus active — FP does not model it")
    fum_net = float(sc.get("fum", 0) or 0) + float(sc.get("fum_lost", 0) or 0)
    if fum_net != -2:
        notes.append(f"lost fumble nets {fum_net:+g} (standard is -2)")

    return {"league_id": _lid(league_id), "name": lg.get("name", "?"),
            "kind": kind, "type": rtype, "scoring": scoring,
            "position": position, "superflex": superflex,
            "season": int(lg.get("season") or 2026), "notes": notes}


def _profile_header(prof: dict) -> str:
    sf = "superflex" if prof["superflex"] else "1QB"
    line = (f"[{prof['name']} · {prof['league_id']} → "
            f"{prof['type']} / {prof['scoring']} / {sf} / {prof['season']}]")
    if prof["notes"]:
        line += "\n  ! " + "\n  ! ".join(prof["notes"])
    return line


# ----------------------------------------------------------------- sleeper data

# K and DEF included: redraft leagues roster them, and leaving them out makes
# every kicker and defense show up as "unresolved sleeper id".
FANTASY_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}
POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DEF": 5}


def slim_players() -> dict:
    """Slim map, memoized in-process. Cold start re-downloads 14MB (~10-20s)."""
    now = time.time()
    if _MEMO.get("slim") and now - _MEMO.get("slim_at", 0) < TTL["players"]:
        return _MEMO["slim"]
    pm = _fetch(f"{SLEEPER_BASE}/players/nfl",
                cache_key="sleeper_players", ttl=TTL["players"])
    out = {}
    for pid, p in pm.items():
        if p.get("position") not in FANTASY_POS:
            continue
        nm = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        if not nm:
            continue
        out[pid] = {"name": nm, "pos": p["position"], "team": p.get("team") or "FA",
                    "age": p.get("age"), "exp": p.get("years_exp"),
                    "inj": p.get("injury_status") or "", "body": p.get("injury_body_part") or "",
                    "rank": p.get("search_rank") if p.get("search_rank") is not None else 999999}
    _MEMO["slim"], _MEMO["slim_at"] = out, now
    return out


def rosters_raw(league_id: str = "") -> list:
    lid = _lid(league_id)
    return _fetch(f"{SLEEPER_BASE}/league/{lid}/rosters",
                  cache_key=f"rosters_{lid}", ttl=TTL["roster"])


def league_users(league_id: str = "") -> dict:
    lid = _lid(league_id)
    users = _fetch(f"{SLEEPER_BASE}/league/{lid}/users",
                   cache_key=f"users_{lid}", ttl=3600)
    return {u["user_id"]: (u.get("metadata", {}).get("team_name") or u.get("display_name"))
            for u in users}


def my_user_id() -> str:
    """Resolve SLEEPER_USERNAME (username OR raw user_id) to a user_id."""
    if not MY_USER:
        return ""
    if MY_USER.isdigit():
        return MY_USER
    try:
        u = _fetch(f"{SLEEPER_BASE}/user/{MY_USER}",
                   cache_key=f"user_{MY_USER}", ttl=86400)
        return u.get("user_id") or ""
    except Exception:
        return ""


def resolve_me(league_id: str = "", rosters: list | None = None) -> int | None:
    """Which roster_id is mine in THIS league.

    Owner lookup first so it works in any league I join; MY_ROSTER_ID is only a
    fallback for the default league, where it is known to be correct.
    """
    lid = _lid(league_id)
    uid = my_user_id()
    if uid:
        for r in (rosters if rosters is not None else rosters_raw(lid)):
            if r.get("owner_id") == uid:
                return r["roster_id"]
    if lid == DEFAULT_LEAGUE:
        return MY_ROSTER
    return None


def _fmt(p: dict, slot: str = "") -> str:
    inj = p["inj"] + (f"({p['body']})" if p["body"] else "")
    line = f"{p['pos']:<3} {p['name']:<22} {p['team']:<4} {str(p['age'] or '?'):<4}"
    if slot:
        line += f" [{slot}]"
    if inj:
        line += f" {inj}"
    return line.rstrip()


# ----------------------------------------------------------------- tools

@mcp.tool()
def league_settings(league_id: str = "") -> str:
    """Full setup for any Sleeper league: format, roster slots, every scoring
    rule, playoffs, waivers, trade deadline — plus the FantasyPros ranking
    profile derived from it. Run this FIRST on an unfamiliar league."""
    lid = _lid(league_id)
    lg = league_meta(lid)
    s = lg.get("settings") or {}
    sc = lg.get("scoring_settings") or {}
    slots = lg.get("roster_positions") or []
    prof = fp_profile(lid)

    counts: dict = {}
    for slot in slots:
        counts[slot] = counts.get(slot, 0) + 1
    starters = [f"{v}x{k}" for k, v in counts.items() if k != "BN"]

    out = [f"{lg.get('name','?')}  ({lid})",
           f"{lg.get('season')} {lg.get('season_type')} · {lg.get('status')} · "
           f"{prof['kind'].upper()} · {lg.get('total_rosters')} teams",
           "",
           "ROSTER: " + " ".join(starters) + f"  |  bench {counts.get('BN',0)}"
           + (f" | IR {counts.get('IR',0)}" if counts.get("IR") else "")
           + (f" | taxi {s.get('taxi_slots')}" if s.get("taxi_slots") else ""),
           f"FP PROFILE: {prof['type']} / {prof['scoring']} / "
           f"{'superflex' if prof['superflex'] else '1QB'} (position={prof['position']})"]
    if prof["notes"]:
        out += ["  ! " + n for n in prof["notes"]]

    waiver = {0: "rolling waivers", 1: "reverse standings", 2: "FAAB"}.get(
        s.get("waiver_type"), f"type {s.get('waiver_type')}")
    out += ["",
            f"LEAGUE: {waiver}"
            + (f" ${s.get('waiver_budget')}" if s.get("waiver_budget") else "")
            + f" · waiver day {s.get('waiver_day_of_week','?')}"
            + f" · trade deadline wk {s.get('trade_deadline','?')}",
            f"PLAYOFFS: start wk {s.get('playoff_week_start','?')} · "
            f"{s.get('playoff_teams','?')} teams · "
            f"{'2-week' if s.get('playoff_round_type') else '1-week'} rounds",
            f"MISC: {s.get('num_teams', lg.get('total_rosters'))} teams · "
            f"best-ball {'on' if s.get('best_ball') else 'off'} · "
            f"draft_id {lg.get('draft_id')}"]

    groups = {"PASSING": ("pass",), "RUSHING": ("rush",),
              "RECEIVING": ("rec", "bonus_rec"), "KICKING": ("fgm", "fga", "xp"),
              "DEFENSE": ("def", "sack", "int", "safe", "blk", "pts_allow", "yds_allow",
                          "ff", "tkl", "idp"),
              "MISC / FUMBLES": ("fum", "st_", "pr", "kr", "bonus", "2pt", "misc")}
    seen = set()
    out += ["", "SCORING"]
    for label, prefixes in groups.items():
        rows = sorted(k for k in sc
                      if k not in seen and any(k.startswith(p) for p in prefixes))
        if not rows:
            continue
        seen.update(rows)
        out.append(f"  {label}")
        out += [f"    {k:<22}{sc[k]:>8g}" for k in rows]
    rest = sorted(k for k in sc if k not in seen)
    if rest:
        out.append("  OTHER")
        out += [f"    {k:<22}{sc[k]:>8g}" for k in rest]

    fum = float(sc.get("fum", 0) or 0)
    fum_lost = float(sc.get("fum_lost", 0) or 0)
    out += ["",
            f"Fumble math: Sleeper stacks fum + fum_lost. Fumble recovered by own "
            f"team = {fum:+g}. Fumble lost = {fum:+g} and {fum_lost:+g} "
            f"= {fum + fum_lost:+g} net."]
    return "\n".join(out)


@mcp.tool()
def my_roster(league_id: str = "") -> str:
    """My team in the given league, resolved to names, grouped by position,
    with IR/taxi slots and injury flags. Live from Sleeper."""
    lid = _lid(league_id)
    rid = resolve_me(lid)
    if rid is None:
        return (f"Cannot tell which roster is mine in league {lid}. "
                "Set SLEEPER_USERNAME in the environment, or call "
                "team_roster(roster_id=N, league_id=...) directly.")
    return team_roster(rid, lid)


@mcp.tool()
def team_roster(roster_id: int, league_id: str = "") -> str:
    """Resolved roster for any team in any league, by roster_id."""
    lid = _lid(league_id)
    sp, users = slim_players(), league_users(lid)
    rosters = rosters_raw(lid)
    me = resolve_me(lid, rosters)
    for r in rosters:
        if r["roster_id"] != roster_id:
            continue
        ir, taxi = set(r.get("reserve") or []), set(r.get("taxi") or [])
        players = r.get("players") or []
        typed, stray = [], []
        for pid in players:
            p = sp.get(pid)
            if not p:
                stray.append(f"??? unresolved id {pid}")
            else:
                typed.append((p, "IR" if pid in ir else ("TAXI" if pid in taxi else "")))
        typed.sort(key=lambda x: (POS_ORDER.get(x[0]["pos"], 9), x[0]["rank"]))
        s = r.get("settings", {})
        head = (f"Roster {roster_id} — {users.get(r.get('owner_id'), '?')}"
                f"{'  <-- me' if roster_id == me else ''}\n"
                f"{s.get('wins',0)}-{s.get('losses',0)} · FAAB used {s.get('waiver_budget_used',0)}\n"
                f"{len(players)} total | {len(ir)} IR | {len(taxi)} taxi | "
                f"{len(players)-len(ir)-len(taxi)} active\n")
        return head + "\n" + "\n".join(_fmt(p, sl) for p, sl in typed) + \
            ("\n" + "\n".join(stray) if stray else "")
    return f"No roster_id {roster_id} in league {lid}."


@mcp.tool()
def league_overview(league_id: str = "") -> str:
    """One line per team: manager, record, roster size, and positional counts.
    Spots positional scarcity across the league."""
    lid = _lid(league_id)
    sp, users = slim_players(), league_users(lid)
    rosters = sorted(rosters_raw(lid), key=lambda x: x["roster_id"])
    me = resolve_me(lid, rosters)
    out = []
    for r in rosters:
        pl = r.get("players") or []
        n = {k: 0 for k in ("QB", "RB", "WR", "TE")}
        for x in pl:
            pos = sp.get(x, {}).get("pos")
            if pos in n:
                n[pos] += 1
        s = r.get("settings", {})
        mark = " <-- me" if r["roster_id"] == me else ""
        out.append(f"{r['roster_id']:>2} {str(users.get(r.get('owner_id'),'?'))[:24]:<25}"
                   f"{s.get('wins',0)}-{s.get('losses',0)}  {len(pl):>2}p "
                   f"{n['QB']:>2}QB {n['RB']:>2}RB {n['WR']:>2}WR {n['TE']:>2}TE{mark}")
    return "\n".join(out)


@mcp.tool()
def free_agents(position: str = "ALL", limit: int = 40, league_id: str = "") -> str:
    """Unrostered players in a league, best-first by Sleeper search_rank.
    position: ALL, QB, RB, WR, TE, K, DEF."""
    sp = slim_players()
    taken = set()
    for r in rosters_raw(_lid(league_id)):
        taken.update(r.get("players") or [])
    pool = sorted((p["rank"], pid, p) for pid, p in sp.items()
                  if pid not in taken and p["team"] != "FA"
                  and (position == "ALL" or p["pos"] == position))
    return "\n".join(_fmt(p) for _, _, p in pool[:limit]) or "None found."


@mcp.tool()
def trending(kind: str = "add", hours: int = 24, limit: int = 25) -> str:
    """League-wide most-added or most-dropped across all of Sleeper.
    kind: add | drop."""
    sp = slim_players()
    data = _fetch(f"{SLEEPER_BASE}/players/nfl/trending/{kind}"
                  f"?lookback_hours={hours}&limit={limit}",
                  cache_key=f"trend_{kind}_{hours}_{limit}", ttl=900)
    return "\n".join(f"{e['count']:>7,}  {_fmt(sp[e['player_id']])}"
                     for e in data if e["player_id"] in sp) or "None."


@mcp.tool()
def fp_rankings(league_id: str = "", position: str = "", ranking_type: str = "",
                scoring: str = "", season: int = 0, limit: int = 150) -> str:
    """FantasyPros consensus rankings, live.

    Defaults derive from the league: dynasty vs redraft, PPR/HALF/STD, and
    superflex (OP) vs 1QB (ALL). Override any of them for mock drafts or to
    read the wider market.
    position: QB RB WR TE K DST FLX OP ALL.  ranking_type: DRAFT ROS DYNASTY
    ADP ROOKIES DYNADP."""
    prof = fp_profile(league_id)
    position = position or prof["position"]
    ranking_type = ranking_type or prof["type"]
    scoring = scoring or prof["scoring"]
    season = season or prof["season"]

    data = _fp(f"nfl/{season}/consensus-rankings",
               {"position": position, "type": ranking_type,
                "scoring": scoring, "experts": "show"},
               cache_key=f"fp_{season}_{position}_{ranking_type}_{scoring}",
               ttl=TTL["rankings"])
    pls = data.get("players", [])
    rows = [f"{str(p.get('rank_ecr','?')):>4}  {(p.get('player_name') or '')[:24]:<25}"
            f"{(p.get('player_position_id') or ''):<4}{(p.get('player_team_id') or ''):<5}"
            f"ecr {p.get('rank_ave','?')}" for p in pls[:limit]]
    return (f"{_profile_header(prof)}\n"
            f"FantasyPros {ranking_type} {position} {scoring} {season} — "
            f"{len(pls)} ranked, showing {min(limit,len(pls))}\n" + "\n".join(rows))


@mcp.tool()
def fp_injuries() -> str:
    """Live FantasyPros NFL injury report."""
    data = _fp("nfl/injuries", {}, cache_key="fp_injuries", ttl=TTL["injuries"])
    return "\n".join(
        f"{(p.get('player_name') or '')[:24]:<25}{(p.get('player_position_id') or ''):<4}"
        f"{(p.get('player_team_id') or ''):<5}"
        f"{p.get('injury_status','')} {p.get('injury_details','')}"[:60]
        for p in data.get("players", [])[:200]) or "No data."


@mcp.tool()
def roster_vs_rankings(roster_id: int = 0, league_id: str = "",
                       position: str = "", ranking_type: str = "",
                       scoring: str = "", season: int = 0) -> str:
    """THE MAIN TOOL. Joins a live Sleeper roster to live FantasyPros consensus
    by normalized name, using the ranking set derived from that league's own
    format and scoring. Shows ECR per player and flags anyone outside consensus."""
    lid = _lid(league_id)
    prof = fp_profile(lid)
    position = position or prof["position"]
    ranking_type = ranking_type or prof["type"]
    scoring = scoring or prof["scoring"]
    season = season or prof["season"]

    rosters = rosters_raw(lid)
    rid = roster_id or resolve_me(lid, rosters)
    if rid is None:
        return (f"Cannot tell which roster is mine in league {lid}. "
                "Pass roster_id, or set SLEEPER_USERNAME.")

    sp = slim_players()
    fp = _fp(f"nfl/{season}/consensus-rankings",
             {"position": position, "type": ranking_type,
              "scoring": scoring, "experts": "show"},
             cache_key=f"fp_{season}_{position}_{ranking_type}_{scoring}",
             ttl=TTL["rankings"])
    idx = {norm_name(p.get("player_name", "")): p for p in fp.get("players", [])}

    target = next((r for r in rosters if r["roster_id"] == rid), None)
    if not target:
        return f"No roster_id {rid} in league {lid}."
    ir, taxi = set(target.get("reserve") or []), set(target.get("taxi") or [])

    ranked, unranked = [], []
    for pid in target.get("players") or []:
        p = sp.get(pid)
        if not p:
            unranked.append(f"     unresolved sleeper id {pid}")
            continue
        slot = "IR" if pid in ir else ("TAXI" if pid in taxi else "")
        m = idx.get(norm_name(p["name"]))
        if m:
            ranked.append((float(m.get("rank_ecr") or 9999), p, slot))
        else:
            unranked.append("     " + _fmt(p, slot))
    ranked.sort(key=lambda x: x[0])

    out = [_profile_header(prof),
           f"Roster {rid} vs FantasyPros {ranking_type} {position} {scoring} {season}",
           ""]
    out += [f"{int(rk):>4}  {_fmt(p, sl)}" for rk, p, sl in ranked]
    if unranked:
        out += ["", f"UNRANKED / outside consensus ({len(unranked)}):"] + unranked
    return "\n".join(out)


@mcp.tool()
def draft_board(league_id: str = "", limit: int = 60) -> str:
    """Live draft: picks so far (most recent first) plus who is on the clock.
    Cached 30s so it stays current during a running draft."""
    lid = _lid(league_id)
    lg = league_meta(lid)
    did = lg.get("draft_id")
    if not did:
        return f"No draft attached to league {lid}."
    draft = _fetch(f"{SLEEPER_BASE}/draft/{did}",
                   cache_key=f"draft_{did}", ttl=TTL["draft"])
    picks = _fetch(f"{SLEEPER_BASE}/draft/{did}/picks",
                   cache_key=f"draftpicks_{did}", ttl=TTL["draft"])
    sp, users = slim_players(), league_users(lid)
    teams = draft.get("settings", {}).get("teams", lg.get("total_rosters", 12))
    slot_to_uid = {int(k): v for k, v in (draft.get("draft_order") or {}).items()} \
        if draft.get("draft_order") else {}

    head = [f"{lg.get('name','?')} draft — {draft.get('status')} · "
            f"{draft.get('type')} · {teams} teams · {len(picks)} picks made"]
    if draft.get("status") == "drafting":
        nxt = len(picks) + 1
        rnd, slot = (nxt - 1) // teams + 1, (nxt - 1) % teams + 1
        if draft.get("type") == "snake" and rnd % 2 == 0:
            slot = teams - slot + 1
        head.append(f"ON THE CLOCK: round {rnd}, pick {slot} — "
                    f"{users.get(slot_to_uid.get(slot), 'slot ' + str(slot))}")

    rows = []
    for pk in reversed(picks[-limit:]):
        p = sp.get(str(pk.get("player_id")))
        who = users.get(pk.get("picked_by")) or f"slot {pk.get('draft_slot')}"
        rows.append(f"{pk.get('round'):>2}.{pk.get('draft_slot'):02d} "
                    f"{str(who)[:18]:<19}"
                    f"{_fmt(p) if p else pk.get('player_id')}")
    return "\n".join(head + [""] + rows)


@mcp.tool()
def refresh_cache(what: str = "all") -> str:
    """Force-refresh cached data.
    what: all | rosters | rankings | players | league | draft."""
    pats = {"all": "*", "rosters": "rosters_*", "rankings": "fp_*",
            "players": "sleeper_players*", "league": "league_*",
            "draft": "draft*"}
    n = 0
    for f in CACHE_DIR.glob(pats.get(what, "*")):
        f.unlink()
        n += 1
    if what in ("all", "players"):
        _MEMO.clear()
    return f"Cleared {n} cached file(s)."


# ----------------------------------------------------------------- auth + app

OPEN_PATHS = {"/healthz"}


class BearerGate(BaseHTTPMiddleware):
    """Optional second factor. The secret URL path is the primary gate.

    /healthz is exempt: Render's health checker cannot send the header, and a
    401 there means the service never goes live."""
    async def dispatch(self, request, call_next):
        if MCP_BEARER and request.url.path not in OPEN_PATHS:
            got = request.headers.get("authorization", "")
            if not got.startswith("Bearer ") or not secrets.compare_digest(
                    got[7:], MCP_BEARER):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_):
    return PlainTextResponse("ok")


class LogErrors(BaseHTTPMiddleware):
    """Render showed a bare 500 with nothing in the logs. Surface the traceback."""
    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logging.exception("Unhandled error on %s %s",
                              request.method, request.url.path)
            raise


if not MCP_SECRET or len(MCP_SECRET) < 24:
    raise SystemExit("MCP_SECRET must be set and at least 24 characters.")

# Starlette does NOT run the lifespan of a mounted sub-app. The MCP streamable
# HTTP app starts its session manager in its lifespan, so without passing it
# through, every MCP request hits an uninitialized task group and 500s while
# /healthz keeps working. Hand the child's lifespan to the parent.
mcp_app = mcp.streamable_http_app()

app = Starlette(
    routes=[
        Route("/healthz", health),
        Mount(f"/{MCP_SECRET}", app=mcp_app),
    ],
    lifespan=lambda _: mcp_app.router.lifespan_context(mcp_app),
)
app.add_middleware(BearerGate)
app.add_middleware(LogErrors)

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    logging.info("DNS-rebinding allowlist: %s",
                 _hosts or "DISABLED (no RENDER_EXTERNAL_HOSTNAME/ALLOWED_HOSTS)")
    logging.info("default league %s · me=%s", DEFAULT_LEAGUE, MY_USER or MY_ROSTER)
    logging.info("MCP mounted at /<MCP_SECRET>/mcp — health at /healthz")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)),
                log_level="info")
