"""App Store customer reviews collector via Apple's official RSS/JSON endpoint.

No auth required. Only ~500 most recent reviews per app are available.
Prefect wiring: `collect(date_from, date_to)` is the task entrypoint.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

RSS_URL = "https://itunes.apple.com/us/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
CHART_URLS = [
    "https://itunes.apple.com/us/rss/topgrossingapplications/limit=100/json",
    "https://rss.applemarketingtools.com/api/v2/us/apps/top-grossing/100/apps.json",
]
MAX_PAGES = 10  # Apple only serves the ~500 most recent reviews
MAX_RETRIES = 3
APPS_FILE = os.environ.get("APPSTORE_APPS_FILE", "./appstore_apps.txt")


def load_apps(path=APPS_FILE):
    apps = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                parts = line.split()
                apps.append((parts[0], " ".join(parts[1:]) or parts[0]))
    return apps


def _get(url):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
        time.sleep(2 ** attempt)
    return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def fetch_top_grossing(limit=100):
    """Fetch today's US top-grossing chart. Returns list of dicts or None."""
    data = _get(CHART_URLS[0])
    if data and (data.get("feed") or {}).get("entry"):
        entries = data["feed"]["entry"]
        chart = []
        for rank, e in enumerate(entries, 1):
            attrs = (e.get("id") or {}).get("attributes") or {}
            chart.append({
                "rank": rank,
                "app_id": attrs.get("im:id"),
                "name": (e.get("im:name") or {}).get("label"),
                "category": ((e.get("category") or {}).get("attributes") or {}).get("label"),
                "artist": (e.get("im:artist") or {}).get("label"),
            })
        return [c for c in chart if c["app_id"]][:limit]
    data = _get(CHART_URLS[1])
    if data and (data.get("feed") or {}).get("results"):
        return [{
            "rank": rank,
            "app_id": r.get("id"),
            "name": r.get("name"),
            "category": (r.get("genres") or [{}])[0].get("name"),
            "artist": r.get("artistName"),
        } for rank, r in enumerate(data["feed"]["results"], 1)][:limit]
    return None


def collect_charts(session, snapshot_date=None):
    """Snapshot today's top-grossing chart into raw_signals."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "apps": []}
    snapshot_date = snapshot_date or date.today()
    now = datetime.utcnow()

    chart = fetch_top_grossing()
    stats["requests"] = 1 if chart else 2
    if not chart:
        log.error("top-grossing chart fetch failed on both endpoints")
        stats["failed"] = 1
        return stats

    records = [{
        "source": "appstore_charts",
        "source_id": f"{snapshot_date.isoformat()}_{c['rank']}_{c['app_id']}",
        "signal_type": "chart_rank",
        "title": c["name"],
        "description": None,
        "amount": None,
        "naics": None,
        "state": None,
        "posted_at": datetime.combine(snapshot_date, datetime.min.time()),
        "fetched_at": now,
        "url": f"https://apps.apple.com/us/app/id{c['app_id']}",
        "rating": None,
        "raw_json": json.dumps({"snapshot_date": snapshot_date.isoformat(), **c}, ensure_ascii=False),
    } for c in chart]
    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    stats["apps"] = [(c["app_id"], c["name"]) for c in chart]
    log.info("chart snapshot %s -> %d entries, %d inserted", snapshot_date, len(chart), ins)
    return stats


def collect(date_from, date_to, session, include_charts=True):
    """Fetch recent reviews for monitored apps (+ today's chart apps); keep reviews >= date_from."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "invalid_apps": []}
    now = datetime.utcnow()
    start_dt = datetime.combine(date_from, datetime.min.time())

    apps = load_apps()
    if include_charts:
        chart_stats = collect_charts(session)
        stats["chart_inserted"] = chart_stats["inserted"]
        stats["chart_skipped"] = chart_stats["skipped"]
        stats["requests"] += chart_stats["requests"]
        seen = {a for a, _ in apps}
        for app_id, name in chart_stats["apps"]:
            if app_id not in seen:
                apps.append((app_id, name or app_id))
                seen.add(app_id)
        stats["review_targets"] = len(apps)

    for app_id, name in apps:
        app_url = f"https://apps.apple.com/us/app/id{app_id}"
        seen_older = False
        for page in range(1, MAX_PAGES + 1):
            data = _get(RSS_URL.format(page=page, app_id=app_id))
            stats["requests"] += 1
            entries = ((data or {}).get("feed") or {}).get("entry")
            if entries is None:
                if page == 1:
                    log.warning("app %s (%s): no feed, invalid app_id?", app_id, name)
                    stats["invalid_apps"].append(app_id)
                    stats["failed"] += 1
                break
            if isinstance(entries, dict):
                entries = [entries]
            # page 1 first entry is the app metadata entry, not a review
            entries = [e for e in entries if e.get("im:rating")]

            records = []
            for e in entries:
                posted = _parse_dt((e.get("updated") or {}).get("label"))
                if posted and posted < start_dt:
                    seen_older = True
                    continue
                rid = (e.get("id") or {}).get("label")
                if not rid:
                    continue
                rating = (e.get("im:rating") or {}).get("label")
                records.append({
                    "source": "appstore",
                    "source_id": str(rid),
                    "signal_type": "review",
                    "title": (e.get("title") or {}).get("label"),
                    "description": (e.get("content") or {}).get("label"),
                    "amount": None,
                    "naics": None,
                    "state": None,
                    "posted_at": posted,
                    "fetched_at": now,
                    "url": app_url,
                    "rating": int(rating) if rating else None,
                    "raw_json": json.dumps({
                        "app_id": app_id,
                        "app_name": name,
                        "rating": int(rating) if rating else None,
                        "voteCount": (e.get("im:voteCount") or {}).get("label"),
                        "voteSum": (e.get("im:voteSum") or {}).get("label"),
                        "author": ((e.get("author") or {}).get("name") or {}).get("label"),
                        "entry": e,
                    }, ensure_ascii=False),
                })
            ins, skip = upsert_signals(session, records)
            stats["inserted"] += ins
            stats["skipped"] += skip
            log.info("app %s (%s) page=%d -> %d in window, %d inserted",
                     app_id, name, page, len(records), ins)
            if seen_older or len(entries) < 50:
                break
            time.sleep(0.5)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="App Store reviews collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"App Store {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
