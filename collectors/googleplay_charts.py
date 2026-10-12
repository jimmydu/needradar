"""Google Play top-charts collector via the Node google-play-scraper package
(scripts/gplay/charts.mjs — `npm install` in scripts/gplay is required).

Snapshots into raw_signals (source='gplay_charts'), and indie-looking apps
become 成功样本 signals (source='gplay_indie') reusing the AppIndie cache and
indie_blacklist (developer-name heuristics adapted for Play: sellerName-like
developer field, ratings count, developer app-count via search).
"""
import json
import logging
import os
import re
import subprocess
import time
from datetime import date, datetime

import requests

from storage import AppIndie, RawSignal

log = logging.getLogger(__name__)

NODE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "scripts", "gplay", "charts.mjs")
COLLECTIONS = ["GROSSING", "TOP_PAID"]
CATEGORIES = ["APPLICATION", "PRODUCTIVITY", "TOOLS", "HEALTH_AND_FITNESS",
              "EDUCATION", "BUSINESS", "MEDICAL", "LIFESTYLE", "FINANCE",
              "SHOPPING", "FOOD_AND_DRINK", "TRAVEL_AND_LOCAL", "SOCIAL",
              "PHOTOGRAPHY"]
CHART_NUM = 100
REQUEST_INTERVAL = 2.0
BLACKLIST_FILE = os.environ.get("INDIE_BLACKLIST_FILE", "./indie_blacklist.txt")

_COMPANY_SUFFIX = re.compile(
    r"\b(llc|inc|ltd|corp|co\.|company|studios|games|labs|group|technologies|"
    r"interactive|media|networks|ventures|holdings|enterprises|pte|gmbh|sarl)\b", re.I)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _load_blacklist():
    out = []
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    out.append(_norm(line))
    return out


def _run_node(collection, category, num):
    try:
        r = subprocess.run(
            ["node", NODE_SCRIPT, collection, category, str(num)],
            capture_output=True, text=True, timeout=120, cwd=os.path.dirname(NODE_SCRIPT))
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("node charts failed (%s) %s/%s", e, collection, category)
        return None
    if r.returncode != 0:
        log.warning("node charts rc=%d %s/%s: %s", r.returncode, collection, category,
                    r.stderr[:200])
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        log.warning("node charts bad json %s/%s", collection, category)
        return None


def _app_details(app_id):
    """Extra fields via python google-play-scraper (ratings count etc.)."""
    try:
        from google_play_scraper import app as gapp
        return gapp(app_id, country="us")
    except Exception as e:
        log.warning("gplay app(%s) failed: %s", app_id, e)
        return None


