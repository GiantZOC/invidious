"""Ranking and filtering behaviour, pinned with fixture data.

These are the weights referenced in the plan; changing one here is a deliberate
edit, not an accident.
"""

from app import ranking

NOW = 1_800_000_000  # 2027-01-01, arbitrary but fixed


def test_weights_sum_to_one():
    assert abs(sum(ranking.WEIGHTS.values()) - 1.0) < 1e-9


def test_cooccurrence_grows_with_reinforcing_sources():
    one = ranking.cooccurrence_score([1.0])
    three = ranking.cooccurrence_score([1.0, 1.0, 1.0])
    assert 0 < one < three < 1.0


def test_cooccurrence_prefers_recent_sources():
    recent = ranking.cooccurrence_score([1.0])
    stale = ranking.cooccurrence_score([400.0])
    assert recent > stale


def test_cooccurrence_of_nothing_is_zero():
    assert ranking.cooccurrence_score([]) == 0.0


def test_channel_affinity_uses_a_neutral_prior_when_unseen():
    assert ranking.channel_affinity_score() == ranking.CHANNEL_AFFINITY_PRIOR


def test_channel_affinity_rewards_watches():
    assert ranking.channel_affinity_score(watches=10) > ranking.channel_affinity_score(
        watches=1
    )


def test_channel_affinity_dislikes_outrank_watches():
    """More watches plus a dislike scores below fewer watches alone."""
    several_watches = ranking.channel_affinity_score(watches=4)
    disliked = ranking.channel_affinity_score(watches=5, dislikes=1)
    assert disliked < several_watches


def test_freshness_decays_and_handles_missing_dates():
    recent = ranking.freshness_score(NOW - 86400, NOW)
    old = ranking.freshness_score(NOW - 90 * 86400, NOW)
    assert recent > old > 0
    assert ranking.freshness_score(None, NOW) == 0.0


def test_novelty_favours_unsubscribed_and_unseen():
    assert ranking.novelty_score(False) > ranking.novelty_score(True)
    assert ranking.novelty_score(False, seen_count=3) < ranking.novelty_score(False)


def test_score_candidate_uses_weights():
    features = {"cooccurrence": 1.0, "channel_affinity": 1.0, "freshness": 1.0,
                "novelty": 1.0}
    assert abs(ranking.score_candidate(features) - 1.0) < 1e-9
    assert ranking.score_candidate({}) == 0.0


def test_rank_candidates_sorts_best_first():
    def candidate(vscore, video_id):
        return {
            "video_id": video_id,
            "published": NOW,
            "features": {
                "cooccurrence": vscore,
                "channel_affinity": 0.0,
                "freshness": 0.0,
                "novelty": 0.0,
            },
        }

    ranked = ranking.rank_candidates(
        [candidate(0.1, "a"), candidate(0.9, "b"), candidate(0.5, "c")]
    )
    assert [c["video_id"] for c in ranked] == ["b", "c", "a"]
    assert ranked[0]["score"] > ranked[-1]["score"]


# ---------------------------------------------------------------------------
# Smart feed filtering
# ---------------------------------------------------------------------------


def item(video_id="v1", **overrides):
    base = {
        "video_id": video_id,
        "title": "A normal video",
        "ucid": "UC1",
        "author": "Chan",
        "length_seconds": 600,
        "published": NOW - 3600,
        "views": 100,
        "live_now": 0,
        "premiere_timestamp": None,
    }
    base.update(overrides)
    return base


PREFS = {
    "hide_shorts": True,
    "hide_live": True,
    "hide_premieres": True,
    "unseen_only": True,
    "min_len": None,
    "max_len": None,
    "group": None,
    "muted_ucids": set(),
    "groups": {},
}


def keep(**overrides):
    prefs = dict(PREFS)
    extra = overrides.pop("prefs", {})
    prefs.update(extra)
    items = [item(**overrides)]
    return ranking.filter_feed(items, prefs=prefs, rules=(), watched_ids=(), now=NOW)


def test_plain_video_survives_filters():
    assert len(keep()) == 1


def test_short_is_hidden_when_configured():
    assert keep(length_seconds=30) == []
    assert len(keep(length_seconds=30, prefs={"hide_shorts": False})) == 1


def test_sixty_second_boundary_is_a_short():
    assert ranking.is_short({"length_seconds": 60}) is True
    assert ranking.is_short({"length_seconds": 61}) is False


