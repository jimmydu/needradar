"""Keyword/rule-based pre-grouping + union-find clustering.

Per cost policy, no per-signal LLM calls: signals are bucketed by curated
domain keywords (or structured fields like CFPB issue / NAICS), merged
within buckets by token Jaccard, and only cross-bucket cluster pairs with
high overlap go to the LLM for merge arbitration (see pipeline).
"""
import re
from collections import Counter, defaultdict

STOPWORDS = set("""
a an the and or of to in on for with is are was were be been it its this that these those
i me my we our you your he she they them their as at by from not no but so if then than
have has had do does did can could will would should may might must
app apps use used using user users get got really just like even still also one two
im ive dont doesnt didnt cant couldnt wont wouldnt isnt arent wasnt werent
the a an de la el en 了 的 是 我 你 这 那 有 在 和 就 都 很 不 也 用 无法 没有 一个
""".split())

# Canonical topics, priority-ordered: first match wins. Latin keywords match
# on word boundaries, CJK keywords as substrings. A signal lands in exactly
# one topic and the topic itself is the cluster (per product decision).
CANONICAL_TOPICS = [
    ("订阅与扣费", ["subscription", "billing", "billed", "refund", "refunded", "charged",
                 "chargeback", "cancel", "cancelled", "cancellation", "free trial", "trial",
                 "auto-renew", "renewal", "unauthorized charge", "renew",
                 "订阅", "扣费", "扣款", "退款", "取消", "续费", "试用", "付费墙"]),
    ("定价与涨价", ["expensive", "overpriced", "price", "pricing", "price increase",
                 "too much", "afford", "涨价", "太贵", "价格", "费用高", "年费"]),
    ("崩溃与稳定性", ["crash", "crashes", "crashing", "freeze", "freezes", "bug", "bugs",
                   "buggy", "broken", "glitch", "error", "闪退", "崩溃", "卡死", "故障"]),
    ("性能与速度", ["slow", "slowly", "lag", "laggy", "lagging", "loading", "performance",
                 "unresponsive", "sluggish", "卡顿", "缓慢", "太慢", "响应慢"]),
    ("数据丢失与同步", ["sync", "syncing", "backup", "lost", "lose", "losing", "missing",
                    "deleted", "disappeared", "gone", "wiped", "migration",
                    "丢失", "同步", "不见", "消失", "备份"]),
    ("广告过多", ["ads", "advertisement", "advertising", "ad-filled", "广告"]),
    ("AI 功能反感", ["ai", "chatbot", "gpt", "llm", "人工智能"]),
    ("客服与支持", ["support", "customer service", "customer support", "no response",
                 "help desk", "客服", "售后"]),
    ("发票与记账", ["invoice", "invoices", "invoicing", "payment", "payments", "payroll",
                 "bookkeeping", "accounting", "quickbooks", "transaction", "transactions",
                 "deposit", "checking", "发票", "记账", "收款", "付款", "报销"]),
    ("预约与排程", ["scheduling", "schedule", "booking", "bookings", "appointment",
                 "calendar", "预约", "排期", "日程"]),
    ("登录与账户", ["login", "log in", "sign in", "password", "verification", "locked",
                 "suspended", "account", "banned", "登录", "账户", "封号", "验证"]),
    ("通知与提醒", ["notification", "notifications", "badge", "alert", "alerts", "remind",
                 "通知", "提醒"]),
    ("界面与易用性", ["interface", "ui", "ux", "intuitive", "confusing", "clunky",
                   "navigation", "layout", "unusable", "hard to use", "难用", "界面", "易用"]),
    ("离线与网络依赖", ["offline", "internet connection", "no connection", "wifi", "离线", "网络"]),
    ("欺诈与盗刷", ["fraud", "scam", "stolen", "identity theft", "phishing", "欺诈", "盗刷", "诈骗"]),
    ("催收与债务", ["debt", "collection", "collections", "collector", "harassment", "催收", "债务"]),
    ("信用报告", ["credit report", "credit score", "equifax", "experian", "transunion", "征信"]),
    ("贷款与房贷", ["mortgage", "loan", "loans", "lender", "escrow", "贷款", "房贷"]),
    ("政府合同与采购", ["solicitation", "rfp", "rfq", "procurement", "bid", "contract award"]),
    ("招聘与外包", ["hire", "hiring", "freelancer", "contractor", "outsourc", "外包", "招聘"]),
    ("功能缺失与请求", ["feature", "features", "wish", "missing feature", "lack", "lacks",
                    "add an option", "parity", "功能", "希望", "建议增加"]),
]

_LATIN_KW = {t: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws if not re.search(r"[一-鿿]", k)) + r")\b")
             for t, kws in CANONICAL_TOPICS}
_CJK_KW = {t: [k for k in kws if re.search(r"[一-鿿]", k)] for t, kws in CANONICAL_TOPICS}


