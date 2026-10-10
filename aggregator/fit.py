"""Software-fit scoring (0-1): can an indie developer deliver this with code?

Rules first; the LLM only arbitrates ambiguous clusters (see pipeline).
Physical-goods procurement, licensed industries and heavy-asset services
score low; software keywords and consumer/SMB sources score high. SAM.gov
PSC (classificationCode) starting with a digit = physical product -> low;
'D' = IT services -> medium-high (contractor eligibility required, noted).
"""

PHYSICAL_KEYWORDS = {
    "stairlift", "stairglide", "valve", "bearing", "blood", "reagent", "equipment",
    "supply", "supplies", "nut", "bolt", "screw", "cable", "pump", "gasket",
    "washer", "bracket", "beam", "connector", "fairing", "cylinder", "switch",
    "wheelchair", "generator", "engine", "motor", "hardware", "lumber", "steel",
    "construction", "hvac", "plumbing", "roofing", "concrete", "vehicle",
    "aircraft", "weapon", "ammunition", "food", "commodities", "fuel",
    "prosthetic", "x-ray", "laboratory", "medical", "surgical", "pharmaceutical",
}
SOFTWARE_KEYWORDS = {
    "software", "app", "apps", "platform", "automation", "automate", "dashboard",
    "integration", "integrate", "invoice", "invoicing", "scheduling", "booking",
    "saas", "tool", "crm", "ai", "api", "workflow", "tracking", "analytics",
    "sync", "notification", "notifications", "subscription", "billing", "payment",
    "payments", "website", "online", "digital", "cloud", "mobile", "data",
    "spreadsheet", "excel", "template", "templates", "marketplace", "portal",
    "软件", "工具", "自动化", "平台", "系统", "记账", "预约", "排期", "发票",
}
LICENSED_KEYWORDS = {
    "license", "licensed", "permit", "medical", "healthcare", "doctor", "legal",
    "attorney", "law", "government", "federal", "bank", "banking", "insurance",
    "mortgage", "lender", "broker", "investment", "clinical",
    "牌照", "资质", "医疗", "法律", "政府", "银行", "保险", "金融",
}
CONSUMER_SOURCES = {"appstore", "reddit_rss", "reddit", "freelancer", "manual"}
GOV_SOURCES = {"sam_gov", "usaspending"}


def rule_software_fit(cluster, stats):
    """Returns (fit, method, note). method: rule / rule_gov_it / rule_gov_svc."""
    import re
    text = (stats["text"] + " " + " ".join(s.title or "" for s, _, _ in cluster)).lower()
    sources = set(stats["sources"])
    words = set(re.findall(r"[a-z0-9\-]+|[一-鿿]+", text))

    physical = words & PHYSICAL_KEYWORDS
    software = words & SOFTWARE_KEYWORDS
    licensed = (words & {k for k in LICENSED_KEYWORDS if " " not in k}) or \
        {k for k in LICENSED_KEYWORDS if " " in k and k in text}

    gov_only = sources and sources <= GOV_SOURCES
    psc_codes = {(raw.get("classificationCode") or "") for _, _, raw in cluster}
    psc_product = any(c[:1].isdigit() for c in psc_codes if c)
    psc_it = any(c.startswith("D") for c in psc_codes)

    if gov_only:
        if psc_it and not psc_product:
            return 0.65, "rule_gov_it", "政府 IT 服务采购：软件可交付，但需承包商资质/实体"
        if psc_product or physical:
            return 0.1, "rule", "实物/物料采购，独立开发者不可交付"
        if physical:
            return 0.1, "rule", "实物/物料采购"
        return 0.35, "rule_gov_svc", "政府服务类采购，多需资质/实体"

    fit = 0.5
    if physical and not software:
        fit = 0.15
    else:
        if software:
            fit += 0.3
        if physical:
            fit -= 0.25
    if sources & CONSUMER_SOURCES:
        fit += 0.15
    if licensed:
        fit -= 0.2
    return round(min(max(fit, 0.05), 0.95), 3), "rule", None


FIT_ARBITRATION_SYSTEM = """你是独立开发者可行性评估员。给定若干需求主题（关键词 + 代表性痛点），
判断一个无美国实体、无牌照、无硬件能力的独立软件开发者能否用纯软件/SaaS/App/自动化脚本交付。
输出 JSON：{"fits": [{"id": 序号, "fit": 0.0到1.0, "note": "一句话理由（中文）"}, ...]}。
参考刻度：纯软件工具=0.8-0.95；软件为主但需线下配合=0.4-0.6；需牌照/实体制造/政府资质=0.05-0.2。"""

ENTRY_POINT_SYSTEM = """你是独立开发者顾问。给定编号的需求主题（关键词 + 代表性痛点 + 来源分布），
为每条输出：
1. entry：一句切入点建议（中文，≤40字）：独立开发者怎么切入，例如"做面向X人群的Y工具/SaaS"、
"做信息聚合/比价工具"等。必须具体、可执行，不要套话。若主题本身不适合软件切入（实物/牌照），
也给出周边软件机会（如投标信息聚合、比价、合规提醒工具）。
2. comps：该方向已有的知名解决方案/竞品，1-3 个简短名称（中英文均可，如 "Rocket Money"、"SteamDB"）。
不确定就给空数组，禁止编造。
输出 JSON：{"entries": [{"id": 序号, "entry": "...", "comps": ["...", "..."]}, ...]}。"""
