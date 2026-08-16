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

# Sleeper and FantasyPros calling one player genuinely DIFFERENT names —
# nicknames, not formatting. norm_name already handles suffixes, apostrophes,
# periods and hyphens; no normalization could bridge Marquise -> Hollywood.
#
# Applied to BOTH sides, so this canonicalises rather than translates: if
# either source later switches to the other name, both still collapse to the
# same key. Keys and values are post-normalization (lowercase, no punctuation,
# no spaces). Add entries only after confirming against the position board —
# run unmatched() to find candidates.
NAME_ALIASES = {
    "marquisebrown": "hollywoodbrown",
}


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", "").replace("-", " ")
    s = "".join(p for p in re.split(r"\s+", s) if p and p not in SUFFIXES)
    return NAME_ALIASES.get(s, s)


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
                    # SORT KEY ONLY — never render this. It is Sleeper's internal
                    # search_rank, not an ECR and not a positional rank. Printing it
                    # next to a position label is exactly the ambiguity _rank_cols
                    # exists to prevent.
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


# --------------------------------------------------------------- rank plumbing

POS_BOARDS = ("QB", "RB", "WR", "TE")


def _board(position: str, ranking_type: str, scoring: str, season: int):
    """One FantasyPros consensus board. Cache key includes position, so each
    board caches independently under TTL['rankings']."""
    return _fp(f"nfl/{season}/consensus-rankings",
               {"position": position, "type": ranking_type,
                "scoring": scoring, "experts": "show"},
               cache_key=f"fp_{season}_{position}_{ranking_type}_{scoring}",
               ttl=TTL["rankings"])


def rank_maps(prof: dict, ranking_type: str, scoring: str, season: int,
              positions=POS_BOARDS) -> tuple[dict, dict, list]:
    """Fetch the overall board plus one board per position.

    Returns (overall, pos_rank, warnings):
      overall  : norm_name -> rank on this league's overall board (OP or ALL)
      pos_rank : norm_name -> (position, rank) on that position's own board
      warnings : notes for any board that failed to load

    These are SEPARATE FantasyPros surveys, not one list sliced two ways, so
    the two numbers will not always agree on ordering. That is expected.
    Boards are fetched through the normal 1h rankings cache; a warm instance
    makes zero network calls here. Never raises — a failed board yields a
    warning and empty entries so callers can still render.
    """
    overall: dict = {}
    pos_rank: dict = {}
    warnings: list = []

    for label, board_pos, sink in (
            ("overall", prof["position"], "overall"),
            *[(p, p, "pos") for p in positions]):
        try:
            data = _board(board_pos, ranking_type, scoring, season)
        except Exception as exc:
            warnings.append(f"{label} board unavailable ({type(exc).__name__})")
            continue
        for pl in data.get("players", []):
            rk = pl.get("rank_ecr")
            if rk is None:
                continue
            nm = norm_name(pl.get("player_name", ""))
            if not nm:
                continue
            if sink == "overall":
                overall.setdefault(nm, int(rk))
            else:
                pos_rank.setdefault(nm, (board_pos, int(rk)))
    return overall, pos_rank, warnings


def _rank_cols(ov, pr) -> str:
    """Render the two rank columns so neither can be misread as the other.

    Overall always carries the '#' prefix and the ' ovr' suffix; positional
    rank is a single glued token (TE9). A bare integer is never emitted next
    to a position label.
    """
    o = f"#{ov} ovr" if ov is not None else "#\u2014 ovr"
    p = f"{pr[0]}{pr[1]}" if pr else "\u2014"
    return f"{o:<9} {p:<7} "


RANK_LEGEND = ("cols: #N ovr = overall rank on the {ov} board | "
               "POSn = rank on that position's own board")


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
def my_roster(league_id: str = "", ranks: bool = True) -> str:
    """My team in the given league, resolved to names, grouped by position,
    with IR/taxi slots, injury flags, and FantasyPros ranks. Live from Sleeper.

    Injury strings here come from Sleeper's player map on a 24h cache and can
    lag badly — confirm any injury independently before acting on it."""
    lid = _lid(league_id)
    rid = resolve_me(lid)
    if rid is None:
        return (f"Cannot tell which roster is mine in league {lid}. "
                "Set SLEEPER_USERNAME in the environment, or call "
                "team_roster(roster_id=N, league_id=...) directly.")
    return team_roster(rid, lid, ranks)


