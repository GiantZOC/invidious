"""Polls the Invidious API and builds the watch/related graph.

Invidious exposes watch history as a bare list of video IDs with no timestamps,
so the sidecar timestamps a video the first time it sees it. The first cycle is
a backfill: those rows get ``first_seen_at = 0`` and ``source = 'backfill'`` so
ranking can tell "seen on day X" from "already there when we started".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .db import Database
from .invidious import InvidiousClient, InvidiousError

log = logging.getLogger("sidecar.collector")

MAX_HISTORY_PAGES = 10
HISTORY_PAGE_SIZE = 100
FEED_PAGES = 2
FEED_PAGE_SIZE = 40
# Related videos enqueued for metadata per harvested video.
MAX_ENQUEUE_PER_VIDEO = 12

UPSERT_VIDEO = """
INSERT INTO videos (video_id, title, ucid, author, length_seconds, published,
                    views, live_now, premiere_timestamp, source, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(video_id) DO UPDATE SET
    title              = COALESCE(excluded.title, videos.title),
    ucid               = COALESCE(excluded.ucid, videos.ucid),
    author             = COALESCE(excluded.author, videos.author),
    length_seconds     = COALESCE(excluded.length_seconds, videos.length_seconds),
    published          = COALESCE(excluded.published, videos.published),
    views              = COALESCE(excluded.views, videos.views),
    live_now           = COALESCE(excluded.live_now, videos.live_now),
    premiere_timestamp = COALESCE(excluded.premiere_timestamp, videos.premiere_timestamp),
    source             = excluded.source,
    fetched_at         = excluded.fetched_at
"""

UPSERT_CHANNEL = """
INSERT INTO channels (ucid, author, updated_at) VALUES (?, ?, ?)
ON CONFLICT(ucid) DO UPDATE SET
    author     = COALESCE(excluded.author, channels.author),
    updated_at = excluded.updated_at
"""

ENQUEUE_PLACEHOLDER = """
INSERT INTO videos (video_id, source, fetched_at) VALUES (?, ?, 0)
ON CONFLICT(video_id) DO NOTHING
"""


def parse_timestamp(value: Any) -> int | None:
    """Accepts epoch seconds (top-level video fields) and RFC3339 (related)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        if text.isdigit():
            return int(text)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _as_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_video(raw: Mapping[str, Any], source: str) -> dict[str, Any] | None:
    """Normalise an Invidious video object (``/api/v1/videos/:id`` or feed item)."""
    video_id = raw.get("videoId")
    if not isinstance(video_id, str) or not video_id:
        return None
    return {
        "video_id": video_id,
        "title": raw.get("title"),
        "ucid": raw.get("authorId") or None,
        "author": raw.get("author"),
        "length_seconds": _as_int(raw.get("lengthSeconds")),
        "published": parse_timestamp(raw.get("published")),
        "views": _as_int(raw.get("viewCount")),
        "live_now": 1 if raw.get("liveNow") else 0,
        "premiere_timestamp": parse_timestamp(raw.get("premiereTimestamp")),
        "source": source,
    }


def related_of(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recommended videos from a ``/api/v1/videos/:id`` response."""
    out: list[dict[str, Any]] = []
    for item in raw.get("recommendedVideos") or []:
        parsed = parse_video(item, "related")
        if parsed:
            out.append(parsed)
    return out


def diff_new_ids(ids: Sequence[str], known: Iterable[str]) -> list[str]:
    """Unseen IDs in the order given, deduplicated."""
    seen = set(known)
    out: list[str] = []
    for video_id in ids:
        if video_id in seen:
            continue
        seen.add(video_id)
        out.append(video_id)
    return out


def _channel_row(ucid: Any, author: Any, now: int) -> tuple | None:
    if not isinstance(ucid, str) or not ucid:
        return None
    return (ucid, author, now)


# ---------------------------------------------------------------------------
# Collection steps
# ---------------------------------------------------------------------------


def store_watches(
    db: Database, video_ids: Sequence[str], now: int, source: str
) -> int:
    """Record IDs as watched, stamping the first time the sidecar saw them."""
    if not video_ids:
        return 0
    first_seen = 0 if source == "backfill" else now
    db.executemany(
        "INSERT INTO watches (video_id, first_seen_at, source) VALUES (?, ?, ?)"
        " ON CONFLICT(video_id) DO NOTHING",
        [(vid, first_seen, source) for vid in video_ids],
    )
    db.executemany(ENQUEUE_PLACEHOLDER, [(vid, source) for vid in video_ids])
    return len(video_ids)


async def poll_history(db: Database, client: InvidiousClient, now: int) -> int:
    known = {row["video_id"] for row in db.query("SELECT video_id FROM watches")}
    backfill = not known
    new: list[str] = []
    for page in range(1, MAX_HISTORY_PAGES + 1):
        page_ids = await client.history_page(page, HISTORY_PAGE_SIZE)
        fresh = diff_new_ids(page_ids, known)
        if not fresh and page > 1:
            break
        known.update(fresh)
        new.extend(fresh)
        if len(page_ids) < HISTORY_PAGE_SIZE:
            break
    store_watches(db, new, now, "backfill" if backfill else "history")
    db.set_meta("last_history_poll", str(now))
    db.set_meta("history_backfilled", "1")
    return len(new)


def videos_to_fetch(db: Database, limit: int) -> list[str]:
    rows = db.query(
        "SELECT video_id FROM videos WHERE fetched_at = 0"
        " ORDER BY (SELECT first_seen_at FROM watches"
        "            WHERE watches.video_id = videos.video_id) DESC, video_id"
        " LIMIT ?",
        (limit,),
    )
    return [row["video_id"] for row in rows]


async def harvest_related(
    db: Database, client: InvidiousClient, limit: int, now: int
) -> dict[str, int]:
    """Fetch metadata for queued videos and record related videos as edges."""
    fetched = 0
    edges = 0
    failed = 0
    for video_id in videos_to_fetch(db, limit):
        try:
            raw = await client.video(video_id)
        except InvidiousError as exc:
            log.warning("video %s: %s", video_id, exc)
            failed += 1
            # Leave fetched_at at 0 so the next cycle retries it.
            continue

        parsed = parse_video(raw, "watch")
        if parsed is None:
            db.execute(
                "UPDATE videos SET fetched_at = ? WHERE video_id = ?", (now, video_id)
            )
            continue

        row = (
            parsed["video_id"],
            parsed["title"],
            parsed["ucid"],
            parsed["author"],
            parsed["length_seconds"],
            parsed["published"],
            parsed["views"],
            parsed["live_now"],
            parsed["premiere_timestamp"],
            "history" if parsed["source"] == "watch" else parsed["source"],
            now,
        )
        db.execute(UPSERT_VIDEO, row)
        channel = _channel_row(parsed["ucid"], parsed["author"], now)
        if channel:
            db.execute(UPSERT_CHANNEL, channel)

        related = related_of(raw)[:MAX_ENQUEUE_PER_VIDEO]
        pairs = []
        enqueue = []
        for item in related:
            if item["video_id"] == video_id:
                continue
            pairs.append((video_id, item["video_id"], now))
            enqueue.append((item["video_id"], "related"))
            db.execute(UPSERT_VIDEO, (
                item["video_id"], item["title"], item["ucid"], item["author"],
                item["length_seconds"], item["published"], item["views"],
                item["live_now"], item["premiere_timestamp"], "related",
                0 if item.get("published") is None else now,
            ))
            channel = _channel_row(item["ucid"], item["author"], now)
            if channel:
                db.execute(UPSERT_CHANNEL, channel)

        db.executemany(
            "INSERT INTO edges (src_video, dst_video, seen_at) VALUES (?, ?, ?)"
            " ON CONFLICT(src_video, dst_video) DO NOTHING",
            pairs,
        )
        db.executemany(ENQUEUE_PLACEHOLDER, enqueue)
        fetched += 1
        edges += len(pairs)

    return {"fetched": fetched, "edges": edges, "failed": failed}


async def poll_subscriptions(db: Database, client: InvidiousClient, now: int) -> int:
    subs = await client.subscriptions()
    rows = []
    for sub in subs:
        ucid = sub.get("authorId")
        if isinstance(ucid, str) and ucid:
            rows.append((ucid, sub.get("author"), now))
    db.executemany(
        "INSERT INTO subscriptions (ucid, author, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(ucid) DO UPDATE SET"
        "   author = COALESCE(excluded.author, subscriptions.author),"
        "   updated_at = excluded.updated_at",
        rows,
    )
    db.set_meta("last_subscription_poll", str(now))
    return len(rows)


async def poll_feed(db: Database, client: InvidiousClient, now: int) -> int:
    stored = 0
    for page in range(1, FEED_PAGES + 1):
        payload = await client.feed(page, FEED_PAGE_SIZE)
        items = list(payload.get("videos") or []) + list(payload.get("notifications") or [])
        rows = []
        for raw in items:
            parsed = parse_video(raw, "feed")
            if not parsed:
                continue
            rows.append(parsed)
            db.execute(UPSERT_VIDEO, (
                parsed["video_id"], parsed["title"], parsed["ucid"], parsed["author"],
                parsed["length_seconds"], parsed["published"], parsed["views"],
                parsed["live_now"], parsed["premiere_timestamp"], "feed", now,
            ))
            channel = _channel_row(parsed["ucid"], parsed["author"], now)
            if channel:
                db.execute(UPSERT_CHANNEL, channel)
        if rows:
            db.executemany(
                "INSERT INTO feed_items (video_id, ucid, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(video_id) DO UPDATE SET"
                "   ucid = COALESCE(excluded.ucid, feed_items.ucid),"
                "   last_seen = excluded.last_seen",
                [(r["video_id"], r["ucid"], now, now) for r in rows],
            )
        stored += len(rows)
        if len(items) < FEED_PAGE_SIZE:
            break
    db.set_meta("last_feed_poll", str(now))
    return stored


async def run_cycle(
    db: Database, client: InvidiousClient, *, limit: int = MAX_ENQUEUE_PER_VIDEO
) -> dict[str, int]:
    """One pass: history -> related graph -> subscriptions -> subscription feed."""
    now = int(time.time())
    stats: dict[str, int] = {}
    try:
        stats["new_watches"] = await poll_history(db, client, now)
    except InvidiousError as exc:
        stats["history_error"] = 1
        log.warning("history poll failed: %s", exc)
    stats.update(await harvest_related(db, client, limit, now))
    try:
        stats["subscriptions"] = await poll_subscriptions(db, client, now)
    except InvidiousError as exc:
        stats["subscription_error"] = 1
        log.warning("subscription poll failed: %s", exc)
    try:
        stats["feed_items"] = await poll_feed(db, client, now)
    except InvidiousError as exc:
        stats["feed_error"] = 1
        log.warning("feed poll failed: %s", exc)
    db.set_meta("last_cycle", str(now))
    return stats


async def collector_loop(
    db: Database,
    client: InvidiousClient,
    *,
    history_seconds: int = 300,
    feed_seconds: int = 900,
    max_per_cycle: int = 20,
) -> None:
    """Fast loop for the watch graph, slow loop for the subscription feed."""
    last_feed = 0.0
    while True:
        started = time.monotonic()
        now = int(time.time())
        try:
            await poll_history(db, client, now)
        except InvidiousError as exc:
            log.warning("history poll failed: %s", exc)
        try:
            await harvest_related(db, client, max_per_cycle, now)
        except InvidiousError as exc:
            log.warning("related harvest failed: %s", exc)
        if now - last_feed >= feed_seconds:
            try:
                await poll_subscriptions(db, client, now)
                await poll_feed(db, client, now)
                last_feed = now
            except InvidiousError as exc:
                log.warning("subscription/feed poll failed: %s", exc)

        elapsed = time.monotonic() - started
        await asyncio.sleep(max(5.0, history_seconds - elapsed))
