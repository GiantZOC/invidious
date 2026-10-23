"""Collector: parsing, diffing, and the storage side effects.

The fixtures below mirror the field names Invidious actually emits
(src/invidious/jsonify/api_v1/video_json.cr): ``viewCount`` on a video,
``published`` as epoch seconds at the top level and RFC 3339 inside
``recommendedVideos``.
"""

import asyncio

import httpx
import pytest
import respx

from app import collector
from app.db import Database
from app.invidious import InvidiousClient

NOW = 1_800_000_000
BASE = "http://invidious.test"


def video(video_id="vid1", published=NOW - 7200, related=None, **overrides):
    payload = {
        "title": f"Title {video_id}",
        "videoId": video_id,
        "description": "desc",
        "published": published,
        "viewCount": 1234,
        "author": "Chan",
        "authorId": "UC1",
        "lengthSeconds": 600,
        "liveNow": False,
        "recommendedVideos": related if related is not None else [],
    }
    payload.update(overrides)
    return payload


def related(video_id="rel1", published="2026-10-01T12:00:00Z"):
    return {
        "videoId": video_id,
        "title": f"Related {video_id}",
        "author": "Other",
        "authorId": "UC2",
        "authorUrl": "/channel/UC2",
        "lengthSeconds": 480,
        "published": published,
    }


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "sidecar.db"))
    database.connect()
    yield database
    database.close()


def client(token="tok"):
    return InvidiousClient(BASE, token, min_interval=0)


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


def test_session_id_is_sent_as_the_sid_cookie():
    sid = "a" * 43 + "="
    assert client(sid)._headers() == {"Cookie": f"SID={sid}"}


def test_api_token_is_sent_as_bearer():
    raw = '{"session":"v1:x","expire":1,"scopes":[":*"],"signature":"s"}'
    assert client(raw)._headers() == {"Authorization": f"Bearer {raw}"}
    encoded = "%7B%22session%22%3A%22v1%3Ax%22%7D"
    assert client(encoded)._headers() == {"Authorization": f"Bearer {encoded}"}


def test_no_token_sends_no_auth():
    assert client("")._headers() == {}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_timestamp_accepts_epoch_and_iso():
    assert collector.parse_timestamp(1_700_000_000) == 1_700_000_000
    assert collector.parse_timestamp("2026-10-01T12:00:00Z") == 1_790_856_000
    assert collector.parse_timestamp("2026-10-01T12:00:00+00:00") == 1_790_856_000
    assert collector.parse_timestamp("1700000000") == 1_700_000_000


def test_parse_timestamp_rejects_junk():
    assert collector.parse_timestamp(None) is None
    assert collector.parse_timestamp("") is None
    assert collector.parse_timestamp("not a date") is None
    assert collector.parse_timestamp(True) is None


def test_parse_video_maps_invidious_field_names():
    parsed = collector.parse_video(video(video_id="abc"), "watch")
    assert parsed["video_id"] == "abc"
    assert parsed["ucid"] == "UC1"
    assert parsed["views"] == 1234
    assert parsed["length_seconds"] == 600
    assert parsed["published"] == NOW - 7200
    assert parsed["live_now"] == 0


def test_parse_video_needs_an_id():
    assert collector.parse_video({"title": "no id"}, "watch") is None
    assert collector.parse_video({}, "watch") is None


def test_related_published_is_iso_epoch():
    items = collector.related_of(video(related=[related()]))
    assert len(items) == 1
    assert items[0]["video_id"] == "rel1"
    assert items[0]["published"] == 1_790_856_000
    assert items[0]["source"] == "related"


def test_related_tolerates_missing_keys():
    assert collector.related_of({}) == []
    assert collector.related_of({"recommendedVideos": [{"title": "idless"}]}) == []


