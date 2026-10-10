"""Indie top-apps report: enrich the latest chart snapshots with iTunes
Lookup data, score indie-ness, and write reports/indie_top_apps_YYYYMMDD.md.

Revenue tiers are ROUGH buckets inferred from chart rank, not ARR estimates
— the report says so. Lookup results are cached in the app_indie table
(7-day refresh) so daily runs only fetch new/changed apps.
"""
import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import requests

from storage import AppIndie, RawSignal, get_session

log = logging.getLogger(__name__)

LOOKUP_URL = "https://itunes.apple.com/lookup"
BLACKLIST_FILE = os.environ.get("INDIE_BLACKLIST_FILE", "./indie_blacklist.txt")
CACHE_DAYS = 7
LOOKUP_INTERVAL = 0.3
ARTIST_INTERVAL = 0.5

_COMPANY_SUFFIX = re.compile(
    r"\b(llc|inc|ltd|corp|co\.|company|studios|games|labs|group|technologies|"
    r"interactive|media|networks|ventures|holdings|enterprises)\b", re.I)


def load_blacklist(path=BLACKLIST_FILE):
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                out.append(_norm(line))
    return out


def _lookup(params):
    for attempt in range(3):
        try:
            resp = requests.get(LOOKUP_URL, params=params, timeout=30)
            if resp.status_code == 200:
                return (resp.json().get("results") or [])
        except (requests.RequestException, ValueError) as e:
            log.warning("lookup error (%s), retry %d/3", e, attempt + 1)
        time.sleep(2 ** attempt)
    return []


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _score(row, blacklist):
    """Recompute indie score from cached fields (called every run, so blacklist
    or heuristic changes take effect without refetching)."""
    seller = (row.seller_name or "").strip()
    hit = next((b for b in blacklist if b in _norm(seller)), None)
    score, reasons = 0, []
    rc = row.user_rating_count or 0
    if rc > 1_000_000:
        score -= 2
        reasons.append(f"评分数巨大({rc})，非小体量")
    if hit:
        reasons.append(f"大厂黑名单:{hit}")
    else:
        if row.artist_app_count is not None:
            if row.artist_app_count <= 5:
                score += 2; reasons.append(f"名下app≤5({row.artist_app_count})")
            elif row.artist_app_count <= 15:
                score += 1; reasons.append(f"名下app≤15({row.artist_app_count})")
        words = seller.split()
        if (len(words) == 2 and all(w[:1].isupper() for w in words)
                and not _COMPANY_SUFFIX.search(seller)):
            score += 1; reasons.append("sellerName像个人姓名")
        if row.seller_url:
            score += 1; reasons.append("有独立官网")
        if rc < 200_000:
            score += 1; reasons.append(f"评分数较小({rc})")
    return hit, score, "; ".join(reasons)


def enrich_app(session, app_id, blacklist):
    """Return AppIndie row (cached <= 7 days, else refetched). Score is always
    recomputed from cached fields."""
    row = session.get(AppIndie, str(app_id))
    if row and row.updated_at and (datetime.utcnow() - row.updated_at).days < CACHE_DAYS:
        row.blacklist_hit, row.indie_score, row.indie_reasons = _score(row, blacklist)
        return row

    results = _lookup({"id": app_id, "country": "us"})
    time.sleep(LOOKUP_INTERVAL)
    if not results:
        return row
    app = results[0]
    seller = app.get("sellerName") or ""
    artist_id = str(app.get("artistId") or "")

    artist_app_count = None
    if artist_id and (not row or row.artist_id != artist_id or row.artist_app_count is None):
        artist_apps = _lookup({"id": artist_id, "entity": "software", "country": "us"})
        time.sleep(ARTIST_INTERVAL)
        artist_app_count = len(artist_apps) or None

    if row is None:
        row = AppIndie(app_id=str(app_id))
    row.name = app.get("trackName")
    row.seller_name = seller
    row.artist_id = artist_id
    row.artist_app_count = artist_app_count
    row.seller_url = app.get("sellerUrl")
    row.user_rating_count = app.get("userRatingCount")
    try:
        row.price = float(app.get("price") or 0)
    except (TypeError, ValueError):
        row.price = None
    row.updated_at = datetime.utcnow()
    row.blacklist_hit, row.indie_score, row.indie_reasons = _score(row, blacklist)
    session.merge(row)
    session.commit()
    return row


