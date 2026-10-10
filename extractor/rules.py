"""Rule-based payment-signal (P-level) classification and cost-hint extraction.

Rules win over LLM per PRD §6: amounts, solicitations, budgets are classified
directly. Returns (p_level or None, cost_hint or None).
"""
import re

# $1,234 / $1.2M / 1200 USD / $50/hr
_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|b|thousand|million|billion)?"
    r"(?:\s?(?:/|per\s)(?:hr|hour|mo|month|yr|year))?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|dollars)\b",
    re.IGNORECASE,
)
_BUDGET_RE = re.compile(
    r"\b(?:budget(?:ed)?|willing to pay|would pay|happy to pay|paying|paid|quote[ds]?"
    r"|charged|costs? (?:me|us)|price[ds]? at|subscription|per (?:month|year|hour|seat|user))\b"
    r"[^.]{0,60}?\$\s?\d|\$\s?\d[^.]{0,60}?"
    r"\b(?:budget|per (?:month|year|hour|seat|user)|a (?:month|year|hour))\b",
    re.IGNORECASE,
)


# Trigger-event patterns for news signals (题材雷达): a policy/market event
# that creates demand for a class of tools. Rule-first, text match only.
TRIGGER_PATTERNS = [
    ("新政策生效→合规工具", re.compile(
        r"\b(new (rule|regulation|law|requirement)s? (takes? effect|coming)|"
        r"compliance deadline|effective date|mandate[ds]?)\b|新规|合规截止", re.I)),
    ("价格差/涨价→比价撮合", re.compile(
        r"\b(price (hike|increase|surge)|prices (rise|soar|jump)|fees? (increase|hike)|"
        r"rate hike|tariff)\b|涨价|加价", re.I)),
    ("短缺/积压→调度", re.compile(
        r"\b(shortage|backlog|waitlist|out of stock|supply crunch|delays? of)\b|短缺|积压|断货", re.I)),
    ("集体诉讼和解→理赔自动化", re.compile(
        r"\b(class action( settlement)?|settlement fund|claim form|payout)\b|集体诉讼|和解", re.I)),
    ("服务关停→迁移替代", re.compile(
        r"\b(shuts? down|shutting down|discontinued|sunsetting|end of life|"
        r"no longer available|winding down)\b|关停|下线|停止服务", re.I)),
]


def detect_trigger(text):
    """Return the first matching trigger-event label, or None."""
    for label, rx in TRIGGER_PATTERNS:
        if rx.search(text or ""):
            return label
    return None


def extract_cost_hint(text):
    """Return a short cost hint string if the text mentions concrete money."""
    if not text:
        return None
    amounts = _MONEY_RE.findall(text)
    amounts = [a.strip() for a in amounts if any(c.isdigit() for c in a)]
    if not amounts:
        return None
    hint = ", ".join(amounts[:5])
    if _BUDGET_RE.search(text):
        hint = f"budget context: {hint}"
    return hint[:200]


def rule_p_level(signal):
    """Determine P-level from structured fields + title/description text.

    P5 = money already paid; P4 = committed budget / active solicitation;
    P3 = explicit money intent in text. Returns None when rules cannot decide.
    """
    source = signal.source
    stype = signal.signal_type
    text = " ".join(filter(None, [signal.title, signal.description]))

    if source == "usaspending":
        return 5  # contract award = money already obligated
    if source == "sam_gov":
        if stype == "中标":
            return 5
        if stype == "招标":
            return 4
        if stype == "意向":
            return 3
    if source == "freelancer":
        if stype == "已完成外包":
            return 5
        return 4 if signal.amount else 3
    if source in ("kickstarter", "indiegogo"):
        # crowdfunding with pledged amount = money already paid
        return 5 if signal.amount else 3
    if source == "manual":
        if stype == "众筹预售":
            return 5 if signal.amount else 4
        if stype == "外包发布":
            return 4
        if stype == "招标/采购":
            return 4
        # 付费抱怨/求推荐 / 其他: fall through to text rules
    if source == "appstore_charts":
        return 5  # top-grossing rank itself is evidence of real payment volume

    # text-bearing sources (appstore/cfpb/reddit_rss/manual free text)
    hint = extract_cost_hint(text)
    if hint:
        return 3
    return None


def rule_cost_hint(signal):
    return extract_cost_hint(" ".join(filter(None, [signal.title, signal.description])))
