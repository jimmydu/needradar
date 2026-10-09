"""Aggregation pipeline: cluster -> (optional) LLM merge arbitration -> score -> persist.

LLM usage policy: rules/keywords do all grouping; the LLM only arbitrates a
small batch of cross-bucket merge candidates (max ~30 pairs in ~3 calls).
"""
import json
import logging
import os
from datetime import date, datetime, timedelta

import requests

from aggregator import scoring
from aggregator.clustering import cluster_signals, cluster_tokens, find_merge_candidates
from storage import Cluster, Extraction, RawSignal

log = logging.getLogger(__name__)

MERGE_SYSTEM = """你是需求主题归并裁判。给定若干对候选主题（每对 A/B，各含关键词与代表性痛点），
判断它们是否描述同一个用户需求主题。只输出 JSON：{"merges": [{"pair": 序号, "merge": true/false}, ...]}。
判断标准：同一底层问题或同一付费场景才算同一主题；仅行业相同不算。"""


def _llm_configured():
    return bool(os.environ.get("OPENAI_API_KEY"))


def _chat_json(system, user, timeout=None):
    """Generic chat->JSON call, same endpoint conventions as extractor.llm."""
    api_key = os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    temperature = float(os.environ.get(
        "OPENAI_TEMPERATURE", "1" if model.startswith("kimi") else "0"))
    timeout = timeout or int(os.environ.get("OPENAI_TIMEOUT", "180"))
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        log.warning("LLM HTTP %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    body = resp.json()
    usage = body.get("usage") or {}
    return (json.loads(body["choices"][0]["message"]["content"]),
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def _cluster_brief(cluster, max_items=3):
    kws = [k for k, _ in cluster_tokens(cluster).most_common(8)]
    reps = []
    for s, e, _ in sorted(cluster, key=lambda m: -(m[1].p_level or 0)):
        text = (e.pain_point or s.title or "").strip()
        if text and text not in reps:
            reps.append(text[:150])
        if len(reps) >= max_items:
            break
    return kws, reps


def llm_arbitrate(clusters, pairs, usage):
    """Ask the LLM which candidate pairs to merge. Returns set of (i, j)."""
    merges = set()
    BATCH = 10
    for start in range(0, len(pairs), BATCH):
        batch = pairs[start:start + BATCH]
        lines = []
        for n, (i, j, _) in enumerate(batch, 1):
            ki, ri = _cluster_brief(clusters[i], 2)
            kj, rj = _cluster_brief(clusters[j], 2)
            lines.append(f"第{n}对\nA 关键词: {', '.join(ki)}; 痛点: {' / '.join(ri)}\n"
                         f"B 关键词: {', '.join(kj)}; 痛点: {' / '.join(rj)}")
        try:
            parsed, ptok, ctok = _chat_json(MERGE_SYSTEM, "\n\n".join(lines))
            usage["calls"] += 1
            usage["prompt_tokens"] += ptok
            usage["completion_tokens"] += ctok
            want = {m.get("pair") for m in parsed.get("merges", []) if m.get("merge")}
            for n, (i, j, _) in enumerate(batch, 1):
                if n in want:
                    merges.add((i, j))
        except Exception as e:
            log.warning("merge arbitration batch failed: %s", e)
    return merges


def apply_merges(clusters, merges):
    from aggregator.clustering import UnionFind
    uf = UnionFind(range(len(clusters)))
    for i, j in merges:
        uf.union(i, j)
    merged = {}
    for idx, c in enumerate(clusters):
        merged.setdefault(uf.find(idx), []).extend(c)
    return list(merged.values())


def load_window_members(session, weeks=4, window_end=None):
    window_end = window_end or datetime.utcnow()
    window_start = window_end - timedelta(weeks=weeks)
    rows = (session.query(RawSignal, Extraction)
            .join(Extraction, Extraction.signal_id == RawSignal.id)
            .all())
    members = []
    for s, e in rows:
        # text sources without an extracted pain point are noise for theming
        if s.source in ("appstore", "reddit_rss", "reddit") and not e.pain_point:
            continue
        ts = s.posted_at or s.fetched_at
        if ts and window_start <= ts.replace(tzinfo=None) <= window_end.replace(tzinfo=None):
            raw = json.loads(s.raw_json) if s.raw_json else {}
            members.append((s, e, raw))
    return members, window_start, window_end


def evidence_quotes(cluster, max_quotes=3):
    """Pick real evidence quotes (verbatim-checked at extraction time)."""
    quotes = []
    for s, e, _ in cluster:
        if not e.evidence_json:
            continue
        ev = json.loads(e.evidence_json)
        quote = ev.get("pain_point") or ev.get("urgency") or ""
        quote = quote.strip()
        if quote and quote not in quotes:
            quotes.append(quote)
        if len(quotes) >= max_quotes:
            break
    return quotes


def run(session, weeks=4, use_llm=True, dry_run=False, run_date=None):
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    run_date = run_date or date.today().isoformat()

    members, window_start, window_end = load_window_members(session, weeks)
    log.info("window %s..%s: %d signals", window_start.date(), window_end.date(), len(members))

    clusters = cluster_signals(members)
    log.info("pre-grouped into %d clusters", len(clusters))

    if use_llm and _llm_configured():
        pairs = find_merge_candidates(clusters)
        log.info("%d merge candidate pairs -> LLM arbitration", len(pairs))
        if pairs:
            merges = llm_arbitrate(clusters, pairs, usage)
            log.info("LLM approved %d merges", len(merges))
            clusters = apply_merges(clusters, merges)
    elif use_llm:
        log.warning("OPENAI_API_KEY not set; skipping merge arbitration")

    # score all clusters; candidate pool = P_max >= P3
    stats_all = [scoring.cluster_stats(c, None, window_end) for c in clusters]
    all_decayed = [st["decayed"] for st in stats_all if st["p_max"] and st["p_max"] >= 3]
    results = []
    for cluster, st in zip(clusters, stats_all):
        if not st["p_max"] or st["p_max"] < 3:
            continue
        wps = scoring.wps_score(st, all_decayed)
        conf = scoring.confidence_score(st)
        acc = scoring.accessibility_score(st)
        kws, reps = _cluster_brief(cluster)
        results.append({
            "cluster": cluster, "stats": st, **wps,
            "confidence": conf, "accessibility": acc,
            "final_score": round(wps["wps"] * conf * acc, 4),
            "name": "、".join(kws[:3]),
            "summary": reps[0] if reps else "、".join(kws[:3]),
            "keywords": kws,
            "quotes": evidence_quotes(cluster),
        })
    results.sort(key=lambda r: -r["final_score"])

    if not dry_run:
        session.query(Cluster).filter(Cluster.run_date == run_date).delete()
        for r in results:
            c = r["cluster"]
            st = r["stats"]
            session.add(Cluster(
                run_date=run_date,
                name=r["name"], summary=r["summary"],
                keywords_json=json.dumps(r["keywords"], ensure_ascii=False),
                member_ids=json.dumps([s.id for s, _, _ in c]),
                sources_json=json.dumps(dict(st["sources"]), ensure_ascii=False),
                p_dist_json=json.dumps({str(k): v for k, v in st["p_dist"].items()}),
                n_signals=st["n"], p_max=st["p_max"],
                median_amount=st["median_amount"],
                wps_p=r["wps_p"], wps_density=r["wps_density"],
                wps_diversity=r["wps_diversity"], wps_amount=r["wps_amount"],
                wps_trend=r["wps_trend"], wps_geo=r["wps_geo"], wps=r["wps"],
                confidence=r["confidence"], accessibility=r["accessibility"],
                final_score=r["final_score"],
                window_start=window_start, window_end=window_end,
            ))
        session.commit()
        log.info("persisted %d candidate clusters (run_date=%s)", len(results), run_date)

    return results, usage
