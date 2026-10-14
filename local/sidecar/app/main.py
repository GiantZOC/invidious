"""Smart feed, Discover, and settings pages served beside Invidious.

Reached through Caddy at /feed/smart, /feed/discover and /x/*. Pages reuse
Invidious's own stylesheet and card markup, so they inherit its themes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import collector, ranking
from .db import Database
from .invidious import InvidiousClient, InvidiousError

log = logging.getLogger("sidecar")

BASE_DIR = Path(__file__).parent
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
FEED_MENU = ["Subscriptions", "Smart", "Discover", "Playlists"]
DISCOVER_LIMIT = 500
FEED_LIMIT = 400

DEFAULT_PREFS: dict[str, Any] = {
    "hide_shorts": True,
    "hide_live": True,
    "hide_premieres": True,
    "unseen_only": True,
    "min_len": None,
    "max_len": None,
    "group": None,
    "sort": "recency",
}
PREF_FIELDS = (
    "hide_shorts",
    "hide_live",
    "hide_premieres",
    "unseen_only",
)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def load_prefs(db: Database) -> dict[str, Any]:
    prefs = dict(DEFAULT_PREFS)
    raw = db.get_meta("smart_prefs")
    if raw:
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            stored = {}
        for key, value in stored.items():
            if key in prefs:
                prefs[key] = value
    # Numeric fields arrive from HTML form fields, which are strings.
    for key in ("min_len", "max_len"):
        value = prefs.get(key)
        prefs[key] = int(value) if value not in (None, "", 0) else None
    return prefs


def groups_from_db(db: Database) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in db.query(
        "SELECT ucid, grp FROM channel_prefs WHERE grp IS NOT NULL AND grp <> ''"
    ):
        groups.setdefault(row["grp"], []).append(row["ucid"])
    return groups


def enrich_prefs(db: Database, prefs: dict[str, Any]) -> dict[str, Any]:
    """Add the per-channel state filter_feed needs, read fresh from the DB."""
    prefs = dict(prefs)
    prefs["muted_ucids"] = {
        row["ucid"] for row in db.query("SELECT ucid FROM channel_prefs WHERE muted = 1")
    }
    prefs["groups"] = groups_from_db(db)
    return prefs


def watched_ids(db: Database) -> set[str]:
    return {row["video_id"] for row in db.query("SELECT video_id FROM watches")}


def blocked_channels(db: Database) -> set[str]:
    return {
        row["target_id"]
        for row in db.query(
            "SELECT DISTINCT target_id FROM feedback"
            " WHERE target_type = 'channel' AND kind = 'block'"
        )
    }


def dismissed_videos(db: Database) -> set[str]:
    return {
        row["target_id"]
        for row in db.query(
            "SELECT DISTINCT target_id FROM feedback"
            " WHERE target_type = 'video' AND kind IN ('dislike', 'dismiss')"
        )
    }


def channel_signal(db: Database) -> dict[str, dict[str, int]]:
    """Watches and ratings per channel, feeding channel_affinity."""
    signal: dict[str, dict[str, int]] = {}

    def bucket(ucid: str) -> dict[str, int]:
        return signal.setdefault(ucid, {"watches": 0, "likes": 0, "dislikes": 0})

    for row in db.query(
        "SELECT v.ucid AS ucid, COUNT(*) AS n FROM watches w"
        " JOIN videos v ON v.video_id = w.video_id"
        " WHERE v.ucid IS NOT NULL GROUP BY v.ucid"
    ):
        bucket(row["ucid"])["watches"] = row["n"]

    for row in db.query(
        "SELECT target_id AS ucid, kind, COUNT(*) AS n FROM feedback"
        " WHERE target_type = 'channel' AND kind IN ('like', 'dislike')"
        " GROUP BY target_id, kind"
    ):
        key = "likes" if row["kind"] == "like" else "dislikes"
        bucket(row["ucid"])[key] = row["n"]
    return signal


def source_ages(db: Database, now: float) -> dict[str, list[float]]:
    """Per candidate: ages (days) of the watched videos that link to it."""
    out: dict[str, list[float]] = {}
    rows = db.query(
        "SELECT e.dst_video AS dst, w.first_seen_at AS seen"
        " FROM edges e JOIN watches w ON w.video_id = e.src_video"
    )
    for row in rows:
        seen = row["seen"]
        # 0 marks an ID imported by the first backfill: date unknown, so it
        # reinforces the candidate without being recent.
        age = 3650.0 if not seen else max(0.0, (now - seen) / 86400.0)
        out.setdefault(row["dst"], []).append(age)
    return out


def build_candidates(db: Database, now: float) -> list[dict[str, Any]]:
    watched = watched_ids(db)
    dismissed = dismissed_videos(db)
    blocked = blocked_channels(db)
    subscribed = {
        row["ucid"] for row in db.query("SELECT ucid FROM subscriptions")
    }
    signal = channel_signal(db)
    ages = source_ages(db, now)
    shown = {
        row["target_id"]: row["n"]
        for row in db.query(
            "SELECT target_id, COUNT(*) AS n FROM feedback"
            " WHERE target_type = 'video' AND kind IN ('dismiss', 'hide')"
            " GROUP BY target_id"
        )
    }
    weights = {
        row["ucid"]: row["weight"]
        for row in db.query("SELECT ucid, weight FROM channel_prefs")
    }
    muted = {
        row["ucid"]
        for row in db.query("SELECT ucid FROM channel_prefs WHERE muted = 1")
    }

    rows = db.query(
        "SELECT v.video_id, v.title, v.ucid, v.author, v.length_seconds,"
        " v.published, v.views, v.live_now, v.premiere_timestamp,"
        " MAX(e.seen_at) AS last_seen, COUNT(DISTINCT e.src_video) AS src_count"
        " FROM edges e JOIN videos v ON v.video_id = e.dst_video"
        " GROUP BY v.video_id"
        " ORDER BY last_seen DESC LIMIT ?",
        (DISCOVER_LIMIT,),
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        ucid = row["ucid"]
        if row["video_id"] in watched or row["video_id"] in dismissed:
            continue
        if ucid and (ucid in blocked or ucid in muted):
            continue
        ch = signal.get(ucid, {})
        features = {
            "cooccurrence": ranking.cooccurrence_score(ages.get(row["video_id"], [])),
            "channel_affinity": ranking.channel_affinity_score(
                watches=ch.get("watches", 0),
                likes=ch.get("likes", 0),
                dislikes=ch.get("dislikes", 0),
            ),
            "freshness": ranking.freshness_score(row["published"], now),
            "novelty": ranking.novelty_score(
                subscribed=bool(ucid) and ucid in subscribed,
                seen_count=shown.get(row["video_id"], 0),
            ),
        }
        candidate = {key: row[key] for key in row.keys()}
        candidate["features"] = features
        candidate["subscribed"] = bool(ucid) and ucid in subscribed
        candidate["weight"] = weights.get(ucid, 1.0)
        out.append(candidate)

    rules = [dict(r) for r in db.query("SELECT kind, pattern FROM rules")]
    out = [
        c for c in out
        if ranking.rule_verdict(c.get("title"), rules) != "block"
    ]
    return ranking.rank_candidates(out)


def feed_rows(db: Database, limit: int = FEED_LIMIT) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT v.video_id, v.title, v.ucid, v.author, v.length_seconds,"
        " v.published, v.views, v.live_now, v.premiere_timestamp,"
        " f.first_seen AS feed_first_seen"
        " FROM feed_items f JOIN videos v ON v.video_id = f.video_id"
        " ORDER BY COALESCE(v.published, f.first_seen) DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


THEME_TTL_SECONDS = 300


async def theme_loop(app: FastAPI) -> None:
    """Mirror the user's dark_mode so sidecar pages match Invidious.

    The sidecar has no session cookie, so the API token's own preferences are
    the source of truth; ?dark_mode=dark|light overrides it per request the same
    way Invidious's own pages do.
    """
    client: InvidiousClient = app.state.client
    while True:
        if client.token:
            try:
                prefs = await client.preferences()
                mode = prefs.get("dark_mode")
                if mode in ("dark", "light"):
                    app.state.dark_mode = mode
            except InvidiousError as exc:
                log.warning("theme refresh failed: %s", exc)
        await asyncio.sleep(THEME_TTL_SECONDS)


def theme_for(request: Request) -> str:
    override = request.query_params.get("dark_mode")
    if override in ("dark", "light"):
        return override
    return getattr(request.app.state, "dark_mode", "auto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.dark_mode = "auto"
    db = Database(os.environ.get("SIDECAR_DB", "/data/sidecar.db"))
    await asyncio.to_thread(db.connect)
    client = InvidiousClient(
        os.environ.get("INVIDIOUS_URL", "http://invidious:3000"),
        os.environ.get("INVIDIOUS_TOKEN", ""),
        min_interval=env_int("FETCH_MIN_INTERVAL_SECONDS", 5),
    )
    app.state.db = db
    app.state.client = client
    app.state.collector_task = None
    app.state.theme_task = None
    app.state.collector_enabled = bool(client.token)

    app.state.theme_task = asyncio.create_task(theme_loop(app))

    if client.token:
        app.state.collector_task = asyncio.create_task(
            collector.collector_loop(
                db,
                client,
                history_seconds=env_int("POLL_HISTORY_SECONDS", 300),
                feed_seconds=env_int("POLL_FEED_SECONDS", 900),
                max_per_cycle=env_int("FETCH_MAX_PER_CYCLE", 20),
            )
        )
        log.info("collector started")
    else:
        log.warning("INVIDIOUS_TOKEN is empty: collector disabled, pages show stored data")

    try:
        yield
    finally:
        if app.state.collector_task is not None:
            app.state.collector_task.cancel()
            await asyncio.gather(app.state.collector_task, return_exceptions=True)
        if app.state.theme_task is not None:
            app.state.theme_task.cancel()
            await asyncio.gather(app.state.theme_task, return_exceptions=True)
        await client.aclose()
        db.close()


app = FastAPI(title="Invidious local sidecar", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/x/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def csp(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    return response


def ctx(request: Request, **extra: Any) -> dict[str, Any]:
    now = time.time()
    dark_mode = theme_for(request)
    context: dict[str, Any] = {
        "request": request,
        "now": now,
        "dark_mode": dark_mode,
        "body_class": dark_mode if dark_mode in ("dark", "light") else "no",
        "active": extra.get("active", ""),
        "feed_menu": [
            ("Subscriptions", "/feed/subscriptions"),
            ("Smart", "/feed/smart"),
            ("Discover", "/feed/discover"),
            ("Settings", "/x/settings"),
        ],
    }
    context.update(ranking.template_context(now))
    context.update(extra)
    return context


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/feed/discover")


@app.get("/feed/smart", response_class=HTMLResponse)
def smart_feed(request: Request, group: str | None = None, sort: str | None = None):
    db: Database = request.app.state.db
    prefs = load_prefs(db)
    if group is not None:
        prefs["group"] = group or None
    if sort in ("recency", "weighted"):
        prefs["sort"] = sort

    groups = groups_from_db(db)
    prefs = enrich_prefs(db, prefs)
    rules = [dict(r) for r in db.query("SELECT kind, pattern FROM rules")]
    watched = watched_ids(db)
    items = feed_rows(db)
    kept = ranking.filter_feed(
        items,
        prefs=prefs,
        rules=rules,
        watched_ids=watched,
        now=time.time(),
    )
    weights = {
        row["ucid"]: row["weight"]
        for row in db.query("SELECT ucid, weight FROM channel_prefs")
    }
    ordered = ranking.sort_items(
        kept,
        mode=prefs["sort"],
        weights=weights,
        now=time.time(),
    )
    return templates.TemplateResponse(
        request=request,
        name="smart.html",
        context=ctx(
            request,
            active="Smart",
            items=ordered,
            prefs=prefs,
            groups=sorted(groups),
            watched=watched,
            rules=rules,
            total=len(items),
            shown=len(ordered),
        ),
    )


@app.get("/feed/discover", response_class=HTMLResponse)
def discover_feed(request: Request):
    db: Database = request.app.state.db
    now = time.time()
    candidates = build_candidates(db, now)
    watched = watched_ids(db)
    shorts = [c for c in candidates if ranking.is_short(c)]
    for_you = [c for c in candidates if not ranking.is_short(c)][:60]
    fresh = [c for c in for_you if not c["subscribed"]][:40]
    from_subs = [c for c in for_you if c["subscribed"]][:40]

    counts = {
        "watches": db.scalar("SELECT COUNT(*) FROM watches", default=0),
        "edges": db.scalar("SELECT COUNT(*) FROM edges", default=0),
        "videos": db.scalar("SELECT COUNT(*) FROM videos", default=0),
    }
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=ctx(
            request,
            active="Discover",
            for_you=for_you,
            fresh=fresh,
            from_subs=from_subs,
            watched=watched,
            counts=counts,
            hidden_shorts=len(shorts),
            token_ready=request.app.state.collector_enabled,
        ),
    )


@app.get("/x/settings", response_class=HTMLResponse)
def settings(request: Request):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=ctx(
            request,
            active="Settings",
            prefs=load_prefs(db),
            rules=[dict(r) for r in db.query("SELECT * FROM rules ORDER BY id")],
            channels=[
                dict(r)
                for r in db.query(
                    "SELECT c.ucid, c.author, p.grp, p.weight, p.muted,"
                    " p.min_len, p.max_len FROM channels c"
                    " LEFT JOIN channel_prefs p ON p.ucid = c.ucid"
                    " ORDER BY c.author"
                )
            ],
            menu=FEED_MENU,
            token_ready=request.app.state.collector_enabled,
        ),
    )


@app.post("/x/settings/prefs")
async def save_prefs(request: Request):
    form = await request.form()
    db: Database = request.app.state.db
    current = load_prefs(db)
    for field in PREF_FIELDS:
        current[field] = form.get(field) == "on"

    def number(key: str) -> int | None:
        raw = str(form.get(key, "")).strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    current["min_len"] = number("min_len")
    current["max_len"] = number("max_len")
    sort = str(form.get("sort", "recency"))
    current["sort"] = sort if sort in ("recency", "weighted") else "recency"
    group = str(form.get("group", "")).strip()
    current["group"] = group or None

    db.set_meta("smart_prefs", json.dumps(current))
    return RedirectResponse("/x/settings", status_code=303)


@app.post("/x/settings/channel")
async def save_channel(request: Request):
    form = await request.form()
    db: Database = request.app.state.db
    ucid = str(form.get("ucid", "")).strip()
    if not ucid:
        return RedirectResponse("/x/settings", status_code=303)

    def number(key: str) -> int | None:
        raw = str(form.get(key, "")).strip()
        return int(raw) if raw.isdigit() and int(raw) > 0 else None

    try:
        weight = float(str(form.get("weight", "1.0")).strip() or "1.0")
    except ValueError:
        weight = 1.0

    grp = str(form.get("grp", "")).strip()
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO channel_prefs (ucid, grp, weight, muted, min_len, max_len)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(ucid) DO UPDATE SET"
        "   grp = excluded.grp, weight = excluded.weight, muted = excluded.muted,"
        "   min_len = excluded.min_len, max_len = excluded.max_len",
        (
            ucid,
            grp or None,
            weight,
            1 if form.get("muted") == "on" else 0,
            number("min_len"),
            number("max_len"),
        ),
    )
    return RedirectResponse("/x/settings", status_code=303)


@app.post("/x/settings/rule")
async def add_rule(request: Request):
    form = await request.form()
    kind = str(form.get("kind", "")).strip()
    pattern = str(form.get("pattern", "")).strip()
    if pattern and kind in ("block", "boost"):
        await asyncio.to_thread(
            request.app.state.db.execute,
            "INSERT INTO rules (kind, pattern, at) VALUES (?, ?, ?)",
            (kind, pattern, int(time.time())),
        )
    return RedirectResponse("/x/settings", status_code=303)


@app.post("/x/settings/rule/delete")
async def delete_rule(request: Request):
    form = await request.form()
    raw = str(form.get("rule_id", "")).strip()
    if raw.isdigit():
        await asyncio.to_thread(
            request.app.state.db.execute,
            "DELETE FROM rules WHERE id = ?",
            (int(raw),),
        )
    return RedirectResponse("/x/settings", status_code=303)


@app.post("/x/settings/restore-menu")
async def restore_menu(request: Request):
    """Re-POST feed_menu, which the Invidious preferences page overwrites.

    Invidious rebuilds the whole preferences document from the POSTed JSON and
    defaults anything left out, so the current document is read first and sent
    back with only feed_menu changed.
    """
    client: InvidiousClient = request.app.state.client
    note = "Feed menu restored."
    status = "ok"
    if not client.token:
        note = "No INVIDIOUS_TOKEN configured."
        status = "error"
    else:
        try:
            current = await client.preferences()
            current["feed_menu"] = list(FEED_MENU)
            code = await client.set_preferences(current)
            check = await client.preferences()
            if check.get("feed_menu") != FEED_MENU:
                # set_preferences answers 204 even when the body failed to parse.
                note = f"Invidious did not store the menu (POST returned {code})."
                status = "error"
        except InvidiousError as exc:
            note = f"Invidious request failed: {exc}"
            status = "error"
    return JSONResponse({"ok": status == "ok", "note": note}, status_code=200)


@app.post("/x/api/feedback")
async def feedback(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "note": "bad json"}, status_code=400)

    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    kind = payload.get("kind")
    if target_type not in ("video", "channel") or kind not in (
        "like", "dislike", "block", "dismiss", "hide", "watched", "unblock"
    ) or not isinstance(target_id, str):
        return JSONResponse({"ok": False, "note": "bad request"}, status_code=400)

    db: Database = request.app.state.db
    await asyncio.to_thread(
        db.execute,
        "INSERT INTO feedback (target_type, target_id, kind, at) VALUES (?, ?, ?, ?)",
        (target_type, target_id, kind, int(time.time())),
    )

    mirrored = None
    if kind == "watched":
        client: InvidiousClient = request.app.state.client
        if client.token:
            try:
                mirrored = await client.mark_watched(target_id)
            except (InvidiousError, OSError) as exc:
                mirrored = f"failed: {exc}"
        await asyncio.to_thread(
            db.execute,
            "INSERT INTO watches (video_id, first_seen_at, source) VALUES (?, ?, ?)"
            " ON CONFLICT(video_id) DO NOTHING",
            (target_id, int(time.time()), "sidecar"),
        )
    return JSONResponse({"ok": True, "mirrored": mirrored})


@app.get("/x/health")
def health(request: Request):
    db: Database = request.app.state.db
    now = int(time.time())

    def age(key: str) -> int | None:
        raw = db.get_meta(key)
        return now - int(raw) if raw and raw.isdigit() else None

    counts = {
        table: db.scalar(f"SELECT COUNT(*) FROM {table}", default=0)
        for table in (
            "videos", "watches", "edges", "subscriptions", "feed_items", "feedback"
        )
    }
    return JSONResponse(
        {
            "ok": True,
            "now": now,
            "collector_enabled": request.app.state.collector_enabled,
            "counts": counts,
            "last_cycle_age": age("last_cycle"),
            "last_history_poll_age": age("last_history_poll"),
            "last_feed_poll_age": age("last_feed_poll"),
            "last_subscription_poll_age": age("last_subscription_poll"),
            "queued_videos": db.scalar(
                "SELECT COUNT(*) FROM videos WHERE fetched_at = 0", default=0
            ),
            "client_last_request_age": (
                int(now - request.app.state.client.last_request_at)
                if request.app.state.client.last_request_at
                else None
            ),
            "client_last_error": request.app.state.client.last_error,
        }
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error for %s", request.url.path)
    return JSONResponse({"ok": False, "note": str(exc)}, status_code=500)
