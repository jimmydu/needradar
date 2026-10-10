"""YouTube collector via official Data API v3 (free 10,000 units/day).

search.list (100 units/call) with pain-point keywords + duration=short,
then commentThreads.list on the top videos. Reads YOUTUBE_API_KEY; skips
gracefully when unset. Prefect wiring: `collect(date_from, date_to)`.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
# pain-intent keywords (same style as the reddit subs list); kept short on
# purpose: each entry costs 100 quota units
SEARCH_QUERIES = [
    "looking for an app", "is there an app that", "alternative to",
    "cancel subscription", "too expensive app", "frustrated with",
]
MAX_COMMENT_VIDEOS = 5
COMMENTS_PER_VIDEO = 20
MAX_RETRIES = 3


def _get(path, params, quota):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(f"{API_BASE}{path}", params=params, timeout=30)
        except requests.RequestException as e:
            log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 403:
            log.warning("quota exhausted or key invalid (HTTP 403): %s", resp.text[:200])
            return None
        if resp.status_code == 429:
            time.sleep(2 ** attempt * 5)
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.warning("YOUTUBE_API_KEY not set; skipping youtube source "
                    "(get a key: Google Cloud Console -> enable YouTube Data API v3 -> create API key)")
        return stats

    now = datetime.utcnow()
    start_dt = datetime.combine(date_from, datetime.min.time())
    records = []
    videos = []

    for q in SEARCH_QUERIES:
        data = _get("/search", {
            "key": api_key, "part": "snippet", "q": q, "type": "video",
            "videoDuration": "short", "order": "date", "maxResults": 10,
            "publishedAfter": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, stats)
        stats["requests"] += 1
        if not data:
            stats["failed"] += 1
            break  # quota/key problem: stop early
        for item in data.get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            sn = item.get("snippet") or {}
            if not vid:
                continue
            videos.append((vid, sn.get("title") or ""))
            records.append({
                "source": "youtube",
                "source_id": f"video_{vid}",
                "signal_type": "post",
                "title": sn.get("title"),
                "description": (sn.get("description") or "")[:5000],
                "amount": None, "naics": None, "state": None,
                "posted_at": _parse_dt(sn.get("publishedAt")),
                "fetched_at": now,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "rating": None,
                "raw_json": json.dumps({"query": q, "kind": "video", "snippet": sn},
                                       ensure_ascii=False),
            })
        time.sleep(1)

    # top comments on a few videos (pain evidence often lives in comments)
    for vid, vtitle in videos[:MAX_COMMENT_VIDEOS]:
        data = _get("/commentThreads", {
            "key": api_key, "part": "snippet", "videoId": vid,
            "order": "relevance", "maxResults": COMMENTS_PER_VIDEO,
        }, stats)
        stats["requests"] += 1
        if not data:
            continue
        for item in data.get("items", []):
            c = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
            cid = item.get("id")
            if not cid:
                continue
            records.append({
                "source": "youtube",
                "source_id": f"comment_{cid}",
                "signal_type": "comment",
                "title": f"Comment on: {vtitle[:80]}",
                "description": (c.get("textDisplay") or "")[:5000],
                "amount": None, "naics": None, "state": None,
                "posted_at": _parse_dt(c.get("publishedAt")),
                "fetched_at": now,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "rating": None,
                "raw_json": json.dumps({"kind": "comment", "video_id": vid,
                                        "likeCount": c.get("likeCount"),
                                        "text": c.get("textDisplay")}, ensure_ascii=False),
            })
        time.sleep(1)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("youtube: %d videos, %d records total", len(videos), len(records))
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="YouTube collector (Data API v3)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"YouTube {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
