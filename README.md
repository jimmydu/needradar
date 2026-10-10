# NeedRadar — Data Collection Layer

**English** | 中文说明见下文

NeedRadar is an independent market research tool that aggregates public data to identify demand signals — problems people are actively willing to pay to solve. It produces weekly aggregate trend reports for internal research.

This repository contains the data collection layer. All collectors use **official APIs or public data feeds only** — no unauthorized scraping:

| Source | Access | Signal type |
|---|---|---|
| SAM.gov | Official API (free key) | Government solicitations & awards |
| USAspending.gov | Official public API (no key) | Federal contract spending |
| CFPB Complaints | Official public API (no key) | Consumer complaints (structured fields) |
| App Store | Apple official RSS feed | App reviews + top-grossing chart snapshots |
| Reddit | Official Data API (`collectors/reddit.py`, OAuth2) — pending Responsible Builder Policy approval; public RSS used as interim fallback (`collectors/reddit_rss.py`) | Community pain-point posts |
| Freelancer.com | Official API (approval pending) | Outsourcing project listings & budgets |

Data handling: read-only collection, aggregate statistical analysis only, no redistribution of raw content, no user profiling. Collected records are stored in a private local database and refreshed per each platform's API terms.

## Signal extraction

`run_extract.py` turns raw signals into structured demand fields (table `extractions`, one row per signal):

- **Rules first** (`extractor/rules.py`): money already paid → P5 (USAspending, SAM.gov awards, completed freelance jobs, funded crowdfunding); committed budget / active solicitation → P4 (SAM.gov solicitations, freelance posts with budget); explicit money intent in text → P3 (regex on `$` amounts, hourly rates, budget context). Rule decisions win over the LLM.
- **LLM** (`extractor/llm.py`) only fills semantic fields (pain point, audience, scenario, urgency, current solution, alternatives, supply gap). Every non-empty field must carry a verbatim evidence quote from the original text; unquotable fields are forced empty.
- Cost controls: LLM runs only on text-bearing sources (app store reviews rated ≤3, Reddit posts, manual entries, freelance listings); structured/title-only sources (SAM.gov, USAspending, CFPB, charts) never call the LLM. Token usage is stored per row in `extractions` for cost accounting.

```bash
python run_extract.py --dry-run --limit 50   # rules only, nothing written
python run_extract.py --no-llm               # rules only, write results
python run_extract.py --source appstore --limit 50
```

LLM config (OpenAI-compatible): `OPENAI_API_KEY` (required), `OPENAI_BASE_URL` (optional), `OPENAI_MODEL` (default `gpt-4o-mini`). No keys are hardcoded or logged.

## Aggregation & WPS ranking

`run_rank.py` clusters signals into demand themes and ranks them per PRD §7–§9:

- **Clustering** (`aggregator/clustering.py`): rules first — each signal is assigned to exactly one **canonical topic** (priority-ordered keyword system, ~20 topics: 订阅与扣费 / 定价与涨价 / 崩溃与稳定性 / 性能与速度 / 数据丢失与同步 / 广告过多 / AI 功能反感 / 客服与支持 / 发票与记账 / 预约与排程 / 登录与账户 / 通知与提醒 / 界面与易用性 / 离线与网络依赖 / 欺诈与盗刷 / 催收与债务 / 信用报告 / 贷款与房贷 / 政府合同与采购 / 招聘与外包 / 功能缺失与请求; Latin keywords match on word boundaries, CJK as substrings; gov procurement sources excluded). The topic itself is the cluster. Signals outside any topic fall back to structured buckets (CFPB issue, NAICS, app/subreddit) + token-Jaccard union-find; only these residual clusters go through LLM merge arbitration (a few batched calls). Text sources without an extracted pain point and chart snapshots are excluded as noise.
- **Scoring** (`aggregator/scoring.py`): WPS = 0.35·P-strength + 0.25·density (weekly-decayed count / candidate median) + 0.15·source-diversity + 0.10·amount-strength + 0.10·trend-persistence + 0.05·geo-HHI; confidence and accessibility per §9.3/§9.4.
- **Software fit** (`aggregator/fit.py`): 0–1 rule score for indie-developer deliverability — physical-goods keywords and numeric PSC codes score ~0.1, SAM.gov PSC 'D' (IT services) 0.65, software keywords + consumer/SMB sources push up, licensed industries push down; ambiguous near-top clusters go to LLM arbitration (a few batched calls).
- **Barrier** (`aggregator/barrier.py`): 0–1 score for post-entry difficulty (higher = easier) — 0.35·technical (engine/driver/realtime integration) + 0.30·compliance (HIPAA/finance licenses/legal/gov clearance) + 0.20·resource (capital/supply chain/data scale) + 0.15·channel (incumbent core features / platform policy gray zones, e.g. auto-cancel subscriptions); each hit carries a one-line Chinese note; ambiguous cases go to batched LLM arbitration.
- **Hard filters** (`aggregator/filtering.py`): after scoring, clusters are removed (not just down-scored) when software_fit < 0.3 (hardware/offline), gov-only with fit ≤ 0.35 (US entity / contractor eligibility required), barrier < 0.4, the topic itself is a licensed activity (debt collection / lending / credit reporting), the demand is fully covered by an incumbent with no differentiation room (rule preselection + LLM verdict; complaints about incumbents are kept as opportunities), or fewer than 2 evidence signals. Filtered rows keep a `filtered_reason` in the `clusters` table and are listed in the report's audit appendix for manual revival.
- Final = WPS × confidence × accessibility × software_fit × barrier.
- Results persist to the `clusters` table (idempotent per `run_date`) and a Markdown report is exported to `reports/topN_YYYYMMDD.md` (main board + pure-WPS Top 20, per-item entry-point suggestions, verbatim evidence quotes).

