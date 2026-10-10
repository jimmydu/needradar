"""Hard filters applied after scoring, before ranking/reporting.

Filtered clusters stay in the DB with `filtered_reason` for audit and manual
revival; the report lists them separately.
"""
import logging
import re

log = logging.getLogger(__name__)

GOV_SOURCES = {"sam_gov", "usaspending"}

# canonical topics whose underlying demand IS a licensed activity
REGULATED_TOPICS = {
    "催收与债务": "债务催收/管理本身受监管（FDCPA），需持证经营",
    "贷款与房贷": "放贷/房贷服务需金融牌照",
    "信用报告": "征信服务受 FCRA 监管，需资质",
}

# demand that only an incumbent-scale product can satisfy (vs complaints
# about incumbents, which are opportunities and are kept)
INCUMBENT_CORE = {
    "quickbooks": "记账核心",
    "excel": "电子表格",
    "turbotax": "报税核心",
    "salesforce": "CRM 核心",
    "slack": "团队协作核心",
}
# complaint markers: incumbent mentioned + complaint = opportunity, keep
COMPLAINT_MARKERS = {
    "expensive", "overpriced", "price", "pricing", "slow", "crash", "bad",
    "confusing", "clunky", "hate", "frustrat", "refund", "charged",
    "太贵", "涨价", "难用", "坑", "垃圾",
}

MONOPOLY_SYSTEM = """你是市场竞争分析员。给定需求主题（关键词 + 代表性痛点），判断其核心需求
是否已被巨头标准产品完整覆盖且无差异化空间（如是，则独立开发者不应进入）。
注意区分：抱怨巨头产品涨价/难用/故障 = 差异化机会，答 false；需求本身只能由
巨头级基础设施满足（如通用搜索引擎、操作系统、大规模云平台）= true。
输出 JSON：{"verdicts": [{"id": 序号, "monopoly": true/false, "note": "一句话理由（中文）"}, ...]}。"""


def _text_of(r):
    return (" ".join(r["keywords"]) + " " + r["summary"]).lower()


def apply_filters(results, usage, use_llm, chat_json):
    """Set r['filtered_reason'] (None = survives). Returns (survivors, filtered)."""
    # rule preselection for monopoly candidates: incumbent named, no complaint
    monopoly_candidates = []
    for r in results:
        if r.get("filtered_reason"):
            continue
        text = _text_of(r)
        incumbent = next((k for k in INCUMBENT_CORE if re.search(rf"\b{re.escape(k)}\b", text)), None)
        if incumbent and not any(m in text for m in COMPLAINT_MARKERS):
            r["_incumbent"] = incumbent
            monopoly_candidates.append(r)

    if use_llm and monopoly_candidates:
        batch = 15
        for start in range(0, len(monopoly_candidates), batch):
            chunk = monopoly_candidates[start:start + batch]
            lines = [f"{n}. 关键词: {', '.join(r['keywords'][:8])}｜代表痛点: {r['summary'][:120]}"
                     for n, r in enumerate(chunk, 1)]
            try:
                parsed, ptok, ctok = chat_json(MONOPOLY_SYSTEM, "\n".join(lines))
                usage["calls"] += 1
                usage["prompt_tokens"] += ptok
                usage["completion_tokens"] += ctok
                by_id = {m.get("id"): m for m in parsed.get("verdicts", [])}
                for n, r in enumerate(chunk, 1):
                    m = by_id.get(n)
                    if m and m.get("monopoly"):
                        r["filtered_reason"] = (
                            f"强竞品垄断：核心需求已被 {r['_incumbent']} 完整覆盖"
                            + (f"（{m['note']}）" if m.get("note") else ""))
            except Exception as e:
                log.warning("monopoly arbitration batch failed: %s", e)

    survivors, filtered = [], []
    for r in results:
        reason = r.get("filtered_reason")
        if not reason:
            if r["software_fit"] < 0.3:
                reason = "代码可行性过低（硬件/实物/线下服务）"
            elif set(r["stats"]["sources"]) <= GOV_SOURCES \
                    and r["software_fit"] <= 0.35 and r["fit_method"] != "rule_gov_it":
                reason = "政府采购需美国实体/承包商资质"
            elif r["barrier"] < 0.4:
                reason = f"门槛过高（{r['barrier_note']}）"
            elif r.get("topic") in REGULATED_TOPICS:
                reason = f"合规：{REGULATED_TOPICS[r['topic']]}"
            elif r["stats"]["n"] < 2:
                reason = "孤信号（证据不足 2 条）"
        r["filtered_reason"] = reason
        (filtered if reason else survivors).append(r)
    return survivors, filtered