def test_diff_new_ids_keeps_order_and_dedupes():
    assert collector.diff_new_ids(["a", "b", "a", "c"], ["b"]) == ["a", "c"]
    assert collector.diff_new_ids([], ["a"]) == []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_store_watches_backfill_stamps_unknown_date(db):
    collector.store_watches(db, ["a", "b"], NOW, "backfill")
    rows = {r["video_id"]: r for r in db.query("SELECT * FROM watches")}
    assert rows["a"]["first_seen_at"] == 0
    assert rows["a"]["source"] == "backfill"
    queued = {r["video_id"] for r in db.query("SELECT video_id FROM videos WHERE fetched_at = 0")}
    assert queued == {"a", "b"}


def test_store_watches_is_idempotent(db):
    collector.store_watches(db, ["a"], NOW, "history")
    collector.store_watches(db, ["a"], NOW + 600, "history")
    assert db.scalar("SELECT COUNT(*) FROM watches", default=0) == 1
    assert db.scalar("SELECT first_seen_at FROM watches WHERE video_id='a'", default=0) == NOW


def test_store_watches_records_real_timestamp(db):
    collector.store_watches(db, ["a"], NOW, "history")
    assert db.scalar("SELECT first_seen_at FROM watches WHERE video_id='a'", default=0) == NOW


# ---------------------------------------------------------------------------
# Networked steps (respx)
# ---------------------------------------------------------------------------


def test_poll_history_backfills_then_deltas(db):
    page = ["v1", "v2", "v3"]
    client_ = client()

    # side_effect, not return_value: respx serialises the body when the mock is
    # built, so a mutated list would otherwise be invisible to later calls.
    def next_page(request):
        return httpx.Response(200, json=list(page))

    with respx.mock(base_url=BASE) as router:
        route = router.get("/api/v1/auth/history").mock(side_effect=next_page)
        assert asyncio.run(collector.poll_history(db, client_, NOW)) == 3
        assert db.get_meta("history_backfilled") == "1"
        rows = {r["video_id"]: r for r in db.query("SELECT * FROM watches")}
        assert all(r["first_seen_at"] == 0 for r in rows.values())

        page.append("v4")
        assert asyncio.run(collector.poll_history(db, client_, NOW + 300)) == 1
        assert db.scalar(
            "SELECT first_seen_at FROM watches WHERE video_id='v4'", default=0
        ) == NOW + 300
        assert db.scalar("SELECT COUNT(*) FROM watches", default=0) == 4
        assert route.call_count >= 2
    asyncio.run(client_.aclose())


def test_poll_history_stops_at_a_short_page(db):
    client_ = client()
    pages = [["a", "b"], ["c"], []]

    def next_page(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[min(page - 1, len(pages) - 1)])

    with respx.mock(base_url=BASE) as router:
        route = router.get("/api/v1/auth/history").mock(side_effect=next_page)
        # HISTORY_PAGE_SIZE is 100, so a 2-item page is the last one.
        assert asyncio.run(collector.poll_history(db, client_, NOW)) == 2
        assert route.call_count == 1
    asyncio.run(client_.aclose())


def test_harvest_related_records_video_edges_and_queue(db):
    collector.store_watches(db, ["v1"], NOW, "history")
    client_ = client()
    payload = video(video_id="v1", related=[related("r1"), related("r2"), related("r3")])

    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/videos/v1").mock(return_value=httpx.Response(200, json=payload))
        stats = asyncio.run(collector.harvest_related(db, client_, 10, NOW + 10))

    assert stats["fetched"] == 1
    assert stats["edges"] == 3
    assert db.scalar("SELECT COUNT(*) FROM edges", default=0) == 3
    assert db.scalar(
        "SELECT fetched_at FROM videos WHERE video_id='v1'", default=0
    ) == NOW + 10
    assert db.scalar(
        "SELECT title FROM videos WHERE video_id='r1'", default=""
    ) == "Related r1"
    # Related videos with a publish date need no extra request to render.
    assert db.scalar("SELECT fetched_at FROM videos WHERE video_id='r1'", default=-1) == NOW + 10
    assert db.scalar("SELECT COUNT(*) FROM channels", default=0) == 2