```bash
python run_rank.py                    # Top 10, 4-week window
python run_rank.py --top 20 --dry-run # no writes, no LLM
```

---

# NeedRadar — 采集层 MVP（中文）

每日采集美国公开市场数据（SAM.gov 机会公告、USAspending 合同授予、App Store 评论、Reddit 帖子），落库到 `raw_signals` 表，供后续信号抽取层使用。

## 安装

```bash
uv venv && uv pip install -r requirements.txt   # 或 python3 -m venv .venv && pip install -r requirements.txt
```

## 配置

- `DATABASE_URL`：默认 `sqlite:///./needradar.db`；接 PostgreSQL 时设为 `postgresql+psycopg2://user:pass@host/db`（需安装 psycopg2）。
- `SAM_GOV_API_KEY_FILE`：默认 `./sam.gov.api.key`（纯文本 key，文件已在 .gitignore 中）。
- `APPSTORE_APPS_FILE`：默认 `./appstore_apps.txt`（每行 `app_id  # 注释`）。每日评论采集目标 = 该清单 + 当日畅销榜 top 100 合并去重。
- `REDDIT_SUBS_FILE`：默认 `./reddit_subs.txt`（每行一个 subreddit 名）。
- `FREELANCER_API_KEY_FILE`：默认 `./freelancer.api.key`（纯文本 token，已在 .gitignore）。也可用 `FREELANCER_ACCESS_TOKEN` 环境变量。

### Freelancer.com 凭证

1. 打开 https://developers.freelancer.com 注册开发者账号
2. 创建应用 / 申请 API key，项目说明定位为"聚合分析"（不是竞争市场平台）
3. 审批通过后把 access_token 写入 `./freelancer.api.key` 或设置 `FREELANCER_ACCESS_TOKEN`

审批可能要几天。未配置时采集器打印提示并跳过。注意 API 条款要求数据缓存 24h 刷新：本采集器按条款每日重采，不做长期缓存复用，raw_json 仅供当日处理使用。

### Reddit 凭证（暂不可用，RSS 兜底中）

Reddit 于 2025-11 关闭自助 API 应用创建，改为 Responsible Builder Policy 人工审批。审批期间默认用 `collectors/reddit_rss.py` 走公开 RSS 兜底（免认证）；局限：无 score/评论数、每 sub 仅最新约 25 条、对抓取频率敏感（已内置 5s 间隔 + 退避）。OAuth 版 `collectors/reddit.py` 保留，审批下来后按下面步骤配置即可启用：

1. 打开 https://www.reddit.com/prefs/apps → **create app**（当前需人工审批）
2. 类型选 **script**，redirect uri 填 `http://localhost`
3. app 名下面那串是 client_id，secret 字段是 client_secret
4. 设置环境变量：`REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET`、`REDDIT_USERNAME`（你的 reddit 用户名，用于 User-Agent）

两个采集器都未配置/不可用时打印提示并跳过，不影响其他源。免费档为非商用（约 100 QPM），商用前需购授权。

## 运行

```bash
python run_daily.py                          # 默认拉前一天，全部源
python run_daily.py --source usaspending     # 只跑某个源（sam_gov/usaspending/appstore/cfpb/reddit_rss/reddit/freelancer）
python run_daily.py --from 2026-10-01 --to 2026-10-08
```

也可单独运行某个采集器：`python -m collectors.sam_gov --from ... --to ...`

注意 SAM.gov 非联邦 key 配额约 10 次/天，采集器按 ptype 分页、limit=1000，配额耗尽时优雅停止不崩溃。App Store RSS 端点免认证但每 App 只有最近约 500 条评论。重复运行通过 (source, source_id) 唯一约束去重，已存在的数据跳过。

