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

# demands a general AI agent can execute online end-to-end (no hardware, no
# local resident process, no proprietary data): support calls, cancel/
# refund/delete-account chores, disputes, form filling
AGENT_REPLACEABLE = {
    "customer support", "customer service", "support", "cancel", "cancellation",
    "cancel subscription", "refund", "refund request", "chargeback", "dispute",
    "delete account", "account deletion", "close account", "complaint",
    "file a claim", "opt out", "unsubscribe", "客服", "取消订阅", "退款",
    "注销", "维权", "催办", "投诉", "申诉",
}

# demands whose viable business model depends on legal practice / attorney
# relationships (class actions, settlement claims, law-firm lead gen, case
# management) — a dead barrier for a non-US-entity solo developer. "settlement"
# is kept bare: in the news corpus it is almost always a legal settlement.
LEGAL_SERVICE = {
    "class action", "settlement", "settlement claim", "settlement fund",
    "claim form", "payout",
    "lawsuit", "legal claim", "attorney", "law firm", "litigation",
    "集体诉讼", "和解金", "和解", "诉讼", "索赔申请", "理赔金",
}

# travel/ticketing booking & price aggregation: fully covered by OTAs
# (Trip.com, Klook, Expedia, GetYourGuide) + official distribution, and gated
# by inventory/distribution agreements. Deliberately excludes generic
# "pricing" (SaaS price-monitoring stays viable).
TRAVEL_OTA = {
    "ticket price", "ticket prices", "park ticket", "park tickets", "airfare",
    "flight deal", "flight deals", "hotel booking", "hotel prices",
    "theme park", "disney", "迪士尼", "门票", "机票", "酒店预订", "酒店比价",
}

# cold-start / two-sided network effects: survival depends on having both
# sides of a market, BD relationships, or paid traffic at scale. Deliberately
# narrow vocabulary — ordinary tools must not hit this.
COLD_START = {
    "marketplace", "platform for", "investors", "crowdfunding platform",
    "two-sided", "双边", "平台撮合", "投资人对接", "撮合",
}

# 横向吸收者：平台原生功能或横向巨头纵向覆盖过的品类。缝隙扫描发现的
# Shopify 细分品类必须过这道"横向吸收检查"——被平台原生/横向 incumbent
# 纵向吸收的缝隙是假缝隙（与判据 #5 头部垄断同级封顶）。
INCUMBENT_ABSORBER = {
    "shopify native", "shopify 原生", "shopify checkout", "stripe", "klaviyo",
    "omnisend", "avalara", "stripe tax", "stripe radar", "signifyd",
    "feedonomics", "channeladvisor", "back in stock", "gorgias", "reconvert",
}

# manual_user 主品类 veto 向下传播：子品类簇名 → 主品类 verdict 名。
# 只传播明确否决过的主品类，不存在的父裁决不影响子簇。
VETO_PARENT_MAP = {
    "shopify·marketing-and-conversion-upsell-and-bundles": "shopify·marketing",
    "shopify·marketing-and-conversion": "shopify·marketing",
    "shopify·marketing-and-conversion-marketing-email-marketing": "shopify·marketing",
    "shopify·marketing-and-conversion-social-trust-product-reviews": "shopify·marketing",
    "shopify·store-design-internationalization-currency-and-translation": "shopify·store-design",
    "shopify·store-design-site-optimization-seo": "shopify·store-design",
    "shopify·orders-and-shipping-shipping-solutions-shipping": "shopify·orders-and-shipping",
    "shopify·sales-channels-selling-online-marketplaces": "shopify·sales-channels",
}
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

    # 平台主场（2026-10-11）：痛点发生在某平台社区内且诉求指向该平台自身
    # 能力范围（kaggle 数据请求→kaggle 平台）——incumbent 主场内无机会。
    # github_issues 不在此列：消费者产品 issue 是普通用户痛点（2026-10-12
    # 改造），只受 payer 开发者词表封顶约束，不受平台主场规则拦截。
    srcs = set(stats["sources"])
    platform_home = srcs <= {"kaggle_requests"}
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
    capped = False
    if gov_only:
        # cap: pure government demand can never be an easy indie entry
        score = min(score, 0.35)
        capped = True

    # general-purpose AI agents (Meta Muse, Claude/ChatGPT assistants) can
    # execute "online errand" demands — support calls, cancellations, refunds,
    # account deletion, disputes — directly, so these are never low-barrier.
    if _hits(text, AGENT_REPLACEABLE):
        score = min(score, 0.5)
        capped = True
        notes.append("通用 agent 可代办（客服/退订/维权类线上任务，正面强敌）")
    # legal-practice-dependent demands (class actions, settlement claims,
    # law-firm lead gen): dead barrier without a US entity / bar admission
    if _hits(text, LEGAL_SERVICE):
        score = min(score, 0.3)
        capped = True
        notes.append("法律服务依赖：集体诉讼/和解申领需执业资质或律师关系")
    # travel/ticket/hotel booking & price aggregation: OTA-covered + inventory
    if _hits(text, TRAVEL_OTA):
        score = min(score, 0.3)
        capped = True
        notes.append("旅行/门票预订：OTA 巨头与官方分销覆盖，库存协议壁垒")
    # cold-start / two-sided network effects: needs both sides or paid scale
    if _hits(text, COLD_START):
        score = min(score, 0.4)
        capped = True
        notes.append("冷启动：双边网络效应/BD 关系/流量采买，无分发优势难存活")
    # 横向吸收检查：平台原生功能/横向 incumbent 纵向覆盖的品类是假缝隙
    if _hits(text, INCUMBENT_ABSORBER):
        score = min(score, 0.35)
        capped = True
        notes.append("横向吸收：品类被平台原生功能或横向巨头纵向覆盖，假缝隙")
    if platform_home:
        score = min(score, 0.35)
        capped = True
        notes.append("平台主场：痛点诉求指向平台自身能力，incumbent 主场内无机会")
    return score, "rule", "；".join(notes) if notes else "低门槛", capped