def _score_indie(developer, ratings, artist_app_count, blacklist):
    hit = next((b for b in blacklist if b in _norm(developer)), None)
    score, reasons = 0, []
    if hit:
        return hit, score, f"大厂黑名单:{hit}"
    if artist_app_count is not None:
        if artist_app_count <= 5:
            score += 2; reasons.append(f"名下app≤5({artist_app_count})")
        elif artist_app_count <= 15:
            score += 1; reasons.append(f"名下app≤15({artist_app_count})")
    words = developer.split()
    if (len(words) == 2 and all(w[:1].isupper() for w in words)
            and not _COMPANY_SUFFIX.search(developer)):
        score += 1; reasons.append("开发者名像个人姓名")
    rc = ratings or 0
    if rc < 200_000:
        score += 1; reasons.append(f"评分数较小({rc})")
    elif rc > 1_000_000:
        score -= 2; reasons.append(f"评分数巨大({rc})，非小体量")
    return hit, score, "; ".join(reasons)


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    today = date.today().isoformat()
    now = datetime.utcnow()
    blacklist = _load_blacklist()

    chart_records = []
    indie_candidates = {}  # appId -> best grossing rank
    for coll in COLLECTIONS:
        for cat in CATEGORIES:
            items = _run_node(coll, cat, CHART_NUM)
            stats["requests"] += 1
            if not items:
                stats["failed"] += 1
                time.sleep(REQUEST_INTERVAL)
                continue
            feed = {"GROSSING": "topgrossing", "TOP_PAID": "toppaid"}[coll]
            for it in items:
                chart_records.append({
                    "source": "gplay_charts",
                    "source_id": f"{today}_{feed}_{cat}_{it['rank']}_{it['appId']}",
                    "signal_type": "chart_rank",
                    "title": it["title"],
                    "description": None,
                    "amount": None, "naics": None, "state": None,
                    "posted_at": datetime.combine(date.today(), datetime.min.time()),
                    "fetched_at": now,
                    "url": f"https://play.google.com/store/apps/details?id={it['appId']}",
                    "rating": None,
                    "raw_json": json.dumps({"store": "google_play", "snapshot_date": today,
                                            "feed": feed, "genre": cat, **it},
                                           ensure_ascii=False),
                })
                if feed == "topgrossing":
                    prev = indie_candidates.get(it["appId"])
                    if not prev or it["rank"] < prev["rank"]:
                        indie_candidates[it["appId"]] = it
            time.sleep(REQUEST_INTERVAL)

    ins, skip = upsert_signals(session, chart_records)
    stats["inserted"] += ins
    stats["skipped"] += skip
    log.info("gplay charts: %d entries, %d unique grossing apps", len(chart_records),
             len(indie_candidates))

    # indie enrichment for top-grossing apps (cache in app_indie, 7 days)
    indie_records = []
    for app_id, it in indie_candidates.items():
        cached = session.get(AppIndie, f"gp:{app_id}")
        fresh = cached and (datetime.utcnow() - cached.updated_at).days < 7
        if fresh:
            continue
        details = _app_details(app_id)
        time.sleep(0.5)
        ratings = (details or {}).get("ratings") or it.get("ratings")
        artist_app_count = None
        dev_id = it.get("developerId") or (details or {}).get("developerId")
        if dev_id:
            try:
                from google_play_scraper import search as gsearch
                artist_app_count = len(gsearch(f"dev:{dev_id}", n_hits=30, country="us"))
            except Exception:
                pass
            time.sleep(0.5)
        if cached is None:
            cached = AppIndie(app_id=f"gp:{app_id}")
        cached.name = it["title"]
        cached.seller_name = it["developer"]
        cached.artist_id = str(dev_id or "")
        cached.artist_app_count = artist_app_count
        cached.user_rating_count = ratings
        cached.price = it.get("price")
        hit, score, reasons = _score_indie(it["developer"], ratings,
                                           artist_app_count, blacklist)
        cached.blacklist_hit = hit
        cached.indie_score = score
        cached.indie_reasons = reasons
        cached.updated_at = now
        session.merge(cached)
        session.commit()

    # emit 成功样本 signals from cached indie rows (score>=2, not blacklisted)
    for app_id, it in indie_candidates.items():
        cached = session.get(AppIndie, f"gp:{app_id}")
        if not cached or cached.blacklist_hit or (cached.indie_score or 0) < 2:
            continue
        tier = ">100万美元（粗估）" if it["rank"] <= 10 else (
            "10-100万美元（粗估）" if it["rank"] <= 50 else "<10万美元（粗估）")
        indie_records.append({
            "source": "gplay_indie",
            "source_id": app_id,
            "signal_type": "成功样本",
            "title": f"{it['title']} — Google Play 小体量高收入独立 App",
            "description": (f"{it['title']}（{it['developer']}）：Google Play 畅销榜 "
                            f"#{it['rank']}（{tier}），评分 {cached.user_rating_count or 0} 条，"
                            f"indie 分 {cached.indie_score}（{cached.indie_reasons}）。"),
            "amount": None, "naics": None, "state": None,
            "posted_at": now, "fetched_at": now,
            "url": f"https://play.google.com/store/apps/details?id={app_id}",
            "rating": None,
            "raw_json": json.dumps({"store": "google_play", "developer": it["developer"],
                                    "rank": it["rank"], "tier": tier,
                                    "user_rating_count": cached.user_rating_count,
                                    "indie_score": cached.indie_score,
                                    "rank_history": {today: it["rank"]},
                                    "genre": it.get("genre")}, ensure_ascii=False),
        })
    ins2, skip2 = upsert_signals(session, indie_records)
    stats["inserted"] += ins2
    stats["skipped"] += skip2
    stats["indie_signals"] = len(indie_records)
    log.info("gplay indie signals: %d (%d new)", len(indie_records), ins2)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Google Play charts collector")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"Google Play charts: {stats}")


if __name__ == "__main__":
    main()
