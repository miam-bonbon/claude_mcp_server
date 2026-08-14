#!/usr/bin/env python3
"""
Dynasty MCP server — remote (Streamable HTTP) build for Render.com.

Same tools as the stdio build. Differences:
  * Streamable HTTP transport so Anthropic's cloud can reach it
  * Shared-secret auth (secret path segment and/or bearer token)
  * Cache lives in /tmp because Render's free-tier disk is ephemeral

Env vars (set in Render dashboard, NOT in code):
  FANTASYPROS_API_KEY   required
  MCP_SECRET            required — long random string, becomes the URL path
  MCP_BEARER            optional — additionally require this bearer token
  SLEEPER_LEAGUE_ID     defaults to the league below
  MY_ROSTER_ID          defaults to 5
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

mcp = FastMCP("dynasty", stateless_http=True, host="0.0.0.0",
              transport_security=_security)

FP_KEY = os.environ.get("FANTASYPROS_API_KEY", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
MCP_BEARER = os.environ.get("MCP_BEARER", "")
FP_BASE = "https://api.fantasypros.com/public/v2/json"
SLEEPER_BASE = "https://api.sleeper.app/v1"
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "1312003800977899520")
MY_ROSTER = int(os.environ.get("MY_ROSTER_ID", "5"))

CACHE_DIR = Path(os.environ.get("DYNASTY_CACHE", "/tmp/dynasty_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTL = {"players": 86400, "rankings": 3600, "injuries": 900, "roster": 120}

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


# ----------------------------------------------------------------- sleeper data

FANTASY_POS = {"QB", "RB", "WR", "TE"}


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


def rosters_raw() -> list:
    return _fetch(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/rosters",
                  cache_key=f"rosters_{LEAGUE_ID}", ttl=TTL["roster"])


def league_users() -> dict:
    users = _fetch(f"{SLEEPER_BASE}/league/{LEAGUE_ID}/users",
                   cache_key=f"users_{LEAGUE_ID}", ttl=3600)
    return {u["user_id"]: (u.get("metadata", {}).get("team_name") or u.get("display_name"))
            for u in users}


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
def my_roster() -> str:
    """My team (roster_id 5), resolved to names, grouped by position, with
    IR/taxi slots and injury flags. Live from Sleeper."""
    return team_roster(MY_ROSTER)


@mcp.tool()
def team_roster(roster_id: int) -> str:
    """Resolved roster for any team in the league by roster_id (1-12)."""
    sp, users = slim_players(), league_users()
    for r in rosters_raw():
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
        order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}
        typed.sort(key=lambda x: (order.get(x[0]["pos"], 9), x[0]["rank"]))
        s = r.get("settings", {})
        head = (f"Roster {roster_id} — {users.get(r.get('owner_id'), '?')}\n"
                f"{s.get('wins',0)}-{s.get('losses',0)} · FAAB used {s.get('waiver_budget_used',0)}\n"
                f"{len(players)} total | {len(ir)} IR | {len(taxi)} taxi | "
                f"{len(players)-len(ir)-len(taxi)} active\n")
        return head + "\n" + "\n".join(_fmt(p, sl) for p, sl in typed) + \
            ("\n" + "\n".join(stray) if stray else "")
    return f"No roster_id {roster_id} in league {LEAGUE_ID}."


@mcp.tool()
def league_overview() -> str:
    """One line per team: manager, record, roster size, QB and TE counts.
    Spots who is QB-starved — the key scarcity in a superflex league."""
    sp, users = slim_players(), league_users()
    out = []
    for r in sorted(rosters_raw(), key=lambda x: x["roster_id"]):
        pl = r.get("players") or []
        qb = sum(1 for x in pl if sp.get(x, {}).get("pos") == "QB")
        te = sum(1 for x in pl if sp.get(x, {}).get("pos") == "TE")
        s = r.get("settings", {})
        mark = " <-- me" if r["roster_id"] == MY_ROSTER else ""
        out.append(f"{r['roster_id']:>2} {str(users.get(r.get('owner_id'),'?'))[:24]:<25}"
                   f"{s.get('wins',0)}-{s.get('losses',0)}  {len(pl):>2}p {qb:>2}QB {te:>2}TE{mark}")
    return "\n".join(out)


@mcp.tool()
def free_agents(position: str = "ALL", limit: int = 40) -> str:
    """Unrostered players, best-first by Sleeper search_rank.
    position: ALL, QB, RB, WR, TE."""
    sp = slim_players()
    taken = set()
    for r in rosters_raw():
        taken.update(r.get("players") or [])
    pool = sorted((p["rank"], pid, p) for pid, p in sp.items()
                  if pid not in taken and p["team"] != "FA"
                  and (position == "ALL" or p["pos"] == position))
    return "\n".join(_fmt(p) for _, _, p in pool[:limit]) or "None found."


@mcp.tool()
def trending(kind: str = "add", hours: int = 24, limit: int = 25) -> str:
    """League-wide most-added or most-dropped. kind: add | drop."""
    sp = slim_players()
    data = _fetch(f"{SLEEPER_BASE}/players/nfl/trending/{kind}"
                  f"?lookback_hours={hours}&limit={limit}",
                  cache_key=f"trend_{kind}_{hours}_{limit}", ttl=900)
    return "\n".join(f"{e['count']:>7,}  {_fmt(sp[e['player_id']])}"
                     for e in data if e["player_id"] in sp) or "None."


@mcp.tool()
def fp_rankings(position: str = "OP", ranking_type: str = "DYNASTY",
                scoring: str = "PPR", season: int = 2026, limit: int = 150) -> str:
    """FantasyPros consensus rankings, live.
    position: OP = superflex/2QB overall; also QB, RB, WR, TE, FLX, ALL.
    ranking_type: DYNASTY, ROS, DRAFT, ADP, ROOKIES, DYNADP.
    NOTE: FP assumes 4-pt passing TDs, no TE premium, no first-down bonus."""
    data = _fp(f"nfl/{season}/consensus-rankings",
               {"position": position, "type": ranking_type,
                "scoring": scoring, "experts": "show"},
               cache_key=f"fp_{season}_{position}_{ranking_type}_{scoring}",
               ttl=TTL["rankings"])
    pls = data.get("players", [])
    rows = [f"{str(p.get('rank_ecr','?')):>4}  {(p.get('player_name') or '')[:24]:<25}"
            f"{(p.get('player_position_id') or ''):<4}{(p.get('player_team_id') or ''):<5}"
            f"ecr {p.get('rank_ave','?')}" for p in pls[:limit]]
    return (f"FantasyPros {ranking_type} {position} {scoring} {season} — "
            f"{len(pls)} ranked, showing {min(limit,len(pls))}\n"
            f"(4-pt pass TD / no TE premium / no 1D bonus — adjust)\n" + "\n".join(rows))


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
def roster_vs_rankings(roster_id: int = 0, position: str = "OP",
                       season: int = 2026) -> str:
    """THE MAIN TOOL. Joins a live Sleeper roster to live FantasyPros dynasty
    superflex consensus by normalized name. Shows ECR per player and flags
    anyone outside consensus entirely."""
    rid = roster_id or MY_ROSTER
    sp = slim_players()
    fp = _fp(f"nfl/{season}/consensus-rankings",
             {"position": position, "type": "DYNASTY", "scoring": "PPR", "experts": "show"},
             cache_key=f"fp_{season}_{position}_DYNASTY_PPR", ttl=TTL["rankings"])
    idx = {norm_name(p.get("player_name", "")): p for p in fp.get("players", [])}

    target = next((r for r in rosters_raw() if r["roster_id"] == rid), None)
    if not target:
        return f"No roster_id {rid}."
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

    out = [f"Roster {rid} vs FantasyPros DYNASTY {position} (superflex) {season}",
           "(FP assumes 4-pt pass TD, no TE premium, no 1D bonus)", ""]
    out += [f"{int(rk):>4}  {_fmt(p, sl)}" for rk, p, sl in ranked]
    if unranked:
        out += ["", f"UNRANKED / outside consensus ({len(unranked)}):"] + unranked
    return "\n".join(out)


@mcp.tool()
def refresh_cache(what: str = "all") -> str:
    """Force-refresh cached data. what: all | rosters | rankings | players."""
    pats = {"all": "*", "rosters": "rosters_*", "rankings": "fp_*",
            "players": "sleeper_players*"}
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
    logging.info("MCP mounted at /<MCP_SECRET>/mcp — health at /healthz")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)),
                log_level="info")