BARRIER_ARBITRATION_SYSTEM = """你是独立开发者进入门槛评估员。给定需求主题（关键词 + 代表性痛点 + 当前门槛说明），
从四个维度评估综合门槛：技术（底层/引擎/驱动/实时集成=高门槛）、合规（医疗/金融/法律/政府资质）、
资源（重资本/供应链/线下履约/数据规模）、渠道（巨头核心功能/平台政策限制）。

**特别判据（命中任一即高门槛，barrier 判 0.1-0.3；第 4 条为 0.2-0.4）**：
1. 通用 AI agent 替代：核心价值可被通用 AI 个人 agent（Meta Muse、Claude/ChatGPT 类助手）
   在不依赖专用硬件/本地常驻进程/专有数据时直接代办——打电话客服、在线维权、退订、
   删账户、追扣费、填表等"线上代办"任务。**澄清**：一次性查价和持续监控类工具
   均可被通用 agent 或既有平台覆盖，除非能论证该领域无 incumbent 且数据壁垒真实可建，
   否则不豁免；若已存在垂类数据平台（如游戏价格的 SteamDB、电商价格的 camelcamelcamel/Keepa、
   机票的 Google Flights 历史价等），新进入者数据积累永远落后，必判高门槛（0.1-0.3）。
   其他豁免：依赖本地常驻系统干预、专用数据积累或硬件交付。
2. 法律服务依赖：可行商业模式需法律服务关系或执业资质——集体诉讼（class action）、
   和解金申领（settlement claim）、法律 lead gen、案件管理等。claim filing 侧被通用 agent
   和 DoNotPay 覆盖，案件管理/lead gen 侧必须有律师关系，对无美国实体者是死门槛。
3. OTA 覆盖：核心价值是旅行/门票/酒店/机票的预订、比价、折扣聚合——已被 Trip.com、Klook、
   Expedia、GetYourGuide 等 OTA 巨头和官方分销体系覆盖，且依赖库存/分销协议。
   豁免：OTA 覆盖不到的垂直工具（小众玩法、本地体验、内容社区）。
4. 冷启动/获客门槛：产品存活依赖双边网络效应（先有一边用户才能吸引另一边，如平台/市场）、
   地推/BD 关系、或大规模流量采买，而开发者无分发优势——barrier 判 0.2-0.4。
   豁免：有明确低成本触达通道的小众工具（ASO/SEO 可命中的具体痛点搜索词、
   垂类社区/Reddit 板块可直接触达目标用户）；纯靠"做出来自然有人来"的消费级产品不豁免。
5. 头部垄断：品类内已有明显头部玩家且功能广度/生态超出小团队可达范围
   （示例：客服工具之于 Gorgias、追加销售之于 ReConvert）→ 高门槛。
   豁免：品类分散、头部臃肿或定价过高留有缝隙时不适用。
6. 模板/组装覆盖：需求可被模板站、现成 SaaS 拼装、或通用 agent 套壳满足
   → 无独立产品空间，高门槛。
7. 数据获取死结：产品核心价值依赖的数据只能通过线下作业、私有渠道、独家关系获得
   → 比技术门槛更致命，高门槛。
8. 平台降维：纯软件方案面临「硬件+系统级平台」（如 Apple Watch/Apple Health）正面覆盖
   且无法差异化 → 高门槛。
9. 红海商品化：需求已被 commoditized，大量同质化产品且无差异化空间 → 高门槛。
10. 内容生产重：产品价值依赖大规模高质量内容创作（课程/游戏内容/媒体库）
    → 对小团队是高门槛。

**抄袭成功者策略（仅对"成功样本"/"新产品"类信号簇）**：这类信号代表已被验证的商业成功，
门槛不按原创评估，而按「能否差异化抄袭」评估：品类是否分散（无垄断者）、
incumbent 是否臃肿/本地化不足/受平台政策限制、跟随者能否用更低价格或更聚焦功能切入。
品类分散且 incumbent 臃肿 → 低门槛（0.6-0.8）；巨头垄断或强网络效应 → 高门槛（0.1-0.3）。

输出 JSON：{"barriers": [{"id": 序号, "barrier": 0.0到1.0（越高=门槛越低/越容易进入）, "note": "一句话主要门槛（中文）"}, ...]}。
参考刻度：纯信息聚合/内容工具=0.85-1.0；普通 SaaS=0.7-0.85；巨头核心功能正面竞争=0.4-0.6；
平台政策灰色地带=0.4-0.6；底层技术、强监管、通用 agent 可代办、法律服务依赖、OTA 覆盖=0.1-0.3；
冷启动/双边网络=0.2-0.4。"""
