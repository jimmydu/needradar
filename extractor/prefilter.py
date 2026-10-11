"""Cheap pre-extraction filter: mark dead-on-arrival signals skipped so they
never hit the LLM queue. Conservative by design — when unsure, let it through.
Skipped rows keep an auditable reason in extractions.skip_reason and can be
revived by clearing it. Aggregation ignores skipped rows.
"""

# complaint/money markers; a signal without any of these AND not a bad review
# has no extractable pain (verified 2026-10-11: appstore LLM pain yield is
# 68% within rating<=3, near zero outside)
PAIN_WORDS = (
    "crash", "broken", "bug", "slow", "expensive", "charged", "refund",
    "cancel", "support", "missing", "lost", "hate", "frustrat", "useless",
    "worst", "annoying", "fix", "issue", "problem", "fail", "error",
    "卡", "崩", "闪退", "难用", "太贵", "扣费", "退款", "客服", "投诉", "丢失",
)

LLM_SOURCES = {"appstore", "reddit_rss", "reddit", "manual", "freelancer"}


def skip_reason(signal):
    """Return a skip reason for high-confidence no-pain signals, else None."""
    if signal.source not in LLM_SOURCES:
        return None
    text = f"{signal.title or ''} {signal.description or ''}".lower().strip()
    if len(text) < 30:
        return "文本过短（<30字符），无痛点可抽"
    has_pain = any(w in text for w in PAIN_WORDS)
    if not has_pain and (signal.rating is None or signal.rating >= 3):
        return "无抱怨/金额线索且非差评，LLM 无抽取价值"
    return None
