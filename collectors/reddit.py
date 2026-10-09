"""Reddit collector via the official Data API (OAuth2 client_credentials).

Credentials from env: REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USERNAME.
If not configured, the collector logs a hint and returns zero stats.
Free tier is non-commercial (~100 QPM). Prefect wiring: `collect()` entrypoint.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
SUBS_FILE = os.environ.get("REDDIT_SUBS_FILE", "./reddit_subs.txt")
MAX_RETRIES = 3
DESC_MAX = 5000


def load_subs(path=SUBS_FILE):
    subs = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                subs.append(line)
    return subs


def _credentials():
    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    user = os.environ.get("REDDIT_USERNAME", "unknown")
    if not cid or not secret:
        log.warning("Reddit credentials not configured (set REDDIT_CLIENT_ID / "
                    "REDDIT_CLIENT_SECRET / REDDIT_USERNAME); skipping reddit source")
        return None
    return cid, secret, f"needradar/0.1 by {user}"


def _get_token(cid, secret, ua):
    resp = requests.post(
        TOKEN_URL,
        auth=(cid, secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": ua},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get(url, headers):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("rate limited, waiting %ds", wait)
            time.sleep(wait)
            continue
        if resp.status_code in (403, 404):
            log.warning("HTTP %d for %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        return resp.json()
    return None


def collect(date_from, date_to, session):
    """Fetch /new posts from monitored subreddits; keep posts >= date_from."""
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    creds = _credentials()
    if not creds:
        return stats
    cid, secret, ua = creds

    token = _get_token(cid, secret, ua)
    headers = {"Authorization": f"bearer {token}", "User-Agent": ua}
    now = datetime.utcnow()
    start_ts = datetime.combine(date_from, datetime.min.time()).timestamp()

    for sub in load_subs():
        data = _get(f"{API_BASE}/r/{sub}/new?limit=100", headers)
        stats["requests"] += 1
        if not data:
            stats["failed"] += 1
            continue
        posts = (data.get("data") or {}).get("children") or []

        records = []
        for p in posts:
            d = p.get("data") or {}
            created = d.get("created_utc")
            if not created or created < start_ts:
                continue
            permalink = d.get("permalink") or ""
            records.append({
                "source": "reddit",
                "source_id": d.get("name"),  # t3_xxx fullname
                "signal_type": "post",
                "title": d.get("title"),
                "description": (d.get("selftext") or "")[:DESC_MAX],
                "amount": None,
                "naics": None,
                "state": None,
                "posted_at": datetime.utcfromtimestamp(created),
                "fetched_at": now,
                "url": f"https://www.reddit.com{permalink}" if permalink else None,
                "rating": None,
                "raw_json": json.dumps({
                    "subreddit": sub,
                    "score": d.get("score"),
                    "num_comments": d.get("num_comments"),
                    "link_flair_text": d.get("link_flair_text"),
                    "post": d,
                }, ensure_ascii=False),
            })
        records = [r for r in records if r["source_id"]]
        ins, skip = upsert_signals(session, records)
        stats["inserted"] += ins
        stats["skipped"] += skip
        log.info("r/%s -> %d in window, %d inserted", sub, len(records), ins)
        time.sleep(1)

    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Reddit posts collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Reddit {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
