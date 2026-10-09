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
- `manual_entry.py` — 手动信号录入 CLI（交互式 + `--import` JSON 批量导入，source='manual'，哈希去重）
- `signals.example.json` — 批量导入格式示例
- `run_daily.py` — 每日入口；各采集器的 `collect(date_from, date_to, session)` 是纯函数，之后可直接包装为 Prefect task。
