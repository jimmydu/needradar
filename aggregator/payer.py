"""Payer-quality scoring (0-1): who pays, and will they actually pay?

Independent dimension from barrier (can it be built) and fit (can software
address it): a demand whose only buyers are broke self-builders is not a
business. Rule cap for obvious self-builder audiences; LLM verdicts persist
in payer_verdicts and are inherited across runs by topic name.
"""

# signals that the likely buyer is a self-builder (cap 0.4). Applied ONLY to
# the cluster summary/keywords text, never to member-signal text — a post
# from r/selfhosted is not automatically a selfhoster customer.
PAYER_LOW = {
    "indie hacker", "side project", "self-hosted", "selfhosted",
    "open source alternative", "个人开发者", "副业项目", "自建", "爱好者",
}

PAYER_SYSTEM = """你是独立开发者商业分析员。给定需求主题（关键词 + 代表性痛点 + 切入点），
评估该主题切入点产品的「付费对象质量」payer_quality（0.0-1.0，越高=买单者越有预算和付费意愿）：
(a) 客户是个人开发者/爱好者/学生 → 0.2-0.4（付费意愿低，且能自己抄一个工具而不付钱）；
(b) 客户是普通消费者且无强付费场景 → 0.3-0.5；
(c) 客户是有预算的小商家/创作者/企业，且产品带持续数据沉淀或嵌入工作流
    （客户自建=长期维护负担，更可能付费）→ 0.6-0.8；
(d) 企业级预算 + 强切换成本 → 0.8-1.0。
注意区分「信号发出者」和「买单者」：Reddit 上抱怨的开发者不一定是客户——
如果产品的合理买家是有预算的商家，按买家评。
**无留存陷阱**：若产品的价值交付是一次性修复（钩子改良、转化率修好客户就退订），
无法形成持续依赖/复购，payer_quality 判低（0.2-0.3），即使当前痛点真实存在。
**成功样本类信号**（signal_type 为"成功样本"或 source 为 appstore_indie/gplay_indie/shopify_apps/betalist 的簇）：
变现已被市场验证，payer_quality 可给 0.7 以上；面向电商商家（Shopify 生态）的工具，
商家付费意愿和预算都强，payer 给 0.7 以上；此时评估重点转向「跟随者能否分到钱」。
**GitHub 源信号**（github_rising/github_issues 簇）：默认客户是开发者，从严评 0.2-0.4。
只有两种形态可豁免上调：① 开源项目的企业托管/服务版（买单者是公司而非开发者）→ 0.6-0.8；
② 飙升仓库揭示的底层需求存在**非开发者产品化**空间（普通消费者/商家买单）→ 按实际买家评。
其余 GitHub 簇一律从严，不要放水。
输出 JSON：{"payers": [{"id": 序号, "payer_quality": 0.0到1.0, "note": "一句话说明买单者与付费逻辑（中文）"}, ...]}。"""
