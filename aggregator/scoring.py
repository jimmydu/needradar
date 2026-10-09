"""WPS / confidence / accessibility scoring, per PRD §9.2–9.5 and §4.4 decay.

All components normalized to [0, 1]. Final = WPS × confidence × accessibility.
"""
import math
from collections import Counter
from statistics import median

P_STRENGTH = {5: 0.85, 4: 0.70, 3: 0.55, 2: 0.35}
WEEK_DECAY = [1.0, 0.85, 0.7, 0.6]  # week 1 (most recent) .. week 4

GOV_SOURCES = {"sam_gov", "usaspending"}
CONSUMER_SOURCES = {"appstore", "reddit_rss", "reddit", "cfpb", "freelancer", "manual"}
BARRIER_KEYWORDS = {
    "license", "licensed", "permit", "medical", "healthcare", "doctor", "legal",
    "attorney", "law", "government", "federal", "bank", "banking", "insurance",
    "mortgage", "lender", "牌照", "资质", "医疗", "法律", "政府", "银行", "保险",
}


def week_index(dt, window_end):
    """0..3 for the 4 rolling weeks ending at window_end; None if outside."""
    if not dt:
        return None
    days = (window_end.date() - dt.date()).days
    if days < 0 or days >= 28:
        return None
    return min(days // 7, 3)


def decayed_count(week_indices):
    return sum(WEEK_DECAY[w] for w in week_indices if w is not None)


def cluster_stats(cluster, extractions, window_end):
    """Aggregate the raw inputs needed for scoring."""
    p_levels = [e.p_level for _, e, _ in cluster if e.p_level]
    p_max = max(p_levels) if p_levels else None
    weeks = [week_index(s.posted_at or s.fetched_at, window_end) for s, _, _ in cluster]
    sources = Counter(s.source for s, _, _ in cluster)
    amounts = [s.amount for s, _, _ in cluster if s.amount]
    states = [s.state for s, _, _ in cluster if s.state]
    n = len(cluster)
    return {
        "n": n,
        "p_max": p_max,
        "p_dist": Counter(p_levels),
        "decayed": decayed_count(weeks),
        "weeks_present": len({w for w in weeks if w is not None}),
        "sources": sources,
        "n_sources": len(sources),
        "amounts": amounts,
        "median_amount": median(amounts) if amounts else None,
        "n_with_amount": sum(1 for s, e, _ in cluster if s.amount or e.cost_hint),
        "states": states,
        "n_rule_p3": sum(1 for _, e, _ in cluster if e.p_level and e.p_level >= 3 and e.p_method == "rule"),
        "text": " ".join((e.pain_point or s.title or "") for s, e, _ in cluster).lower(),
    }


def wps_score(stats, all_decayed):
    p = P_STRENGTH.get(stats["p_max"], 0.0)

    med = median(all_decayed) if all_decayed else 0
    density = min(stats["decayed"] / med, 1.0) if med > 0 else 0.0

    diversity = min(stats["n_sources"] / 5, 1.0)

    if stats["median_amount"]:
        frac = stats["n_with_amount"] / stats["n"]
        norm = min(math.log10(max(stats["median_amount"], 1)) / 6, 1.0)  # $1M -> 1.0
        amount = frac * norm
    else:
        amount = 0.0

    trend = max(stats["weeks_present"], 1) / 4  # new theme = 0.25

    coverage = len(stats["states"]) / stats["n"]
    if coverage < 0.3:
        geo = 0.5  # not enough state data -> treat as national
    else:
        cnt = Counter(stats["states"])
        total = sum(cnt.values())
        geo = sum((c / total) ** 2 for c in cnt.values())

    wps = 0.35 * p + 0.25 * density + 0.15 * diversity + 0.10 * amount + 0.10 * trend + 0.05 * geo
    return {
        "wps_p": round(p, 4), "wps_density": round(density, 4),
        "wps_diversity": round(diversity, 4), "wps_amount": round(amount, 4),
        "wps_trend": round(trend, 4), "wps_geo": round(geo, 4),
        "wps": round(wps, 4),
    }


def confidence_score(stats):
    time_consistency = 1.0 if stats["weeks_present"] >= 2 else 0.5
    conf = (0.4 * min(stats["n"] / 10, 1)
            + 0.3 * min(stats["n_sources"] / 3, 1)
            + 0.3 * time_consistency
            + min(0.05 * stats["n_rule_p3"], 0.15))
    return round(min(conf, 1.0), 4)


def accessibility_score(stats):
    srcs = set(stats["sources"])
    if srcs and srcs <= GOV_SOURCES:
        reach = 0.2
    elif srcs & CONSUMER_SOURCES:
        reach = 1.0
    else:
        reach = 0.5

    amt = stats["median_amount"]
    if amt is None:
        deal = 0.3
    elif 1_000 <= amt <= 50_000:
        deal = 1.0
    elif 50_000 < amt <= 500_000:
        deal = 0.6
    else:
        deal = 0.3

    barrier = 0.3 if any(k in stats["text"] for k in BARRIER_KEYWORDS) else 1.0
    acc = 0.5 * reach + 0.3 * deal + 0.2 * barrier
    return round(acc, 4)
