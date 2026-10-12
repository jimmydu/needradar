"""Extraction pipeline: rule P-level for everything, LLM semantics for
text-bearing sources under a per-source cost policy.
"""
import json
import logging
import os
import time
from datetime import datetime

from extractor import llm, rules
from storage import Extraction, RawSignal

log = logging.getLogger(__name__)

# LLM policy per source: "always" | "low_rated" (appstore <=3 stars) | "never"
LLM_POLICY = {
    "sam_gov": "never",        # title only, description is a link
    "usaspending": "never",    # title only, huge volume
    "cfpb": "never",           # structured title only, narratives discontinued
    "appstore_charts": "never",  # ranking rows, no text
    "appstore": "low_rated",   # cost cap: only reviews rated <=3
    "reddit_rss": "always",
    "reddit": "always",
    "freelancer": "always",
    "manual": "always",
    "hackernews": "always",  # Ask HN 求助帖/Show HN 抱怨
    "discord_ao": "always",  # Discord 镜像 thread（痛点链路）
    "targeted_review": "always",  # 已预筛 ≤3 星
    # 行业研究/数据平台方向的新源：规则足够（金额/票数/热度），LLM 只给
    # stackexchange（问题正文有真痛点文本）；其余一律规则
    "stackexchange": "always",
    "github_issues": "always",   # 高赞痛点 issue 走痛点链路（LLM 抽语义）
    "github_rising": "never",    # 成功样本：结构化字段自解释
    "freelancer_research": "never",
    "google_trends": "never",
    "kaggle": "never",
    "kaggle_requests": "never",  # 请求帖标题自解释，规则 P3 即可
    "hf_datasets": "never",
    # 众筹/新闻: 规则已能定级（P5 金额、触发事件标签），标题/简介直接进聚类，
    # LLM 语义抽取对榜单边际收益低（e4b 每条 ~18s，全量 Kickstarter 需 5+ 小时）
    "kickstarter": "never",
    "indiegogo": "never",
    "news": "never",
    "youtube": "never",
    # 成功样本/JD：结构化字段自解释，不进 LLM
    "appstore_indie": "never",
    "gplay_indie": "never",
    "shopify_apps": "never",
    "betalist": "never",
    "remoteok": "never", "remotive": "never", "weworkremotely": "never",
}


def _needs_llm(signal):
    policy = LLM_POLICY.get(signal.source, "never")
    if policy == "always":
        return bool(signal.title or signal.description)
    if policy == "low_rated":
        return signal.rating is not None and signal.rating <= 3
    return False


def candidates(session, source=None, limit=None, force=False):
    q = session.query(RawSignal)
    if source:
        q = q.filter(RawSignal.source == source)
    if not force:
        done = session.query(Extraction.signal_id).subquery()
        q = q.filter(~RawSignal.id.in_(done))
    q = q.order_by(RawSignal.id)
    if limit:
        q = q.limit(limit)
    return q.all()


MA_SYSTEM = """你是并购新闻信息抽取器。从新闻标题/摘要中抽取三要素，输出 JSON：
{"acquirer": "收购方公司名（未知为空字符串）",
 "acquiree": "被收购方/产品名（未知为空）",
 "domain": "被收购方所在领域/产品形态（如 客服SaaS/招聘平台/数据分析，未知为空）"}
只输出 JSON，禁止编造。"""

FUNDING_SYSTEM = """你是融资新闻信息抽取器。从新闻标题/摘要中抽取四要素，输出 JSON：
{"company": "融资公司名（未知为空字符串）",
 "round": "轮次（如 Seed/Series A/B/C/并购/未知）",
 "amount_usd": "金额数字（美元，如 25000000；未知为 null）",
 "domain": "所在领域/产品形态（如 客服SaaS/机器人/医疗，未知为空）"}
只输出 JSON，禁止编造。金额注意换算 million/billion 为纯数字。"""


def _extract_fields(signal, system, timeout=None):
    import requests as rq
    from extractor import llm as llm_mod
    cfg = llm_mod.resolve_config("light")
    if not cfg:
        return None
    base, model, api_key = cfg
    timeout = timeout or int(os.environ.get("OPENAI_TIMEOUT", "180"))
    resp = rq.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"标题: {signal.title or ''}\n摘要: {(signal.description or '')[:1000]}"},
            ],
            "temperature": llm_mod.default_temperature(model),
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    return json.loads(body["choices"][0]["message"]["content"])


def extract_ma(signal, timeout=None):
    """M&A 3-field extraction for trigger_type='并购' news signals."""
    return _extract_fields(signal, MA_SYSTEM, timeout)


