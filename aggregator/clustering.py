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

# canonical topic -> trigger keywords (lowercase substring match on tokens)
KEYWORD_BUCKETS = {
    "价格过高/订阅收费": {"expensive", "overpriced", "price", "pricing", "pricy", "costly",
                      "subscription", "subscriptions", "paywall", "upgrade"},
    "退款/计费纠纷": {"refund", "refunds", "charged", "charge", "billing", "billed",
                   "unauthorized", "auto-renewal", "renewal", "cancellation", "cancel", "cancelled"},
    "崩溃/闪退/故障": {"crash", "crashes", "crashing", "freeze", "freezes", "freezing",
                   "bug", "bugs", "buggy", "broken", "glitch", "error", "errors"},
    "运行缓慢/性能": {"slow", "slowly", "lag", "laggy", "lagging", "loading", "performance",
                   "unresponsive", "responsive", "sluggish"},
    "同步/备份问题": {"sync", "syncing", "synchronization", "backup", "backups"},
    "通知/提醒问题": {"notification", "notifications", "badge", "badges", "alert", "alerts"},
    "登录/账户问题": {"login", "log", "signin", "password", "verification", "locked", "suspended",
                   "account"},
    "客服/支持差": {"support", "customer", "service", "agent", "agents", "response", "help"},
    "发票/收付款/记账": {"invoice", "invoices", "invoicing", "payment", "payments", "payroll",
                      "transaction", "transactions", "check", "deposit", "bookkeeping", "accounting",
                      "quickbooks", "bank", "banking", "checking"},
    "数据丢失/迁移": {"lost", "lose", "losing", "missing", "deleted", "disappeared", "gone",
                   "wipe", "wiped", "migration"},
    "AI 功能问题": {"ai", "bot", "chatbot", "gpt", "agent"},
    "离线/网络依赖": {"offline", "internet", "connection", "wifi", "online"},
    "界面/易用性": {"interface", "ui", "ux", "intuitive", "confusing", "clunky", "navigation",
                 "design", "layout", "unusable"},
    "功能缺失/请求": {"feature", "features", "wish", "add", "missing", "lack", "lacks", "lacking",
                   "need", "needs", "option", "parity"},
    "更新后变差": {"update", "updates", "updated", "version", "since"},
    "信用报告/征信": {"credit", "report", "reporting", "score", "equifax", "experian", "transunion"},
    "催收/债务": {"debt", "collection", "collections", "collector", "harassment"},
    "欺诈/盗刷": {"fraud", "scam", "stolen", "identity", "theft", "dispute"},
    "抵押贷款/房贷": {"mortgage", "loan", "loans", "lender", "servicing", "escrow"},
    "政府合同/采购": {"solicitation", "contract", "contracts", "rfp", "bid", "procurement", "award"},
    "招聘/外包人力": {"hire", "hiring", "freelancer", "contractor", "developer", "designer"},
}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{1,}|[一-鿿]{2,}")


def tokenize(text):
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS}


def doc_text(signal, extraction):
    return extraction.pain_point or signal.title or ""


def bucket_key(signal, extraction, raw):
    """Assign a pre-grouping bucket: structured fields first, then keywords."""
    src = signal.source
    if src == "cfpb":
        return f"cfpb:{(raw.get('issue') or 'misc')[:60]}"
    if src in ("sam_gov", "usaspending"):
        return f"gov:{signal.naics or 'misc'}"
    tokens = tokenize(doc_text(signal, extraction))
    for topic, kws in KEYWORD_BUCKETS.items():
        if tokens & kws:
            return f"kw:{topic}"
    app = raw.get("app_name") or raw.get("app_id")
    if src == "appstore" and app:
        return f"app:{app}"
    if src in ("reddit_rss", "reddit"):
        return f"sub:{raw.get('subreddit') or 'misc'}"
    sig_tokens = sorted(tokens)[:3]
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
    """members: list of (signal, extraction, raw_dict). Returns list of clusters,
    each a list of member tuples."""
    buckets = defaultdict(list)
    for m in members:
        buckets[bucket_key(*m)].append(m)

    clusters = []
    for key, group in buckets.items():
        token_sets = [tokenize(doc_text(s, e) + " " + (s.title or "")) for s, e, _ in group]
        uf = UnionFind(range(len(group)))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if _jaccard(token_sets[i], token_sets[j]) >= jaccard_threshold:
                    uf.union(i, j)
        merged = defaultdict(list)
        for i, m in enumerate(group):
            merged[uf.find(i)].append(m)
        clusters.extend(merged.values())
    return clusters


def cluster_tokens(cluster):
    cnt = Counter()
    for s, e, _ in cluster:
        cnt.update(tokenize(doc_text(s, e) + " " + (s.title or "")))
    return cnt


def find_merge_candidates(clusters, threshold=0.2, max_pairs=30):
    """Cross-bucket cluster pairs with high token overlap, for LLM arbitration."""
    cents = [cluster_tokens(c) for c in clusters]
    sets = [set(c) for c in cents]
    pairs = []
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            sim = _jaccard(sets[i], sets[j])
            if sim >= threshold:
                pairs.append((i, j, sim))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:max_pairs]
