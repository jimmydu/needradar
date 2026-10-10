"""Aggregation pipeline: cluster -> (optional) LLM merge arbitration -> score -> persist.

LLM usage policy: rules/keywords do all grouping; the LLM only arbitrates a
small batch of cross-bucket merge candidates (max ~30 pairs in ~3 calls).
"""
import json
import logging
import os
from datetime import date, datetime, timedelta

import requests

from aggregator import barrier as barrier_mod
from aggregator import filtering, fit, scoring
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
    """cluster: (bucket_key, members)."""
    key, members = cluster
    kws = [k for k, _ in cluster_tokens(members).most_common(8)]
    reps = []
    for s, e, _ in sorted(members, key=lambda m: -(m[1].p_level or 0)):
        text = (e.pain_point or s.title or "").strip()
        if text and text not in reps:
            reps.append(text[:150])
        if len(reps) >= max_items:
            break
    topic = key[6:] if key.startswith("topic:") else None
    return kws, reps, topic


def llm_arbitrate(clusters, pairs, usage):
    """Ask the LLM which candidate pairs to merge. Returns set of (i, j)."""
    merges = set()
    BATCH = 10
    for start in range(0, len(pairs), BATCH):
        batch = pairs[start:start + BATCH]
        lines = []
        for n, (i, j, _) in enumerate(batch, 1):
            ki, ri, ti = _cluster_brief(clusters[i], 2)
            kj, rj, tj = _cluster_brief(clusters[j], 2)
            lines.append(f"第{n}对\nA 主题: {ti or '-'}; 关键词: {', '.join(ki)}; 痛点: {' / '.join(ri)}\n"
                         f"B 主题: {tj or '-'}; 关键词: {', '.join(kj)}; 痛点: {' / '.join(rj)}")
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
    for idx, (key, members) in enumerate(clusters):
        root = uf.find(idx)
        if root not in merged:
            merged[root] = [key, []]
        merged[root][1].extend(members)
        if not merged[root][0].startswith("topic:") and key.startswith("topic:"):
            merged[root][0] = key  # prefer the canonical topic as the cluster key
    return [(k, m) for k, m in merged.values()]


def load_window_members(session, weeks=4, window_end=None):
    window_end = window_end or datetime.utcnow()
    window_start = window_end - timedelta(weeks=weeks)
    rows = (session.query(RawSignal, Extraction)
            .join(Extraction, Extraction.signal_id == RawSignal.id)
            .all())
    members = []
    for s, e in rows:
        # text sources without an extracted pain point are noise for theming;
        # chart snapshots are market-direction evidence, not themes themselves
        if s.source == "appstore_charts":
            continue
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


def _fit_arbitrate(results, usage, batch=15):
    """LLM re-scores ambiguous software_fit values in place."""
    for start in range(0, len(results), batch):
        chunk = results[start:start + batch]
        lines = []
        for n, r in enumerate(chunk, 1):
            lines.append(f"{n}. 关键词: {', '.join(r['keywords'][:8])}｜"
                         f"代表痛点: {r['summary'][:120]}｜来源: {dict(r['stats']['sources'])}")
        try:
            parsed, ptok, ctok = _chat_json(fit.FIT_ARBITRATION_SYSTEM, "\n".join(lines))
            usage["calls"] += 1
            usage["prompt_tokens"] += ptok
            usage["completion_tokens"] += ctok
            by_id = {m.get("id"): m for m in parsed.get("fits", [])}
            for n, r in enumerate(chunk, 1):
                m = by_id.get(n)
                if not m:
                    continue
                try:
                    val = float(m.get("fit"))
                except (TypeError, ValueError):
                    continue
                if 0 <= val <= 1:
                    # guardrail: LLM may not raise the fit of gov non-IT clusters
                    # (VPL/prosthetics-type hardware keeps its rule score)
                    if r["fit_method"] in ("rule_gov_svc", "rule") \
                            and set(r["stats"]["sources"]) <= {"sam_gov", "usaspending"} \
                            and val > r["software_fit"]:
                        continue
                    r["software_fit"] = round(val, 3)
                    r["fit_method"] = "llm"
                    r["fit_note"] = m.get("note")
        except Exception as e:
            log.warning("fit arbitration batch failed: %s", e)


