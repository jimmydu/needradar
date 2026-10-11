"""Hugging Face Datasets collector (public Hub API, no key).

Trending/most-downloaded datasets — AI-circle demand signals, tagged as such
(raw_json.circle='ai'). No per-item dates in the list API; daily snapshot,
dedupe by dataset id.
"""
import json
import logging
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)

API_URL = "https://huggingface.co/api/datasets"
MAX_RETRIES = 3


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    records = []
    for sort, params in (
        ("trending", {"sort": "trending", "limit": 50}),
        ("downloads", {"sort": "downloads", "direction": "-1", "limit": 50}),
    ):
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(API_URL, params=params, timeout=30)
                if resp.status_code == 200:
                    items = resp.json()
                    stats["requests"] += 1
                    break
                if resp.status_code == 429:
                    time.sleep(2 ** attempt * 5)
                    continue
                resp.raise_for_status()
            except requests.RequestException as e:
                log.warning("request error (%s), retry %d/%d", e, attempt + 1, MAX_RETRIES)
                time.sleep(2 ** attempt)
        else:
            stats["failed"] += 1
            continue
        for d in items:
            did = d.get("id")
            if not did:
                continue
            desc = (d.get("description") or "").strip()
            records.append({
                "source": "hf_datasets",
                "source_id": did,
                "signal_type": "数据集",
                "title": did,
                "description": desc[:5000] or None,
                "amount": None, "naics": None, "state": None,
                "posted_at": now, "fetched_at": now,
                "url": f"https://huggingface.co/datasets/{did}",
                "rating": d.get("likes"),
                "raw_json": json.dumps({
                    "sort": sort, "likes": d.get("likes"),
                    "downloads": d.get("downloads"),
                    "lastModified": d.get("lastModified"),
                    "tags": d.get("tags"), "circle": "ai",
                }, ensure_ascii=False),
            })
        time.sleep(2)

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("hf_datasets: %d items, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = argparse.ArgumentParser().parse_args()
    from storage import get_session
    print(f"HF datasets: {collect(None, None, get_session())}")


if __name__ == "__main__":
    main()