@mcp.tool()
def team_roster(roster_id: int, league_id: str = "", ranks: bool = True) -> str:
    """Resolved roster for any team in any league, by roster_id.

    ranks=True (default) prefixes each player with overall and positional
    FantasyPros rank. A rankings outage NEVER takes down the roster listing:
    the rows render with em-dashes and a warning line instead."""
    lid = _lid(league_id)
    sp, users = slim_players(), league_users(lid)
    rosters = rosters_raw(lid)
    me = resolve_me(lid, rosters)

    overall, pos_rank, warnings, prof = {}, {}, [], None
    if ranks:
        try:
            prof = fp_profile(lid)
            overall, pos_rank, warnings = rank_maps(
                prof, prof["type"], prof["scoring"], prof["season"])
        except Exception as exc:
            warnings = [f"ranks unavailable ({type(exc).__name__}) — "
                        "roster shown without them"]

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
        # Within a position group, sort by positional rank so the printed
        # column reads monotonically; search_rank only breaks ties for players
        # no board covers, which pushes them to the bottom of their group.
        def _key(x):
            pr = pos_rank.get(norm_name(x[0]["name"]))
            return (POS_ORDER.get(x[0]["pos"], 9),
                    pr[1] if pr else 9999,
                    x[0]["rank"])
        typed.sort(key=_key)

        s = r.get("settings", {})
        head = (f"Roster {roster_id} — {users.get(r.get('owner_id'), '?')}"
                f"{'  <-- me' if roster_id == me else ''}\n"
                f"{s.get('wins',0)}-{s.get('losses',0)} · FAAB used {s.get('waiver_budget_used',0)}\n"
                f"{len(players)} total | {len(ir)} IR | {len(taxi)} taxi | "
                f"{len(players)-len(ir)-len(taxi)} active")
        if ranks:
            head += "\n" + RANK_LEGEND.format(ov=prof["position"] if prof else "OP")
        for w in warnings:
            head += f"\n  ! {w}"

        body = []
        for p, sl in typed:
            if ranks:
                nm = norm_name(p["name"])
                body.append(_rank_cols(overall.get(nm), pos_rank.get(nm))
                            + _fmt(p, sl))
            else:
                body.append(_fmt(p, sl))
        return head + "\n\n" + "\n".join(body) + \
            ("\n" + "\n".join(stray) if stray else "")
    return f"No roster_id {roster_id} in league {lid}."


@mcp.tool()
def league_overview(league_id: str = "", check_names: bool = True) -> str:
    """One line per team: manager, record, roster size, and positional counts.
    Spots positional scarcity across the league.

    check_names=True appends a name-match footer. This is deliberate: a
    name-match failure is silent and self-concealing, so the alarm belongs in
    a tool that gets called anyway rather than one someone must remember."""
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

    if check_names:
        try:
            total, matched, rows, warnings = scan_unmatched(lid)
            if warnings:
                out.append(f"\nname match: skipped, {len(warnings)} board(s) "
                           "unavailable")
            elif rows:
                out.append(f"\nname match: {matched}/{total} matched · "
                           f"{len(rows)} on no board — run unmatched() to see "
                           "who, some may be nickname mismatches worth real value")
            else:
                out.append(f"\nname match: {total}/{total} matched")
        except Exception as exc:
            out.append(f"\nname match: check failed ({type(exc).__name__})")
    return "\n".join(out)


