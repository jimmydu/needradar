"""Barrier scoring (0-1; higher = lower barrier = easier to enter).

Distinct from software_fit ("can software address this"): barrier measures
how hard delivery is after entry. Rule-first, four sub-dimensions:
  technical   0.35  deep system/engine/driver/realtime integration
  compliance  0.30  HIPAA / finance licenses / legal / gov clearance
  resource    0.20  capital, supply chain, offline fulfillment, data scale
  channel     0.15  head-on with incumbents' core features / platform policy
barrier = weighted sum; final score multiplies it in.
"""
import re

GOV_SOURCES = {"sam_gov", "usaspending"}

TECH_DEEP = {
    "engine", "hook", "hooks", "driver", "kernel", "firmware", "embedded",
    "realtime", "real-time", "protocol", "低代码", "引擎", "驱动", "内核",
    "底层", "固件", "实时", "协议",
}
COMPLIANCE_HIGH = {
    "hipaa", "medical device", "clinical", "lending", "loan", "mortgage",
    "debt", "collection", "payment license", "money transmitter", "legal",
    "attorney", "clearance", "医疗器械", "放贷", "催收", "债务", "法律", "资质",
    "医疗", "金融", "支付牌照",
}
RESOURCE_HEAVY = {
    "supply chain", "logistics", "fulfillment", "warehouse", "inventory",
    "nationwide", "manufacturing", "hardware", "供应链", "履约", "物流",
    "库存", "制造", "全网", "线下",
}
CHANNEL_INCUMBENT = {
    "quickbooks", "excel", "turbotax", "salesforce", "slack", "notion",
}
CHANNEL_PLATFORM = {
    "auto-cancel", "automatically cancel", "cancel subscriptions",
    "refund request", "自动取消", "一键取消", "退款申诉", "取消订阅",
}
GAME_PERF = {"game", "games", "手游", "steam", "fps", "帧率", "掉帧"}
INFO_AGG = {"聚合", "比价", "提醒", "monitor", "tracking", "alert", "aggregator",
            "comparison", "比价", "信息聚合"}


def _hits(text, kws):
    return [k for k in kws if (k in text if re.search(r"[一-鿿]", k)
                               else re.search(rf"\b{re.escape(k)}\b", text))]


def rule_barrier(members, stats):
    """Returns (score, method, note). note is a one-line Chinese explanation."""
    text = (stats["text"] + " " + " ".join(s.title or "" for s, _, _ in members)).lower()

    tech = 1.0
    tech_hits = _hits(text, TECH_DEEP)
    game_perf = _hits(text, GAME_PERF) and _hits(text, {"performance", "monitor",
                                                        "slow", "lag", "性能", "卡顿"})
    if tech_hits:
        tech = 0.2
    elif game_perf:
        tech = 0.35  # game perf tooling implies engine-level integration

    compliance = 1.0
    comp_hits = _hits(text, COMPLIANCE_HIGH)
    if comp_hits:
        compliance = 0.2

    gov_only = set(stats["sources"]) <= GOV_SOURCES
    if gov_only:
        # government contracting requires a US entity / contractor registration
        # (SAM.gov) and often clearances — the compliance dim must reflect that
        # even when no regulated-industry keyword appears in the title
        compliance = min(compliance, 0.2)
        comp_hits = comp_hits or ["government contract"]

    resource = 1.0
    res_hits = _hits(text, RESOURCE_HEAVY)
    if res_hits:
        resource = 0.4

    channel = 1.0
    if _hits(text, CHANNEL_PLATFORM):
        channel = 0.5
    elif _hits(text, CHANNEL_INCUMBENT):
        channel = 0.5
    if gov_only:
        channel = min(channel, 0.3)  # no access to the gov sales channel as an indie

    notes = []
    if tech < 1:
        notes.append("技术：需游戏引擎级/底层系统集成" if game_perf and not tech_hits
                     else f"技术：深度底层集成（{tech_hits[0]}）")
    if compliance < 1:
        notes.append("合规：政府合同需美国实体/承包商资质/安全许可" if gov_only
                     else f"合规：{comp_hits[0]} 受监管/需资质")
    if resource < 1:
        notes.append(f"资源：{res_hits[0]} 需重资本/线下履约/数据规模")
    if channel < 1 and not gov_only:
        notes.append("渠道：平台政策限制（自动取消/退款灰色地带）"
                     if _hits(text, CHANNEL_PLATFORM) else "竞争：直面巨头核心功能")
    elif gov_only:
        notes.append("渠道：无政府销售渠道")

    score = round(0.35 * tech + 0.30 * compliance + 0.20 * resource + 0.15 * channel, 3)
    if gov_only:
        # cap: pure government demand can never be an easy indie entry
        score = min(score, 0.35)
    return score, "rule", "；".join(notes) if notes else "低门槛"


BARRIER_ARBITRATION_SYSTEM = """你是独立开发者进入门槛评估员。给定需求主题（关键词 + 代表性痛点 + 当前门槛说明），
从四个维度评估综合门槛：技术（底层/引擎/驱动/实时集成=高门槛）、合规（医疗/金融/法律/政府资质）、
资源（重资本/供应链/线下履约/数据规模）、渠道（巨头核心功能/平台政策限制）。
输出 JSON：{"barriers": [{"id": 序号, "barrier": 0.0到1.0（越高=门槛越低）, "note": "一句话主要门槛（中文）"}, ...]}。
参考刻度：纯信息聚合/内容工具=0.85-1.0；普通 SaaS=0.7-0.85；巨头核心功能正面竞争=0.4-0.6；
平台政策灰色地带=0.4-0.6；底层技术或强监管=0.1-0.3。"""
