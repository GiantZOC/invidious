"""Pure scoring and filtering for the smart feed and Discover page.

Every function here takes plain dicts, so ranking behaviour is pinned by the
fixture tests in ``tests/`` without a database or a network.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

# Discover score. Only signals the collector actually stores appear here.
WEIGHTS: dict[str, float] = {
    "cooccurrence": 0.40,
    "channel_affinity": 0.30,
    "freshness": 0.20,
    "novelty": 0.10,
}

CO_OCCURRENCE_HALF_LIFE_DAYS = 30.0
FRESHNESS_HALF_LIFE_DAYS = 14.0
# Number of reinforcing sources that scores 0.5 on co-occurrence.
CO_OCCURRENCE_SATURATION = 5.0
# Score for a channel the user has never watched from and never rated.
CHANNEL_AFFINITY_PRIOR = 0.5
NOVELTY_SUBSCRIBED_BASE = 0.3

SHORT_MAX_SECONDS = 60
DAY_SECONDS = 86400.0


def _decay(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 0.0
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def cooccurrence_score(source_ages_days: Sequence[float]) -> float:
    """Recency-weighted count of watched videos that pointed at this candidate."""
    raw = sum(_decay(a, CO_OCCURRENCE_HALF_LIFE_DAYS) for a in source_ages_days)
    if raw <= 0:
        return 0.0
    return raw / (raw + CO_OCCURRENCE_SATURATION)


def channel_affinity_score(
    watches: int = 0, likes: int = 0, dislikes: int = 0
) -> float:
    """How much the user's history says they like this channel, in 0..1.

    A dislike is weighted far above a like: one dismissal should move a channel
    more than a couple of views move it up.
    """
    positive = watches + 2 * likes
    negative = 4 * dislikes
    if positive == 0 and negative == 0:
        return CHANNEL_AFFINITY_PRIOR
    return positive / (positive + negative + 1)


def freshness_score(published: int | None, now: float) -> float:
    if not published:
        return 0.0
    return _decay((now - published) / DAY_SECONDS, FRESHNESS_HALF_LIFE_DAYS)


def novelty_score(subscribed: bool, seen_count: int = 0) -> float:
    base = NOVELTY_SUBSCRIBED_BASE if subscribed else 1.0
    return base / (1.0 + max(0, seen_count))


def score_candidate(
    features: Mapping[str, float], weights: Mapping[str, float] = WEIGHTS
) -> float:
    return sum(w * features.get(name, 0.0) for name, w in weights.items())


def rank_candidates(
    candidates: Iterable[dict[str, Any]],
    weights: Mapping[str, float] = WEIGHTS,
) -> list[dict[str, Any]]:
    """Attach ``score`` and ``features`` to each candidate, best first."""
    scored = []
    for candidate in candidates:
        features = candidate["features"]
        candidate["features"] = dict(features)
        candidate["score"] = score_candidate(features, weights)
        scored.append(candidate)
    scored.sort(key=lambda c: (c["score"], c.get("published") or 0), reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Smart feed
# ---------------------------------------------------------------------------


def matches_term(text: str | None, pattern: str) -> bool:
    if not text:
        return False
    return pattern.casefold() in text.casefold()


def rule_verdict(title: str | None, rules: Sequence[Mapping[str, Any]]) -> str | None:
    """Return 'block', 'boost', both-combined 'block+boost', or None."""
    blocks = [r for r in rules if r.get("kind") == "block"]
    boosts = [r for r in rules if r.get("kind") == "boost"]
    blocked = any(matches_term(title, r.get("pattern", "")) for r in blocks)
    boosted = any(matches_term(title, r.get("pattern", "")) for r in boosts)
    if blocked:
        return "block+boost" if boosted else "block"
    return "boost" if boosted else None


def is_short(item: Mapping[str, Any]) -> bool:
    length = item.get("length_seconds")
    return bool(length) and 0 < length <= SHORT_MAX_SECONDS and not item.get("live_now")


def is_live(item: Mapping[str, Any]) -> bool:
    return bool(item.get("live_now"))


def is_premiere(item: Mapping[str, Any], now: float) -> bool:
    stamp = item.get("premiere_timestamp")
    return bool(stamp) and stamp > now


def filter_feed(
    items: Sequence[Mapping[str, Any]],
    *,
    prefs: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any]] = (),
    watched_ids: Iterable[str] = (),
    now: float,
) -> list[dict[str, Any]]:
    """Apply the smart-feed settings, returning plain dicts in input order."""
    watched = set(watched_ids)
    muted = set(prefs.get("muted_ucids") or ())
    group = prefs.get("group")
    groups = prefs.get("groups") or {}
    min_len = prefs.get("min_len")
    max_len = prefs.get("max_len")
    out: list[dict[str, Any]] = []

    for item in items:
        ucid = item.get("ucid")
        verdict = rule_verdict(item.get("title"), rules)
        if verdict == "block":
            continue
        if ucid in muted:
            continue
        if group and group not in groups_for(ucid, groups):
            continue
        if prefs.get("hide_shorts") and is_short(item):
            continue
        if prefs.get("hide_live") and is_live(item):
            continue
        if prefs.get("hide_premieres") and is_premiere(item, now):
            continue
        if prefs.get("unseen_only") and item.get("video_id") in watched:
            continue

        length = item.get("length_seconds")
        if min_len and length is not None and length < min_len:
            continue
        if max_len and length is not None and length > max_len:
            continue

        row = dict(item)
        row["boosted"] = verdict in ("boost", "block+boost")
        out.append(row)
    return out


def groups_for(ucid: str | None, groups: Mapping[str, Sequence[str]]) -> set[str]:
    """Groups a channel belongs to; ungrouped channels are in 'All'."""
    result = {"All"}
    if not ucid:
        return result
    for name, members in groups.items():
        if ucid in members:
            result.add(name)
    return result


def sort_items(
    items: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    weights: Mapping[str, float] | None = None,
    now: float,
) -> list[dict[str, Any]]:
    """mode: 'recency' (newest published) or 'weighted' (channel weight x recency)."""
    rows = [dict(item) for item in items]
    if mode == "weighted":
        weights = weights or {}

        def key(row: Mapping[str, Any]) -> float:
            weight = weights.get(row.get("ucid"), 1.0)
            published = row.get("published") or 0
            age_days = max(0.0, (now - published) / DAY_SECONDS)
            # Channel weight shifts the recency curve, it does not override it.
            return weight * _decay(age_days, 7.0 * max(weight, 0.01))

        rows.sort(key=key, reverse=True)
    else:
        rows.sort(key=lambda r: (r.get("published") or 0), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def format_length(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


AGE_STEPS: tuple[tuple[str, int, float], ...] = (
    ("minute", 60, 60),
    ("hour", 24, 3600),
    ("day", 7, 86400),
    ("week", 4, 604800),
    ("month", 12, 2629800),
    ("year", 10**9, 31557600),
)


def format_age(published: int | None, now: float) -> str:
    if not published:
        return ""
    delta = max(0, int(now - published))
    if delta < 60:
        return "just now"
    count = 1
    label = "year"
    for label, limit, unit in AGE_STEPS:
        count = int(delta // unit)
        if count < limit:
            break
    count = max(1, count)
    return f"{count} {label}{'' if count == 1 else 's'} ago"


def format_views(views: int | None) -> str:
    if not views:
        return ""
    if views < 1000:
        return f"{views} views"
    value = float(views)
    for suffix in ("K", "M", "B"):
        value /= 1000.0
        if value < 1000:
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix} views"
    return f"{value:.0f}T views"


def template_context(now: float) -> dict[str, Callable]:
    """Jinja globals, kept here so templates and tests share one definition."""
    return {
        "format_length": format_length,
        "format_age": lambda published: format_age(published, now),
        "format_views": format_views,
    }