def canonical_topic(signal, extraction, raw):
    """First matching canonical topic, or None. Gov procurement sources are
    excluded: their titles are procurement jargon, not user-pain language,
    and they already have structured NAICS buckets. News is a topic radar
    (trigger events), not user-pain evidence — excluded from pain topics so
    headlines don't hijack topic summaries or inflate evidence counts.
    Crowdfunding sources are products, not pain posts — excluded likewise."""
    if signal.source in ("sam_gov", "usaspending", "news", "kickstarter", "indiegogo"):
        return None
    text = " ".join(filter(None, [
        extraction.pain_point, signal.title,
        raw.get("issue") or "", raw.get("product") or "",
    ])).lower()
    if not text:
        return None
    for topic, _ in CANONICAL_TOPICS:
        rx = _LATIN_KW.get(topic)
        if rx and rx.search(text):
            return topic
        if any(k in text for k in _CJK_KW.get(topic, [])):
            return topic
    return None

_CJK_RE = re.compile(r"[一-鿿]+")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{1,}|[一-鿿]+")


def tokenize(text):
    tokens = set()
    for tok in _TOKEN_RE.findall((text or "").lower()):
        if _CJK_RE.fullmatch(tok):
            # no segmentation for CJK: use char bigrams so similar phrases overlap
            tokens.update(tok[i:i + 2] for i in range(len(tok) - 1))
        elif tok not in STOPWORDS:
            tokens.add(tok)
    return tokens


def doc_text(signal, extraction):
    return extraction.pain_point or signal.title or ""


def residual_bucket_key(signal, extraction, raw):
    """Fallback bucket for signals outside the canonical topics."""
    src = signal.source
    if src == "cfpb":
        return f"cfpb:{(raw.get('issue') or 'misc')[:60]}"
    if src in ("sam_gov", "usaspending"):
        return f"gov:{signal.naics or 'misc'}"
    if src == "news":
        # trigger-event label from the extraction layer groups news into
        # opportunity-type buckets (e.g. 短缺/积压→调度)
        return f"news:{extraction.trigger_type or 'misc'}"
    if src in ("kickstarter", "indiegogo"):
        # crowdfunding products cluster by KS/IGG category
        return f"crowd:{str(raw.get('category') or raw.get('projectType') or 'misc')[:40]}"
    app = raw.get("app_name") or raw.get("app_id")
    if src == "appstore" and app:
        return f"app:{app}"
    if src in ("reddit_rss", "reddit"):
        return f"sub:{raw.get('subreddit') or 'misc'}"
    sig_tokens = sorted(tokenize(doc_text(signal, extraction)))[:3]
    return f"misc:{'_'.join(sig_tokens) or signal.source}"


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_signals(members, jaccard_threshold=0.3):
    """members: list of (signal, extraction, raw_dict). Returns list of
    (cluster_key, members). Canonical-topic members form one cluster per
    topic ("topic:<name>"); the rest fall back to structured buckets with
    token-Jaccard merging ("res:<bucket>")."""
    topics = defaultdict(list)
    residuals = []
    for m in members:
        t = canonical_topic(*m)
        if t:
            topics[t].append(m)
        else:
            residuals.append(m)

    clusters = [(f"topic:{t}", g) for t, g in topics.items()]

    buckets = defaultdict(list)
    for m in residuals:
        buckets[residual_bucket_key(*m)].append(m)
    for key, group in buckets.items():
        if key.startswith("crowd:"):
            # crowdfunding: the category itself is the theme (one project is
            # not a demand theme); skip token-level splitting entirely
            clusters.append((f"res:{key}", group))
            continue
        token_sets = [tokenize(doc_text(s, e) + " " + (s.title or "")) for s, e, _ in group]
        uf = UnionFind(range(len(group)))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if _jaccard(token_sets[i], token_sets[j]) >= jaccard_threshold:
                    uf.union(i, j)
        merged = defaultdict(list)
        for i, m in enumerate(group):
            merged[uf.find(i)].append(m)
        clusters.extend((f"res:{key}", c) for c in merged.values())
    return clusters


def cluster_tokens(cluster):
    cnt = Counter()
    for s, e, _ in cluster:
        cnt.update(tokenize(doc_text(s, e) + " " + (s.title or "")))
    return cnt


def find_merge_candidates(clusters, threshold=0.2, max_pairs=30):
    """Cross-bucket cluster pairs with high token overlap, for LLM arbitration.
    clusters: list of (bucket_key, members)."""
    cents = [cluster_tokens(m) for _, m in clusters]
    sets = [set(c) for c in cents]
    pairs = []
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            sim = _jaccard(sets[i], sets[j])
            if sim >= threshold:
                pairs.append((i, j, sim))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:max_pairs]
