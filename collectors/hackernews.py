"""Hacker News collector via Algolia HN Search API (no key).

Focus: Ask HN help posts (pain) and Show HN threads + complaint comments.
Who-is-hiring is NOT collected (hiring has its own pipeline).
Incremental via state file (last objectID timestamp).
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_URL = "https://hn.algolia.com/api/v1/search_by_date"
STATE_FILE = os.environ.get("HN_STATE_FILE", "./hackernews_state.json")
INTERVAL = 1.0
QUERIES = [
    "Ask HN frustrated", "Ask HN looking for", "Ask HN is there",
    "Ask HN how do you", "Ask HN alternative", "Ask HN tool",
    "Show HN",
]
MAX_PAGES_PER_QUERY = 2


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _search(query, page=0, tags=None):
    params = {"query": query, "hitsPerPage": 50, "page": page}
    if tags:
        params["tags"] = tags
    for attempt in range(3):
        try:
            resp = requests.get(API_URL, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
        except (requests.RequestException, ValueError) as e:
            log.warning("request error (%s), retry %d/3", e, attempt + 1)
            time.sleep(2 ** attempt)
    return None


def _parse_dt(ts):
    try:
        return datetime.utcfromtimestamp(int(ts))
    except (ValueError, TypeError):
        return None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    start_ts = datetime.combine(date_from, datetime.min.time()).timestamp()
    records = []

    for q in QUERIES:
        tags = "comment" if q == "Show HN" else "story"
        # Show HN 帖本身 + 其评论里的抱怨（评论通道抓 comment 并保留父帖）
        for page in range(MAX_PAGES_PER_QUERY):
            data = _search(q, page, tags)
            stats["requests"] += 1
            if not data:
                stats["failed"] += 1
                break
            hits = data.get("hits", [])
            for h in hits:
                ts = h.get("created_at_i")
                if ts and ts < start_ts:
                    continue
                t = (h.get("title") or "").strip()
                if not t or t == "[dead]":
                    continue  # 被踩死/无标题帖无信号价值
                oid = h.get("objectID")
                if not oid:
                    continue
                is_comment = tags == "comment"
                title = h.get("title") or (h.get("story_title") if is_comment else None)
                body = h.get("comment_text") if is_comment else h.get("text")
                records.append({
                    "source": "hackernews",
                    "source_id": str(oid),
                    "signal_type": "post",
                    "title": title,
                    "description": (body or "")[:5000] or None,
                    "amount": None, "naics": None, "state": None,
                    "posted_at": _parse_dt(ts),
                    "fetched_at": now,
                    "url": f"https://news.ycombinator.com/item?id={oid}",
                    "rating": h.get("points"),
                    "raw_json": json.dumps({
                        "query": q, "kind": "comment" if is_comment else "story",
                        "points": h.get("points"), "num_comments": h.get("num_comments"),
                        "author": h.get("author"),
                    }, ensure_ascii=False),
                })
            if len(hits) < 50:
                break
            time.sleep(INTERVAL)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("hackernews: %d records, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Hacker News collector")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"HN {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
