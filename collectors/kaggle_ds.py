"""Kaggle datasets collector (needs ~/.kaggle/kaggle.json API token).

Graceful skip when the key is missing. Covers datasets search/trending;
dataset-request discussions live on the forums which need auth — skipped.
"""
import json
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)


def _client():
    kjson = os.path.expanduser("~/.kaggle/kaggle.json")
    token_file = os.path.expanduser("~/.kaggle/access_token")
    env_user = os.environ.get("KAGGLE_USERNAME")
    env_key = os.environ.get("KAGGLE_KEY")

    if os.path.exists(kjson) or (env_user and env_key):
        pass  # official client handles both
    elif os.path.exists(token_file) and env_user:
        # bare-token file: synthesize env credentials for the official client
        os.environ["KAGGLE_USERNAME"] = env_user
        os.environ["KAGGLE_KEY"] = open(token_file).read().strip()
    elif os.path.exists(token_file):
        log.warning("~/.kaggle/access_token 是裸 token（无 username）；"
                    "官方客户端需要 username+key 配对，请同时设置 KAGGLE_USERNAME；跳过")
        return None
    else:
        log.warning("kaggle key missing (~/.kaggle/kaggle.json 或 ~/.kaggle/access_token + "
                    "KAGGLE_USERNAME)；跳过。获取：kaggle.com/settings -> Create New API Token")
        return None
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api = KaggleApi()
        api.authenticate()
        return api
    except ImportError:
        log.warning("kaggle package not installed (uv pip install kaggle); skipping")
        return None
    except Exception as e:
        log.warning("kaggle auth failed: %s", e)
        return None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    api = _client()
    if not api:
        return stats

    now = datetime.utcnow()
    records = []
    try:
        for term in ("market research", "industry", "consumer", "sales"):
            for d in api.dataset_list(search=term, sort_by="hottest", max_size=20):
                ref = str(getattr(d, "ref", "") or "")
                if not ref:
                    continue
                records.append({
                    "source": "kaggle",
                    "source_id": ref,
                    "signal_type": "数据集",
                    "title": getattr(d, "title", None) or ref,
                    "description": (getattr(d, "subtitle", "") or "")[:5000],
                    "amount": None, "naics": None, "state": None,
                    "posted_at": None, "fetched_at": now,
                    "url": f"https://www.kaggle.com/datasets/{ref}",
                    "rating": getattr(d, "voteCount", None),
                    "raw_json": json.dumps({
                        "ref": ref, "search_term": term,
                        "downloads": getattr(d, "downloadCount", None),
                        "votes": getattr(d, "voteCount", None),
                        "usability": getattr(d, "usabilityRating", None),
                    }, ensure_ascii=False),
                })
            stats["requests"] += 1
    except Exception as e:
        log.error("kaggle list failed: %s", e)
        stats["failed"] = 1
        return stats

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("kaggle: %d datasets, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = argparse.ArgumentParser().parse_args()
    from storage import get_session
    print(f"Kaggle: {collect(None, None, get_session())}")


if __name__ == "__main__":
    main()