def _barrier_arbitrate(results, usage, batch=15):
    """LLM re-scores ambiguous barrier values in place."""
    for start in range(0, len(results), batch):
        chunk = results[start:start + batch]
        lines = []
        for n, r in enumerate(chunk, 1):
            lines.append(f"{n}. 关键词: {', '.join(r['keywords'][:8])}｜"
                         f"代表痛点: {r['summary'][:120]}｜当前门槛说明: {r['barrier_note']}")
        try:
            parsed, ptok, ctok = _chat_json(barrier_mod.BARRIER_ARBITRATION_SYSTEM,
                                            "\n".join(lines))
            usage["calls"] += 1
            usage["prompt_tokens"] += ptok
            usage["completion_tokens"] += ctok
            by_id = {m.get("id"): m for m in parsed.get("barriers", [])}
            for n, r in enumerate(chunk, 1):
                m = by_id.get(n)
                if not m:
                    continue
                try:
                    val = float(m.get("barrier"))
                except (TypeError, ValueError):
                    continue
                if 0 <= val <= 1:
                    r["barrier"] = round(val, 3)
                    r["barrier_method"] = "llm"
                    if m.get("note"):
                        r["barrier_note"] = m["note"]
        except Exception as e:
            log.warning("barrier arbitration batch failed: %s", e)


def _rule_entry_point(r):
    st = r["stats"]
    kw = r["keywords"][0] if r["keywords"] else r["summary"][:20]
    audience = None
    for _, e, _ in r["cluster"]:
        if e.audience:
            audience = e.audience
            break
    who = audience or "目标用户"
    if r["software_fit"] >= 0.7:
        return f"做面向{who}的「{kw}」工具/SaaS，先落地页预售验证"
    if r["software_fit"] >= 0.4:
        return f"围绕「{kw}」做信息聚合/自动化工具，软件为主、线下交付外包"
    if r["fit_method"] == "rule_gov_it":
        return f"「{kw}」政府 IT 方向：先做投标信息聚合/合规工具，承包需资质"
    return f"「{kw}」不适合纯软件切入，可做比价/投标信息聚合等周边工具"


def _llm_entry_points(results, usage, batch=25):
    for start in range(0, len(results), batch):
        chunk = results[start:start + batch]
        lines = []
        for n, r in enumerate(chunk, 1):
            lines.append(f"{n}. 关键词: {', '.join(r['keywords'][:8])}｜"
                         f"代表痛点: {r['summary'][:120]}｜"
                         f"来源: {dict(r['stats']['sources'])}｜代码可行性: {r['software_fit']}｜"
                         f"门槛: {r['barrier']}（{r['barrier_note']}）")
        try:
            parsed, ptok, ctok = _chat_json(fit.ENTRY_POINT_SYSTEM, "\n".join(lines))
            usage["calls"] += 1
            usage["prompt_tokens"] += ptok
            usage["completion_tokens"] += ctok
            by_id = {m.get("id"): m.get("entry") for m in parsed.get("entries", [])}
            for n, r in enumerate(chunk, 1):
                if by_id.get(n):
                    r["entry_point"] = by_id[n]
        except Exception as e:
            log.warning("entry point batch failed: %s", e)