def test_harvest_related_queues_undated_related_videos(db):
    collector.store_watches(db, ["v1"], NOW, "history")
    client_ = client()
    payload = video(video_id="v1", related=[related("r1", published="")])

    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/videos/v1").mock(return_value=httpx.Response(200, json=payload))
        asyncio.run(collector.harvest_related(db, client_, 10, NOW + 10))

    assert db.scalar(
        "SELECT fetched_at FROM videos WHERE video_id='r1'", default=-1
    ) == 0


def test_harvest_related_leaves_failed_video_queued(db):
    collector.store_watches(db, ["v1"], NOW, "history")
    client_ = client()
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/videos/v1").mock(return_value=httpx.Response(500, json={}))
        stats = asyncio.run(collector.harvest_related(db, client_, 10, NOW + 10))
    assert stats["failed"] == 1
    assert stats["fetched"] == 0
    assert collector.videos_to_fetch(db, 10) == ["v1"]
    asyncio.run(client_.aclose())


def test_harvest_respects_the_cycle_limit(db):
    collector.store_watches(db, ["v1", "v2", "v3"], NOW, "history")
    client_ = client()

    def echo(request):
        return httpx.Response(
            200, json=video(request.url.path.rsplit("/", 1)[-1])
        )

    with respx.mock(base_url=BASE) as router:
        router.get(path__regex=r"/api/v1/videos/.*").mock(side_effect=echo)
        stats = asyncio.run(collector.harvest_related(db, client_, 2, NOW + 10))
    assert stats["fetched"] == 2
    assert len(collector.videos_to_fetch(db, 10)) == 1


def test_poll_subscriptions(db):
    client_ = client()
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/auth/subscriptions").mock(
            return_value=httpx.Response(
                200, json=[{"author": "Chan", "authorId": "UC1"}, {"authorId": ""}]
            )
        )
        assert asyncio.run(collector.poll_subscriptions(db, client_, NOW)) == 1
    assert db.scalar("SELECT author FROM subscriptions WHERE ucid='UC1'", default="") == "Chan"


def test_poll_feed_stores_items_and_metadata(db):
    client_ = client()
    payload = {
        "videos": [video(video_id="f1")],
        "notifications": [video(video_id="f2", liveNow=True)],
    }
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/auth/feed").mock(return_value=httpx.Response(200, json=payload))
        stored = asyncio.run(collector.poll_feed(db, client_, NOW))
    assert stored == 2
    assert db.scalar("SELECT COUNT(*) FROM feed_items", default=0) == 2
    assert db.scalar("SELECT live_now FROM videos WHERE video_id='f2'", default=-1) == 1


def test_run_cycle_reports_stats_and_survives_failures(db):
    client_ = client()
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/auth/history").mock(
            return_value=httpx.Response(200, json=["v1"])
        )
        router.get("/api/v1/videos/v1").mock(
            return_value=httpx.Response(200, json=video(related=[related()]))
        )
        router.get("/api/v1/auth/subscriptions").mock(
            return_value=httpx.Response(200, json=[{"author": "Chan", "authorId": "UC1"}])
        )
        router.get("/api/v1/auth/feed").mock(
            return_value=httpx.Response(200, json={"videos": [video("f1")], "notifications": []})
        )
        stats = asyncio.run(collector.run_cycle(db, client_, limit=5))

    assert stats["new_watches"] == 1
    assert stats["fetched"] == 1
    assert stats["subscriptions"] == 1
    assert stats["feed_items"] == 1
    assert (db.get_meta("last_cycle") or "").isdigit()


def test_run_cycle_tolerates_history_outage(db):
    client_ = client()
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/auth/history").mock(return_value=httpx.Response(401, json={}))
        router.get("/api/v1/auth/subscriptions").mock(return_value=httpx.Response(200, json=[]))
        router.get("/api/v1/auth/feed").mock(
            return_value=httpx.Response(200, json={"videos": [], "notifications": []})
        )
        stats = asyncio.run(collector.run_cycle(db, client_, limit=5))
    assert stats.get("history_error") == 1
    asyncio.run(client_.aclose())