def revenue_tier(rank):
    """Rough bucket from grossing rank — NOT an ARR estimate."""
    if rank <= 10:
        return ">100万美元（粗估）"
    if rank <= 50:
        return "10-100万美元（粗估）"
    return "<10万美元（粗估）"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Indie top-apps report")
    p.add_argument("--date", help="snapshot date, default latest")
    p.add_argument("--top", type=int, default=50)
    args = p.parse_args()

    session = get_session()
    q = session.query(RawSignal).filter_by(source="appstore_charts")
    snap_date = args.date or q.order_by(RawSignal.id.desc()).first().source_id.split("_")[0]
    rows = q.all()
    entries = []
    for r in rows:
        raw = json.loads(r.raw_json or "{}")
        if raw.get("snapshot_date") != snap_date:
            continue
        entries.append({"app_id": raw["app_id"], "title": r.title,
                        "feed": raw["feed"], "genre": raw["genre"],
                        "rank": raw["rank"], "price": raw.get("price")})
    log.info("snapshot %s: %d chart entries, %d unique apps",
             snap_date, len(entries), len({e["app_id"] for e in entries}))

    blacklist = load_blacklist()
    best = {}  # app_id -> best overall topgrossing rank (genre charts tracked separately)
    for e in entries:
        b = best.setdefault(e["app_id"], {"rank": None, "genres": set(), "feeds": set(),
                                          "title": e["title"], "price": e["price"]})
        if e["feed"] == "topgrossingapplications" and e["genre"] == "overall":
            b["rank"] = e["rank"] if b["rank"] is None else min(b["rank"], e["rank"])
        b["genres"].add(e["genre"])
        b["feeds"].add(e["feed"])

    indies, excluded = [], 0
    for app_id, b in best.items():
        info = enrich_app(session, app_id, blacklist)
        if not info:
            continue
        if info.blacklist_hit or (info.indie_score or 0) < 2:
            excluded += 1
            continue
        indies.append({"app_id": app_id, "rank": b["rank"], "genres": b["genres"],
                       "feeds": b["feeds"], "info": info})
    indies.sort(key=lambda x: (x["rank"] is None, x["rank"] or 999, -x["info"].indie_score))
    log.info("indie pass: %d, excluded: %d", len(indies), excluded)

    today = date.today().isoformat()
    lines = [
        f"# 小体量高收入 App 榜（{today}，榜单快照 {snap_date}）",
        "",
        "筛选：top-grossing/top-paid 总榜 + productivity/utilities/developer-tools/health-fitness/education "
        "品类榜，iTunes Lookup 富化 + indie 启发式（名下 app 数、sellerName 形态、独立官网、评分数；大厂黑名单排除）。",
        "**收入层级是按畅销总榜榜位的粗分桶，不是 ARR 估算，仅供量级参考；仅上品类榜的标注「量级不明」。**",
        "",
    ]
    for rank, x in enumerate(indies[:args.top], 1):
        info = x["info"]
        feeds = "/".join(sorted(f.replace("applications", "") for f in x["feeds"]))
        tier = revenue_tier(x["rank"]) if x["rank"] else "仅品类榜上榜（量级不明）"
        rank_str = f"#{x['rank']}" if x["rank"] else "未上总榜"
        lines.append(
            f"{rank}. **{info.name}**（{info.seller_name}）— 畅销总榜 {rank_str}，"
            f"层级 {tier}，价格 ${info.price or 0:.2f}，"
            f"评分 {info.user_rating_count or 0} 条，上榜：{feeds}｜品类 {', '.join(sorted(x['genres']))}\n"
            f"   indie 分 {info.indie_score}（{info.indie_reasons}）｜{info.seller_url or ''} "
            f"https://apps.apple.com/us/app/id{x['app_id']}")
        lines.append("")

    path = f"reports/indie_top_apps_{today.replace('-', '')}.md"
    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n共 {len(indies)} 个 indie app（排除 {excluded} 个）")
    for rank, x in enumerate(indies[:10], 1):
        rs = f"#{x['rank']}" if x["rank"] else "genre-only"
        print(f"{rank:2d}. {x['info'].name} — {rs} {x['info'].seller_name} score={x['info'].indie_score}")
    print(f"报告已导出: {path}")


if __name__ == "__main__":
    main()