## 信号抽取

`run_extract.py` 把原始信号抽取为结构化需求字段（`extractions` 表，每条信号一行，`signal_id` 主键幂等，重复跑自动跳过）：

- **规则优先**（`extractor/rules.py`）：已发生付费 → P5（USAspending、SAM.gov 中标、已完成外包、已筹得众筹）；明确预算/在途招标 → P4（SAM.gov 招标、带预算的外包发布）；文本中的明确金钱意向 → P3（`$` 金额、时薪、预算语境正则，同时抽 `cost_hint`）。规则判定优先于 LLM。
- **LLM 只做语义字段**（`extractor/llm.py`）：痛点、人群、场景、紧急度、现有方案、替代方案、供给不足。每个非空字段必须带原文逐字引用（evidence quote），无法引用的字段强制置空，防止编造。
- 成本控制：LLM 只处理有正文的源（App Store ≤3 星评论、Reddit 帖子、手动录入、外包项目）；纯标题/结构化源（SAM.gov、USAspending、CFPB、榜单）不调 LLM。每次调用的 token 用量落 `extractions` 表，便于核算成本。

```bash
python run_extract.py --dry-run --limit 50   # 只跑规则、打印，不写库
python run_extract.py --no-llm               # 只跑规则并写库
python run_extract.py --source appstore --limit 50   # 规则 + LLM（仅 ≤3 星评论）
python run_extract.py --force                # 重抽已有结果
```

LLM 配置（OpenAI 兼容接口）：`OPENAI_API_KEY`（必需）、`OPENAI_BASE_URL`（可选，可指向任何兼容端点）、`OPENAI_MODEL`（默认 `gpt-4o-mini`）。不硬编码任何 key。

## 聚合与 WPS 排序

`run_rank.py` 把信号聚类为需求主题并按 PRD §7–§9 打分排序：

- **聚类**（`aggregator/clustering.py`）：规范主题体系——每条信号按优先级关键词命中归入唯一规范主题（订阅与扣费/定价与涨价/崩溃与稳定性/性能与速度/数据丢失与同步/广告过多/AI 功能反感/客服与支持/发票与记账/预约与排程/登录与账户/通知与提醒/界面与易用性/离线与网络依赖/欺诈与盗刷/催收与债务/信用报告/贷款与房贷/政府合同与采购/招聘与外包/功能缺失与请求；英文词边界匹配、中文子串匹配；政府采购源不参与主题匹配），主题本身即簇，杜绝同主题碎裂。主题之外的残余信号走结构化分桶（CFPB issue、NAICS、App/sub）+ token Jaccard 并查集，仅残余簇参与 LLM 合并裁决（几次批量调用）。无 pain_point 的文本源信号与榜单快照不参与聚类。
- **打分**（`aggregator/scoring.py`）：WPS = 0.35·等级强度 + 0.25·密度（周衰减加权/候选池中位数）+ 0.15·来源多样性 + 0.10·金额线索 + 0.10·趋势持续性 + 0.05·地域集中度；置信度与可进入性按 §9.3/§9.4。
- **代码可行性**（`aggregator/fit.py`）：0–1 规则分，衡量独立开发者可否纯软件交付——实物/物料关键词与数字 PSC 码约 0.1，SAM.gov PSC 以 D 开头（IT 服务）0.65，软件关键词 + 消费/SMB 来源加分，牌照行业减分；顶部分数模糊（0.3–0.65）的簇交 LLM 批量裁决。
- **门槛高度**（`aggregator/barrier.py`）：0–1 分（越高=门槛越低），衡量切入后交付难度——0.35·技术（引擎/驱动/实时底层集成）+ 0.30·合规（HIPAA/金融牌照/法律/政府资质）+ 0.20·资源（重资本/供应链/数据规模）+ 0.15·渠道（巨头核心功能/平台政策灰色地带，如自动取消订阅）；每条附一句门槛说明；边界簇 LLM 批量裁决。
- 最终分 = WPS × 置信度 × 可进入性 × 代码可行性 × 门槛高度。
- **硬过滤**（`aggregator/filtering.py`）：打分后出榜前剔除——software_fit<0.3（硬件/线下）、纯政府采购且 fit≤0.35（需美国实体/承包商资质）、barrier<0.4（门槛过高）、主题本质为持证经营（催收/放贷/征信）、强竞品垄断无差异化空间（规则预选 + LLM 裁决；抱怨巨头涨价/难用的主题保留为机会）、孤信号（n<2 且 P_max<P4；P4/P5 孤信号保留）。被过滤簇在 `clusters` 表保留 `filtered_reason`，报告附「已过滤清单」供人工复核复活；报告另附「大证据量但未进榜主题」说明（≥20 条证据但无付费意愿信号，按 §9.1 不进榜，不改公式）。
- 结果落 `clusters` 表（按 `run_date` 幂等重跑），导出 `reports/top50_YYYYMMDD.md`（主榜 Top 50 + 纯 WPS 榜 Top 20，每条含切入点建议与原文引用）。