def scan_unmatched(league_id: str = "") -> tuple[int, int, list, list]:
    """Walk every roster in the league and find players no board ranks.

    Returns (total, matched, rows, warnings) where rows are
    (roster_id, owner, player) tuples. Cheap on a warm cache: the boards,
    player map and rosters are all already loaded by other calls."""
    lid = _lid(league_id)
    sp, users = slim_players(), league_users(lid)
    rosters = rosters_raw(lid)
    prof = fp_profile(lid)
    overall, pos_rank, warnings = rank_maps(
        prof, prof["type"], prof["scoring"], prof["season"])

    total = matched = 0
    rows = []
    for r in sorted(rosters, key=lambda x: x["roster_id"]):
        for pid in r.get("players") or []:
            p = sp.get(pid)
            if not p:
                continue
            total += 1
            nm = norm_name(p["name"])
            if nm in overall or nm in pos_rank:
                matched += 1
            else:
                rows.append((r["roster_id"],
                             str(users.get(r.get("owner_id"), "?")), p))
    return total, matched, rows, warnings


@mcp.tool()
def pick_landscape(league_id: str = "", seasons: str = "") -> str:
    """Who owns which future rookie picks, from Sleeper's traded_picks endpoint.

    Sleeper does NOT put picks in the roster object, so this is the only
    source. The endpoint returns only picks that have MOVED; everything else
    is still held by its original owner, so full ownership is derived by
    starting from the default grid and applying the trades.

    A pick keeps its origin forever: 'roster 4's 2027 1st' stays identifiable
    after any number of hands. Multi-hop trades are resolved by following the
    chain from the original owner, so the current holder is correct even when
    a pick moved several times.

    seasons: comma-separated, e.g. "2027,2028". Defaults to the two seasons
    after the current one."""
    lid = _lid(league_id)
    lg = league_meta(lid)
    users = league_users(lid)
    rosters = rosters_raw(lid)
    rids = sorted(r["roster_id"] for r in rosters)
    owner_of = {r["roster_id"]: str(users.get(r.get("owner_id"), "?"))
                for r in rosters}

    cur = int(lg.get("season") or 2026)
    if seasons.strip():
        want = [s.strip() for s in seasons.split(",") if s.strip()]
    else:
        want = [str(cur + 1), str(cur + 2)]

    rounds = int((lg.get("settings") or {}).get("draft_rounds") or 4)

    # Pull this league and, for dynasty rollovers, the previous league — a
    # future pick traded last season may still be recorded under the old id.
    sources, traded, notes = [(lid, "current")], [], []
    prev = lg.get("previous_league_id")
    if prev:
        sources.append((prev, "previous"))
    for src_id, label in sources:
        try:
            rows = _fetch(f"{SLEEPER_BASE}/league/{src_id}/traded_picks",
                          cache_key=f"picks_{src_id}", ttl=TTL["roster"])
            for row in rows or []:
                row["_src"] = label
            traded += rows or []
        except Exception as exc:
            notes.append(f"{label} league picks unavailable ({type(exc).__name__})")

    def holder(season: str, rnd: int, origin: int) -> int:
        """Current owner of the pick originally belonging to `origin`.

        Sleeper's `owner_id` is ALREADY the current holder, so the old
        chain-walk from previous_owner_id bought nothing and could fail
        silently: if Sleeper stores one row per pick (updating
        previous_owner_id to the most recent prior owner) rather than one row
        per hop, a twice-traded pick has no row pointing back to the original
        owner, the walk finds nothing on step one, and the pick is reported as
        never having moved.

        This resolves without assuming either storage model. Rows from the
        current league win over rows carried in from previous_league_id, since
        a rollover can leave a stale copy of the same pick. Among the winners,
        the current holder is the owner_id that is nobody else's
        previous_owner_id — the terminal node — which is the final owner under
        per-hop storage and the only owner under per-pick storage.
        """
        rows = [t for t in traded
                if str(t.get("season")) == season
                and int(t.get("round") or 0) == rnd
                and int(t.get("roster_id") or 0) == origin]
        if not rows:
            return origin
        rows = [t for t in rows if t.get("_src") == "current"] or rows
        owners = {int(t.get("owner_id") or 0) for t in rows}
        prevs = {int(t.get("previous_owner_id") or 0) for t in rows}
        terminal = owners - prevs
        if len(terminal) == 1:
            return terminal.pop()
        return int(rows[-1].get("owner_id") or origin)

    held = {rid: [] for rid in rids}
    for season in want:
        for rnd in range(1, rounds + 1):
            for origin in rids:
                cur_owner = holder(season, rnd, origin)
                held.setdefault(cur_owner, []).append((season, rnd, origin))

    out = [f"PICK LANDSCAPE — {lg.get('name','?')} · seasons {', '.join(want)} "
           f"· {rounds} rookie rounds"]
    for n in notes:
        out.append(f"  ! {n}")
    out.append(f"{len(traded)} traded-pick records; everything else sits with "
               "its original owner")
    out.append("")

    # One season requested -> full round-by-round grid. More than one -> firsts
    # only, or the block becomes unreadable. The old code advertised the grid
    # in its footer but hardcoded `if r == 1`, so no argument ever produced it.
    single = len(want) == 1

    for rid in rids:
        picks = sorted(held.get(rid, []))
        own = sum(1 for s, r, o in picks if o == rid)
        head = (f"{rid:>2} {owner_of.get(rid,'?')[:22]:<23}"
                f"{len(picks):>2} picks ({own} own)")
        if not single:
            firsts = [f"{s} 1st ({'own' if o == rid else f'r{o}'})"
                      for s, r, o in picks if r == 1]
            out.append(head + " | " + (", ".join(firsts) if firsts
                                       else "no firsts"))
            continue
        out.append(head)
        for rnd in range(1, rounds + 1):
            origins = sorted(o for s, r, o in picks if r == rnd)
            if not origins:
                out.append(f"     R{rnd}  —")
                continue
            tags = ", ".join("own" if o == rid else f"r{o}" for o in origins)
            out.append(f"     R{rnd}  {len(origins)}x  ({tags})")

    if not single:
        out += ["", "Firsts only. Call with a single season "
                    "(seasons=\"2027\") for the full round-by-round grid."]
    return "\n".join(out)


