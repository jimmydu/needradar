"""App Store chart snapshots: top-grossing + top-paid, overall + key genres.

Daily snapshots into raw_signals (source='appstore_charts'), source_id
includes date/feed/genre/rank so rank movements are computable later.
The old itunes.apple.com RSS works from this machine; the newer
applemarketingtools.com endpoint times out — not used.
"""
import json
import logging
import time
from datetime import date, datetime

import requests

log = logging.getLogger(__name__)

FEEDS = ["topgrossingapplications", "toppaidapplications"]
GENRES = {  # None = overall
    None: "overall",
    "6007": "productivity",
    "6002": "utilities",
    "6026": "developer-tools",
    "6013": "health-fitness",
    "6017": "education",
}
RSS_URL = "https://itunes.apple.com/us/rss/{feed}/limit=100{genre}/json"
REQUEST_INTERVAL = 0.8


def _get(url):
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/3", e, attempt + 1)
        time.sleep(2 ** attempt)
    return None


def collect(date_from, date_to, session):
    """Snapshot all configured feeds. date_from/date_to ignored (daily snapshot)."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    today = date.today().isoformat()
    now = datetime.utcnow()

    for feed in FEEDS:
        for genre_id, genre_name in GENRES.items():
            genre_part = f"/genre={genre_id}" if genre_id else ""
            data = _get(RSS_URL.format(feed=feed, genre=genre_part))
            stats["requests"] += 1
            entries = ((data or {}).get("feed") or {}).get("entry") or []
            if not entries:
                stats["failed"] += 1
                time.sleep(REQUEST_INTERVAL)
                continue
            records = []
            for rank, e in enumerate(entries, 1):
                attrs = (e.get("id") or {}).get("attributes") or {}
                app_id = attrs.get("im:id")
                if not app_id:
                    continue
                records.append({
                    "source": "appstore_charts",
                    "source_id": f"{today}_{feed}_{genre_name}_{rank}_{app_id}",
                    "signal_type": "chart_rank",
                    "title": (e.get("im:name") or {}).get("label"),
                    "description": None,
                    "amount": None, "naics": None, "state": None,
                    "posted_at": datetime.combine(date.today(), datetime.min.time()),
                    "fetched_at": now,
                    "url": f"https://apps.apple.com/us/app/id{app_id}",
                    "rating": None,
                    "raw_json": json.dumps({
                        "snapshot_date": today, "feed": feed, "genre": genre_name,
                        "rank": rank, "app_id": app_id,
                        "category": ((e.get("category") or {}).get("attributes") or {}).get("label"),
                        "artist": (e.get("im:artist") or {}).get("label"),
                        "price": ((e.get("im:price") or {}).get("attributes") or {}).get("amount"),
                    }, ensure_ascii=False),
                })
            ins, skip = upsert_signals(session, records)
            stats["inserted"] += ins
            stats["skipped"] += skip
            log.info("charts %s/%s -> %d entries, %d inserted", feed, genre_name, len(entries), ins)
            time.sleep(REQUEST_INTERVAL)
    return stats


def main():
    import argparse
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="App Store chart snapshot collector")
    args = p.parse_args()
    from storage import get_session
    stats = collect(None, None, get_session())
    print(f"App Store charts: {stats}")


if __name__ == "__main__":
    main()