def test_live_is_hidden_and_not_treated_as_short():
    assert keep(live_now=1, length_seconds=30) == []
    assert len(keep(live_now=1, length_seconds=30, prefs={"hide_live": False})) == 1
    assert ranking.is_short({"length_seconds": 30, "live_now": 1}) is False


def test_premiere_is_detected_by_future_timestamp():
    assert ranking.is_premiere({"premiere_timestamp": NOW + 3600}, NOW) is True
    assert ranking.is_premiere({"premiere_timestamp": NOW - 3600}, NOW) is False
    assert keep(premiere_timestamp=NOW + 3600) == []


def test_watched_is_hidden_only_with_unseen_only():
    items = [item(video_id="seen")]
    hidden = ranking.filter_feed(
        items, prefs=PREFS, rules=(), watched_ids={"seen"}, now=NOW
    )
    shown = ranking.filter_feed(
        items,
        prefs={**PREFS, "unseen_only": False},
        rules=(),
        watched_ids={"seen"},
        now=NOW,
    )
    assert hidden == []
    assert len(shown) == 1


def test_muted_channel_is_dropped():
    assert keep(ucid="UCmute", prefs={"muted_ucids": {"UCmute"}}) == []


def test_group_tab_keeps_only_group_members():
    prefs = {**PREFS, "group": "Music", "groups": {"Music": ["UCgood"]}}
    kept = ranking.filter_feed(
        [item(video_id="in", ucid="UCgood"), item(video_id="out", ucid="UCbad")],
        prefs=prefs,
        rules=(),
        watched_ids=(),
        now=NOW,
    )
    assert [row["video_id"] for row in kept] == ["in"]


def test_length_bounds():
    assert keep(length_seconds=100, prefs={"min_len": 300}) == []
    assert keep(length_seconds=10000, prefs={"max_len": 3600}) == []
    assert len(keep(length_seconds=600, prefs={"min_len": 300, "max_len": 3600})) == 1


def test_block_rule_drops_and_boost_rule_marks():
    rules = [
        {"kind": "block", "pattern": "trailer"},
        {"kind": "boost", "pattern": "lecture"},
    ]
    blocked = ranking.filter_feed(
        [item(title="New trailer!")], prefs=PREFS, rules=rules, watched_ids=(), now=NOW
    )
    boosted = ranking.filter_feed(
        [item(title="Guest lecture")], prefs=PREFS, rules=rules, watched_ids=(), now=NOW
    )
    assert blocked == []
    assert boosted[0]["boosted"] is True
    assert ranking.filter_feed(
        [item(title="Ordinary")], prefs=PREFS, rules=rules, watched_ids=(), now=NOW
    )[0]["boosted"] is False


def test_rule_matching_is_case_insensitive_substring():
    assert ranking.matches_term("NEW Trailer", "trailer") is True
    assert ranking.matches_term(None, "trailer") is False


def test_sort_by_recency_then_weighted_prefers_heavy_channels():
    old = item(video_id="old", published=NOW - 6 * 86400, ucid="UCfav")
    recent = item(video_id="recent", published=NOW - 3600, ucid="UCmeh")

    by_recency = ranking.sort_items([old, recent], mode="recency", now=NOW)
    assert [r["video_id"] for r in by_recency] == ["recent", "old"]

    by_weight = ranking.sort_items(
        [old, recent],
        mode="weighted",
        weights={"UCfav": 4.0, "UCmeh": 0.2},
        now=NOW,
    )
    assert [r["video_id"] for r in by_weight] == ["old", "recent"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_length():
    assert ranking.format_length(75) == "1:15"
    assert ranking.format_length(3723) == "1:02:03"
    assert ranking.format_length(None) == ""


def test_format_age():
    assert ranking.format_age(None, NOW) == ""
    assert ranking.format_age(NOW - 30, NOW) == "just now"
    assert ranking.format_age(NOW - 3600, NOW) == "1 hour ago"
    assert ranking.format_age(NOW - 7200, NOW) == "2 hours ago"
    assert ranking.format_age(NOW - 86400 * 3, NOW) == "3 days ago"
    assert ranking.format_age(NOW - 86400 * 400, NOW) == "1 year ago"


def test_format_views():
    assert ranking.format_views(0) == ""
    assert ranking.format_views(999) == "999 views"
    assert ranking.format_views(1500) == "1.5K views"
    assert ranking.format_views(2_400_000) == "2.4M views"