def run(session, weeks=4, use_llm=True, dry_run=False, run_date=None):
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    run_date = run_date or date.today().isoformat()

    members, window_start, window_end = load_window_members(session, weeks)
    log.info("window %s..%s: %d signals", window_start.date(), window_end.date(), len(members))

    clusters = cluster_signals(members)
    n_topic = sum(1 for k, _ in clusters if k.startswith("topic:"))
    log.info("pre-grouped into %d clusters (%d canonical topics, %d residual)",
             len(clusters), n_topic, len(clusters) - n_topic)

    if use_llm and _llm_configured():
        # merge arbitration only applies to residual (non-topic) clusters
        topic_clusters = [c for c in clusters if c[0].startswith("topic:")]
        residuals = [c for c in clusters if not c[0].startswith("topic:")]
        pairs = find_merge_candidates(residuals)
        log.info("%d merge candidate pairs (residual only) -> LLM arbitration", len(pairs))
        if pairs:
            merges = llm_arbitrate(residuals, pairs, usage)
            log.info("LLM approved %d merges", len(merges))
            residuals = apply_merges(residuals, merges)
        clusters = topic_clusters + residuals
    elif use_llm:
        log.warning("OPENAI_API_KEY not set; skipping merge arbitration")

    # score all clusters; candidate pool = P_max >= P3
    stats_all = [scoring.cluster_stats(m, None, window_end) for _, m in clusters]
    all_decayed = [st["decayed"] for st in stats_all if st["p_max"] and st["p_max"] >= 3]
    results = []
    for (key, members), st in zip(clusters, stats_all):
        if not st["p_max"] or st["p_max"] < 3:
            continue
        wps = scoring.wps_score(st, all_decayed)
        conf = scoring.confidence_score(st)
        acc = scoring.accessibility_score(st)
        fit_val, fit_method, fit_note = fit.rule_software_fit(members, st)
        barrier_val, barrier_method, barrier_note = barrier_mod.rule_barrier(members, st)
        kws, reps, topic = _cluster_brief((key, members))
        results.append({
            "cluster": members, "topic": topic, "stats": st, **wps,
            "confidence": conf, "accessibility": acc,
            "software_fit": fit_val, "fit_method": fit_method, "fit_note": fit_note,
            "barrier": barrier_val, "barrier_method": barrier_method,
            "barrier_note": barrier_note,
            "final_score": round(wps["wps"] * conf * acc * fit_val * barrier_val, 4),
            "name": topic or "、".join(kws[:3]),
            "summary": (f"{topic}：{reps[0]}" if topic and reps else
                        (topic or (reps[0] if reps else "、".join(kws[:3])))),
            "keywords": kws,
            "quotes": evidence_quotes(members),
        })

    # LLM arbitration for ambiguous clusters near the top (cost-capped)
    if use_llm and _llm_configured():
        results.sort(key=lambda r: -(r["wps"] * r["confidence"] * r["accessibility"]))
        ambiguous = [r for r in results[:120] if 0.3 < r["software_fit"] < 0.65]
        _fit_arbitrate(ambiguous, usage)
        ambiguous_b = [r for r in results[:120] if 0.45 < r["barrier"] < 0.95]
        _barrier_arbitrate(ambiguous_b, usage)
        for r in results:
            r["final_score"] = round(r["wps"] * r["confidence"] * r["accessibility"]
                                     * r["software_fit"] * r["barrier"], 4)

    # hard filters: after scoring, before ranking
    survivors, filtered = filtering.apply_filters(
        results, usage, use_llm and _llm_configured(), _chat_json)
    log.info("filters: %d candidates -> %d survivors, %d filtered",
             len(results), len(survivors), len(filtered))

    # entry points: rule template for survivors, LLM polish for the visible top
    survivors.sort(key=lambda r: -r["final_score"])
    for r in survivors:
        r["entry_point"] = _rule_entry_point(r)
    if use_llm and _llm_configured():
        _llm_entry_points(survivors[:50], usage)

    if not dry_run:
        session.query(Cluster).filter(Cluster.run_date == run_date).delete()
        for r in results:
            members = r["cluster"]
            st = r["stats"]
            session.add(Cluster(
                run_date=run_date,
                name=r["name"], summary=r["summary"],
                keywords_json=json.dumps(r["keywords"], ensure_ascii=False),
                member_ids=json.dumps([s.id for s, _, _ in members]),
                sources_json=json.dumps(dict(st["sources"]), ensure_ascii=False),
                p_dist_json=json.dumps({str(k): v for k, v in st["p_dist"].items()}),
                n_signals=st["n"], p_max=st["p_max"],
                median_amount=st["median_amount"],
                wps_p=r["wps_p"], wps_density=r["wps_density"],
                wps_diversity=r["wps_diversity"], wps_amount=r["wps_amount"],
                wps_trend=r["wps_trend"], wps_geo=r["wps_geo"], wps=r["wps"],
                confidence=r["confidence"], accessibility=r["accessibility"],
                final_score=r["final_score"],
                software_fit=r["software_fit"], fit_method=r["fit_method"],
                entry_point=r.get("entry_point"),
                barrier=r["barrier"], barrier_note=r["barrier_note"],
                filtered_reason=r["filtered_reason"],
                window_start=window_start, window_end=window_end,
            ))
        session.commit()
        log.info("persisted %d candidate clusters (run_date=%s)", len(results), run_date)

    return survivors, filtered, usage