@mcp.tool()
def unmatched(league_id: str = "") -> str:
    """NAME-MATCH AUDIT. Every rostered player in the league that no
    FantasyPros board ranks. Run this after waiver day and after any trade.

    Two different causes land here and this tool CANNOT tell them apart:
      1. Consensus genuinely does not rank him — fine, ignore.
      2. A name-match failure — FP ranks him under a different name, so he
         silently reads as worthless. Sleeper's "Marquise Brown" is FP's
         "Hollywood Brown"; that one cost a real WR108 until it was found.

    Check each name against its position board via fp_rankings before
    dismissing it. Confirmed nicknames go in NAME_ALIASES in this file."""
    lid = _lid(league_id)
    try:
        total, matched, rows, warnings = scan_unmatched(lid)
    except Exception as exc:
        return f"Name-match audit failed ({type(exc).__name__}): {exc}"

    lg = league_meta(lid)
    out = [f"NAME-MATCH AUDIT — {lg.get('name','?')}"]
    if warnings:
        out += [f"  ! {w}" for w in warnings]
        out.append("  ! COUNT BELOW IS INFLATED BY THE ABOVE — do not act on it")
    out.append(f"{total} rostered · {matched} matched · {len(rows)} on no board")
    out.append("")
    if not rows:
        out.append("Every rostered player matched a board.")
        return "\n".join(out)
    out += [f"{rid:>2} {owner[:20]:<21}{_fmt(p)}" for rid, owner, p in rows]
    out += ["", "Verify each against fp_rankings(position=...) before "
                "assuming unranked."]
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
    # Suffix is load-bearing: a bare integer immediately left of a position
    # label reads as a rank. This number is a waiver count, not a rank.
    verb = "adds" if kind == "add" else "drops"
    return "\n".join(f"{'+' + format(e['count'], ',') + ' ' + verb:<16}"
                     f"{_fmt(sp[e['player_id']])}"
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


# The injuries endpoint does NOT share the rankings response shape. It
# returns its array under "injuries", not "players", and the per-player keys
# are bare: name / position_id / team_id / status, not player_name /
# player_position_id / player_team_id / injury_status. Reading it with the
# rankings keys yields an empty list on every call, which the old `or "No
# data."` reported as an outage for months. Verified against live JSON
# 2026-08-16.
#
# Confirmed query-param behaviour — do not "improve" this:
#   no params              -> 114 entries (37 PUP, 69 IR, 8 OUT)  <-- correct
#   ?season=&week=1        -> byte-identical to no params
#   ?season=&week=0|draft  -> 11 entries, 9 of them retired players
# week=draft is NOT the offseason value; passing it makes the feed worse.

# Worst first, so the lines that matter sit at the top.
_INJ_ORDER = {"OUT": 0, "IR": 1, "PUP": 2, "DOUBTFUL": 3,
              "QUESTIONABLE": 4, "PROBABLE": 5}
_INJ_SKILL = ("QB", "RB", "WR", "TE")


@mcp.tool()
def fp_injuries(positions: str = "SKILL", include_retired: bool = False) -> str:
    """Live FantasyPros NFL injury report.

    positions: "SKILL" (QB/RB/WR/TE, the default), "ALL", or a comma-separated
      list such as "RB,WR". The feed is league-wide and includes IDP — only 44
      of 114 entries were skill positions on 2026-08-16, so ALL is mostly noise
      for a league with no kickers or defenses.
    include_retired: the feed carries retired players as OUT with
      comment="retired" (Russell Wilson, Thielen). Off by default.

    CAVEAT: the response carries "public_api_limited": true, and as of
    2026-08-16 there were no QUESTIONABLE/DOUBTFUL entries and every
    probability_of_playing was null. That is consistent with preseason (no
    practice reports exist yet) OR with the public tier stripping them.
    Re-test in Week 1 before trusting this for game-day status.
    """
    data = _fp("nfl/injuries", {}, cache_key="fp_injuries", ttl=TTL["injuries"])
    rows = data.get("injuries", [])
    if not rows:
        return (f"fp_injuries: request succeeded but the injuries list was "
                f"empty (count={data.get('count')}, keys={sorted(data)[:8]}). "
                f"Not a request failure — check the response shape.")

    want = positions.strip().upper()
    if want == "SKILL":
        keep = set(_INJ_SKILL)
    elif want == "ALL":
        keep = None
    else:
        keep = {p.strip().upper() for p in positions.split(",") if p.strip()}

    sel = [p for p in rows
           if (keep is None or p.get("position_id") in keep)
           and (include_retired or p.get("comment") != "retired")]
    sel.sort(key=lambda p: (_INJ_ORDER.get((p.get("status") or "").upper(), 9),
                            p.get("position_id") or "", p.get("name") or ""))

    if not sel:
        return f"No injuries matching positions={positions} ({len(rows)} in feed)."

    out = [f"FANTASYPROS INJURIES — {len(sel)} of {len(rows)} · positions={positions}"]
    for p in sel[:200]:
        detail = p.get("injury_type") or p.get("comment") or ""
        prob = p.get("probability_of_playing")
        out.append(
            f"  {(p.get('status') or '?'):<4}{(p.get('position_id') or '??'):<4}"
            f"{(p.get('name') or '')[:26]:<27}{(p.get('team_id') or ''):<4}"
            f"{detail[:26]:<27}{(p.get('injury_update_date') or '')[:10]}"
            + (f"  {prob}%" if prob is not None else ""))
    if data.get("public_api_limited"):
        out.append("  (public_api_limited=true — feed may be a subset)")
    return "\n".join(out)


@mcp.tool()
def roster_vs_rankings(roster_id: int = 0, league_id: str = "",
                       position: str = "", ranking_type: str = "",
                       scoring: str = "", season: int = 0,
                       group_by_position: bool = False) -> str:
    """THE MAIN TOOL. Joins a live Sleeper roster to live FantasyPros consensus
    by normalized name, using the ranking set derived from that league's own
    format and scoring.

    Every player gets TWO numbers: overall rank on the league's overall board
    (rendered '#88 ovr') and rank on that player's own positional board
    (rendered 'TE9'). They come from different FantasyPros surveys, so their
    orderings will sometimes disagree — that is not a bug.

    position: FILTERS the roster to that position (QB RB WR TE K DEF).
      It does NOT change which board is used; both boards are always fetched.
    group_by_position: emit one block per position instead of one overall-
      sorted list. Better for 'how is my TE room', worse for cross-position
      trade comparison."""
    lid = _lid(league_id)
    prof = fp_profile(lid)
    ranking_type = ranking_type or prof["type"]
    scoring = scoring or prof["scoring"]
    season = season or prof["season"]
    pos_filter = (position or "").strip().upper()
    if pos_filter in ("", "ALL", "OP", "FLX"):
        pos_filter = ""

    rosters = rosters_raw(lid)
    rid = roster_id or resolve_me(lid, rosters)
    if rid is None:
        return (f"Cannot tell which roster is mine in league {lid}. "
                "Pass roster_id, or set SLEEPER_USERNAME.")
    target = next((r for r in rosters if r["roster_id"] == rid), None)
    if not target:
        return f"No roster_id {rid} in league {lid}."

    sp = slim_players()
    overall, pos_rank, warnings = rank_maps(prof, ranking_type, scoring, season)
    ir, taxi = set(target.get("reserve") or []), set(target.get("taxi") or [])

    rows, nowhere, stray = [], [], []
    for pid in target.get("players") or []:
        p = sp.get(pid)
        if not p:
            stray.append(f"     unresolved sleeper id {pid}")
            continue
        if pos_filter and p["pos"] != pos_filter:
            continue
        slot = "IR" if pid in ir else ("TAXI" if pid in taxi else "")
        nm = norm_name(p["name"])
        ov, pr = overall.get(nm), pos_rank.get(nm)
        if ov is None and pr is None:
            nowhere.append("     " + _fmt(p, slot))
        else:
            rows.append((ov, pr, p, slot))

    def line(ov, pr, p, sl):
        return _rank_cols(ov, pr) + _fmt(p, sl)

    out = [_profile_header(prof),
           f"Roster {rid} vs FantasyPros {ranking_type} {scoring} {season}"
           + (f" — {pos_filter} only" if pos_filter else ""),
           RANK_LEGEND.format(ov=prof["position"])]
    for w in warnings:
        out.append(f"  ! {w}")
    out.append("")

    if group_by_position:
        for grp in sorted({p["pos"] for _, _, p, _ in rows},
                          key=lambda x: POS_ORDER.get(x, 9)):
            block = [r for r in rows if r[2]["pos"] == grp]
            block.sort(key=lambda r: (r[1][1] if r[1] else 9999,
                                      r[0] if r[0] is not None else 9999))
            out.append(f"{grp} ({len(block)})")
            out += ["  " + line(*r) for r in block]
            out.append("")
    else:
        rows.sort(key=lambda r: (r[0] if r[0] is not None else 99999,
                                 POS_ORDER.get(r[2]["pos"], 9),
                                 r[1][1] if r[1] else 9999))
        out += [line(*r) for r in rows]

    # Split deliberately: ranked-nowhere is the bucket most likely to contain a
    # norm_name() mismatch (FP ranks him, the string just did not line up), and
    # that failure is otherwise silent. Missing from ONE board is normal depth.
    if nowhere:
        out += ["", f"ON NO BOARD ({len(nowhere)}) — genuinely unranked, or a "
                    "name-match failure; verify before dismissing:"] + nowhere
    if stray:
        out += ["", f"UNRESOLVED SLEEPER IDS ({len(stray)}):"] + stray
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
    what: all | rosters | rankings | players | league | draft | injuries."""
    # "rankings" globs fp_{season}_* so it no longer also nukes fp_injuries,
    # which lives on a much shorter TTL and is expensive to lose.
    pats = {"all": "*", "rosters": "rosters_*", "rankings": "fp_2*",
            "players": "sleeper_players*", "league": "league_*",
            "draft": "draft*", "injuries": "fp_injuries*"}
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