```bash
python run_rank.py                     # Top 50，4 周窗口
python run_rank.py --top 20 --dry-run  # 不写库不调 LLM
```

## 手动录入

无合法 API 的高价值源（如 Kickstarter most-funded 榜单）人工浏览后手动录入，入 `raw_signals` 表（source='manual'），与自动采集走同一去重和后续流程。

```bash
python manual_entry.py                      # 交互式，逐字段提示，可连续录入多条
python manual_entry.py --import signals.json  # 批量导入（参考 signals.example.json）
```

批量导入 JSON 为数组，每项字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| title | 是 | 标题 |
| platform | 是 | 来源平台（kickstarter / indiegogo / 朋友推荐 / 自由文本） |
| url | 否 | 证据链接 |
| description | 否 | 描述 |
| amount | 否 | 金额线索（数字） |
| amount_note | 否 | 金额说明原文，进 raw_json |
| signal_type | 是 | 1=外包发布 2=众筹预售 3=招标/采购 4=付费抱怨/求推荐 5=其他（也可直接写中文值） |
| p_level | 否 | 付费信号等级 1-5，不确定留空，后续自动判定 |
| posted_at | 否 | YYYY-MM-DD，默认今天 |
| notes | 否 | 备注，进 raw_json |

source_id 为 `manual_` + title/platform/url 的哈希，同一信号重复录入自动跳过。

## 结构

- `collectors/sam_gov.py` — SAM.gov Get Opportunities API v2
- `collectors/usaspending.py` — USAspending spending_by_award API
- `collectors/appstore.py` — App Store 评论 + 畅销榜快照（榜单端点 `itunes.apple.com/.../topgrossingapplications`，备选 applemarketingtools；榜单 source='appstore_charts'，source_id=`{date}_{rank}_{app_id}`；评论目标 = 固定清单 + 当日 top 100）
- `collectors/cfpb.py` — CFPB 投诉数据库（免 key，signal_type=complaint；注意上游 API 的 `frm` 分页参数自 2025-03 起失效（cfpb/cfpb.github.io#292），本采集器按"逐日 + size=10000 单页"规避，单日总量超限时报 failed）
- `collectors/reddit_rss.py` — Reddit 公开 RSS 兜底（免认证，source='reddit_rss'，5s 间隔 + 429/503 退避）
- `collectors/reddit.py` — Reddit 帖子（官方 Data API，OAuth client_credentials，signal_type=post，raw_json 含 score/num_comments/link_flair_text；当前审批制，有凭证才跑）
- `collectors/freelancer.py` — Freelancer.com 活跃项目（官方 API，`freelancer-oauth-v1` header，signal_type=外包发布/已完成外包，raw_json 含 budget/bids/雇主国家/技能标签）
- `storage.py` — SQLAlchemy 模型与落库（raw_signals 表；启动时自动补 `rating` 列迁移）
- `extractor/rules.py` — 规则抽取（P 级判定 + 金额/预算 cost_hint 正则）
- `extractor/llm.py` — LLM 语义抽取（OpenAI 兼容，evidence quote 契约，token 计量）
- `extractor/pipeline.py` — 抽取流水线（规则全量 + 按源策略调 LLM + 写 extractions 表）
- `run_extract.py` — 抽取入口（--source/--limit/--force/--dry-run/--no-llm）
- `aggregator/clustering.py` — 聚类（关键词/结构化预分组 + Jaccard 并查集 + LLM 合并裁决）
- `aggregator/scoring.py` — WPS / 置信度 / 可进入性打分（PRD §9.2–9.5 原公式）
- `aggregator/fit.py` — 代码可行性打分（实物/PSC/软件关键词规则 + LLM 边界裁决 + 切入点生成）
- `aggregator/barrier.py` — 门槛高度打分（技术/合规/资源/渠道四子项规则 + LLM 边界裁决）
- `aggregator/pipeline.py` — 聚合流水线（窗口加载 → 聚类 → 打分 → 落 clusters 表）
- `run_rank.py` — 排序入口（--top/--weeks/--dry-run/--no-llm，导出 reports/top10_*.md）
- `manual_entry.py` — 手动信号录入 CLI（交互式 + `--import` JSON 批量导入，source='manual'，哈希去重）
- `signals.example.json` — 批量导入格式示例
- `run_daily.py` — 每日入口；各采集器的 `collect(date_from, date_to, session)` 是纯函数，之后可直接包装为 Prefect task。
