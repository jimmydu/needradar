"""Online side-gig platform watcher (题材雷达 + 远程工作报告区块，不进痛点主榜).

For each platform in online_gigs.txt: fetch homepage + FAQ/eligibility page,
extract status fields by keyword rules (open registration, China acceptance,
payout rails, hourly rates, task types), snapshot page hashes for change
detection. Missing fields -> manual_review.
"""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LIST_FILE = os.environ.get("ONLINE_GIGS_FILE", "./online_gigs.txt")
STATE_FILE = os.environ.get("ONLINE_GIGS_STATE", "./online_gigs_state.json")
INTERVAL = 3.0

_OPEN_RE = re.compile(r"(join|sign ?up|register|apply|get started|start earning)", re.I)
_CLOSED_RE = re.compile(r"(not currently (accepting|hiring)|waitlist|closed to new)", re.I)
_CHINA_RE = re.compile(r"china|chinese|mainland", re.I)
_PAY_RE = re.compile(r"paypal|payoneer|wise|alipay|支付宝", re.I)
_RATE_RE = re.compile(r"\$\s?\d+(?:\.\d+)?\s*(?:-|–|to)?\s*\$?\d*(?:\.\d+)?\s*/?\s*(?:per\s)?(?:hour|hr|小时)?", re.I)
_TASK_RE = re.compile(r"annotation|labeling|survey|testing|transcription|translation|"
                      r"writing|review|data collection|user test", re.I)


def load_platforms(path=LIST_FILE):
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            out.append({"name": parts[0], "home": parts[1] if len(parts) > 1 else None,
                        "faq": parts[2] if len(parts) > 2 and parts[2] else None,
                        "note": parts[3] if len(parts) > 3 else ""})
    return out


def _fetch(url):
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if resp.status_code == 200:
            return resp.text
        log.warning("HTTP %d for %s", resp.status_code, url)
        return None
    except requests.RequestException as e:
        log.warning("request error %s: %s", url, e)
        return None


def _fields(text):
    low = text.lower()
    return {
        "open": "closed" if _CLOSED_RE.search(low) else ("open" if _OPEN_RE.search(low) else "unknown"),
        "china": "mentioned" if _CHINA_RE.search(low) else "not_found",
        "payout": sorted(set(_PAY_RE.findall(low))),
        "rates": sorted(set(m.group(0) for m in _RATE_RE.finditer(text)))[:6],
        "tasks": sorted(set(m.group(0).lower() for m in _TASK_RE.finditer(low))),
    }


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "platforms": {}}
    now = datetime.utcnow()
    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))

    records = []
    for p in load_platforms():
        pages = [u for u in (p["home"], p["faq"]) if u]
        texts, hashes = [], {}
        ok = True
        for u in pages:
            html = _fetch(u)
            stats["requests"] += 1
            if html is None:
                ok = False
                stats["platforms"][p["name"]] = "failed"
                break
            texts.append(html)
            hashes[u] = hashlib.sha256(html.encode()).hexdigest()[:12]
            stats["platforms"][p["name"]] = "ok"
            time.sleep(INTERVAL)
        if not ok:
            stats["failed"] += 1
            records.append({
                "source": "online_gigs", "source_id": p["name"],
                "signal_type": "平台监控",
                "title": f"{p['name']} 状态：抓取失败（manual_review）",
                "description": p["note"], "amount": None, "naics": None,
                "state": None, "posted_at": now, "fetched_at": now,
                "url": p["home"], "rating": None,
                "raw_json": json.dumps({"platform": p["name"], "status": "fetch_failed",
                                        "note": p["note"]}, ensure_ascii=False),
            })
            continue

        fields = _fields("\n".join(texts))
        changed = {u: (state.get(u) != h) for u, h in hashes.items()}
        is_change = any(state.get(u) and state.get(u) != h for u, h in hashes.items())
        fields["changed_pages"] = [u for u, c in changed.items() if c]
        fields["status_change"] = is_change
        stats["platforms"][p["name"]] = f"ok, open={fields['open']}, china={fields['china']}"

        records.append({
            "source": "online_gigs", "source_id": p["name"],
            "signal_type": "平台监控",
            "title": f"{p['name']}：注册={fields['open']}，中国={fields['china']}",
            "description": p["note"], "amount": None, "naics": None,
            "state": None, "posted_at": now, "fetched_at": now,
            "url": p["home"], "rating": None,
            "raw_json": json.dumps({"platform": p["name"], "note": p["note"],
                                    "hashes": hashes, **fields}, ensure_ascii=False),
        })
        state.update(hashes)

    json.dump(state, open(STATE_FILE, "w"))
    # 监控型源：同平台重复出现应更新（状态变化是新闻级信号），不是跳过
    from storage import RawSignal
    inserted = updated = 0
    for rec in records:
        prev = session.query(RawSignal).filter_by(
            source="online_gigs", source_id=rec["source_id"]).first()
        if prev:
            prev.raw_json = rec["raw_json"]
            prev.fetched_at = now
            prev.title = rec["title"]
            updated += 1
        else:
            session.add(RawSignal(**rec))
            inserted += 1
    session.commit()
    stats["inserted"] = inserted
    stats["updated"] = updated
    log.info("online_gigs: %d platforms, %d inserted, %d updated", len(records), inserted, updated)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = argparse.ArgumentParser().parse_args()
    from storage import get_session
    print(collect(None, None, get_session()))


if __name__ == "__main__":
    main()
