"""Targeted low-star review collection for indie-success apps.

Reads targeted_apps (populated by run_indie_report.py — indie apps, with
fast_rising flag for rapid chart climbers). Apple via official RSS JSON
(most-recent pages, keep rating<=3), Google Play via google-play-scraper
reviews (sort NEWEST, filter_score<=3).
"""
import json
import logging
import time
from datetime import datetime

import requests

from storage import RawSignal, TargetedApp

log = logging.getLogger(__name__)

RSS_URL = "https://itunes.apple.com/us/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
MAX_PAGES = 4
INTERVAL = 0.8
DESC_MAX = 5000


def _get_json(url):
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/3", e, attempt + 1)
        time.sleep(2 ** attempt)
    return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _apple_reviews(app_id, start_dt):
    out = []
    for page in range(1, MAX_PAGES + 1):
        data = _get_json(RSS_URL.format(page=page, app_id=app_id))
        entries = ((data or {}).get("feed") or {}).get("entry") or []
        entries = [e for e in entries if e.get("im:rating")]
        for e in entries:
            rating = int((e.get("im:rating") or {}).get("label") or 0)
            posted = _parse_dt((e.get("updated") or {}).get("label"))
            if rating > 3:
                continue
            if posted and posted < start_dt:
                continue
            rid = (e.get("id") or {}).get("label")
            if not rid:
                continue
            out.append({
                "id": str(rid), "rating": rating, "posted": posted,
                "title": (e.get("title") or {}).get("label"),
                "content": (e.get("content") or {}).get("label"),
                "voteCount": (e.get("im:voteCount") or {}).get("label"),
            })
        if len(entries) < 50:
            break
        time.sleep(INTERVAL)
    return out


def _gplay_reviews(app_id, start_dt):
    try:
        from google_play_scraper import reviews, Sort
    except ImportError:
        log.warning("google-play-scraper not installed")
        return []
    out = []
    try:
        result, _ = reviews(app_id, lang="en", country="us",
                            sort=Sort.NEWEST, count=200, filter_score_with=3)
    except Exception as e:
        log.warning("gplay reviews(%s) failed: %s", app_id, e)
        return out
    for r in result:
        posted = r.get("at")
        if posted and posted.replace(tzinfo=None) < start_dt:
            continue
        out.append({
            "id": r.get("reviewId"), "rating": r.get("score"), "posted": posted,
            "title": None, "content": r.get("content"),
            "voteCount": r.get("thumbsUpCount"),
        })
    return out


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    start_dt = datetime.combine(date_from, datetime.min.time())
    now = datetime.utcnow()
    targets = session.query(TargetedApp).all()
    log.info("targeted reviews: %d apps on the list", len(targets))

    records = []
    for t in targets:
        if t.store == "apple":
            revs = _apple_reviews(t.app_id, start_dt)
        else:
            revs = _gplay_reviews(t.app_id, start_dt)
        stats["requests"] += 1
        for rv in revs:
            records.append({
                "source": "targeted_review",
                "source_id": f"{t.store}_{rv['id']}",
                "signal_type": "review",
                "title": rv["title"] or f"{t.name} 差评",
                "description": (rv["content"] or "")[:DESC_MAX],
                "amount": None, "naics": None, "state": None,
                "posted_at": rv["posted"], "fetched_at": now,
                "url": None, "rating": rv["rating"],
                "raw_json": json.dumps({
                    "app_id": t.app_id, "app_name": t.name, "store": t.store,
                    "target_reason": t.reason, "voteCount": rv["voteCount"],
                }, ensure_ascii=False),
            })
        log.info("%s (%s): %d low-star reviews in window", t.name, t.app_id, len(revs))
        time.sleep(INTERVAL)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Targeted low-star review collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    from datetime import date, timedelta
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Targeted reviews {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