def extract_funding(signal, timeout=None):
    """融资四要素抽取 for trigger_type='融资' news signals."""
    return _extract_fields(signal, FUNDING_SYSTEM, timeout)


def extract_batch(session, source=None, limit=None, force=False, dry_run=False, use_llm=True):
    from extractor import prefilter
    stats = {"total": 0, "rule_p": 0, "llm_ok": 0, "llm_fail": 0, "no_p": 0,
             "skipped": 0, "prompt_tokens": 0, "completion_tokens": 0}
    signals = candidates(session, source, limit, force)
    stats["total"] = len(signals)

    for sig in signals:
        skip = prefilter.skip_reason(sig)
        text = " ".join(filter(None, [sig.title, sig.description]))
        rec = {
            "signal_id": sig.id,
            "p_level": rules.rule_p_level(sig),
            "p_method": "rule" if rules.rule_p_level(sig) else "none",
            "trigger_type": rules.detect_trigger(text) if sig.source in ("news", "techcrunch") else None,
            "cost_hint": rules.rule_cost_hint(sig),
            "audience": None, "scenario": None, "pain_point": None,
            "urgency": None, "current_solution": None, "alternatives": None,
            "supply_gap": None, "evidence_json": None, "model": None,
            "prompt_tokens": 0, "completion_tokens": 0, "llm_raw": None,
            "ma_json": None,
            "skip_reason": skip,
            "extracted_at": datetime.utcnow(),
        }
        if rec["p_level"]:
            stats["rule_p"] += 1
        else:
            stats["no_p"] += 1

        if skip:
            stats["skipped"] += 1
        elif (rec["trigger_type"] or "").startswith(("并购", "融资")) and use_llm and not dry_run:
            # 并购三要素 / 融资四要素抽取（题材雷达方向验证信号，量小成本低）
            try:
                is_ma = rec["trigger_type"].startswith("并购")
                fields = extract_ma(sig) if is_ma else extract_funding(sig)
                key_field = "acquiree" if is_ma else "company"
                if fields and (fields.get("acquirer") or fields.get(key_field)):
                    rec["ma_json"] = json.dumps(fields, ensure_ascii=False)
                    stats["ma_ok"] = stats.get("ma_ok", 0) + 1
                    if is_ma and fields.get("acquiree"):
                        from storage import Watchlist
                        session.add(Watchlist(
                            name=fields["acquiree"], acquirer=fields.get("acquirer") or None,
                            domain=fields.get("domain") or None,
                            source_signal_id=sig.id,
                            note="被收购产品余波监控：用户抱怨涨价/停更/关停是高价值痛点"))
                time.sleep(0.2)
            except Exception as e:
                log.warning("MA extract failed for signal %d: %s", sig.id, e)
                stats["llm_fail"] += 1
        elif use_llm and _needs_llm(sig) and not dry_run:
            try:
                fields, evidence, model, ptok, ctok, raw = llm.extract_semantics(sig)
                rec.update({
                    "pain_point": fields["pain_point"] or None,
                    "audience": fields["audience"] or None,
                    "scenario": fields["scenario"] or None,
                    "urgency": fields["urgency"] or None,
                    "current_solution": fields["current_solution"] or None,
                    "alternatives": fields["alternatives"] or None,
                    "supply_gap": fields["supply_gap"] or None,
                    "evidence_json": json.dumps(evidence, ensure_ascii=False),
                    "model": model,
                    "prompt_tokens": ptok,
                    "completion_tokens": ctok,
                    "llm_raw": raw,
                })
                stats["llm_ok"] += 1
                stats["prompt_tokens"] += ptok
                stats["completion_tokens"] += ctok
                time.sleep(0.2)
            except llm.LLMNotConfigured:
                log.warning("OPENAI_API_KEY not set; LLM extraction skipped")
                break
            except Exception as e:
                log.warning("LLM failed for signal %d: %s", sig.id, e)
                stats["llm_fail"] += 1

        if dry_run:
            log.info("[dry-run] #%d %s/%s -> P%s (%s) cost_hint=%s",
                     sig.id, sig.source, sig.signal_type, rec["p_level"], rec["p_method"], rec["cost_hint"])
            continue

        session.merge(Extraction(**rec))
        if stats["llm_ok"] % 100 == 0:
            session.commit()
    if not dry_run:
        session.commit()

    cost = llm.estimate_cost(llm.MODEL, stats["prompt_tokens"], stats["completion_tokens"])
    stats["estimated_cost_usd"] = round(cost, 4) if cost is not None else None
    return stats
