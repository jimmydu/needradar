"""Kickstarter collector via Webrobots monthly datasets (zero-risk fallback).

Direct scraping is Cloudflare-blocked (403), so we use Webrobots' monthly
full-site CSV snapshots: https://webrobots.io/kickstarter-datasets/
(~100MB zip). Monthly cadence: the collector records the last processed
dataset month in kickstarter_state.json and exits instantly when the latest
dataset is not newer, so daily runs cost nothing.

Dedup + growth: same project appears in every monthly snapshot. Existing
rows are UPDATED (amount/backers), and the pledged delta vs the previous
snapshot is recorded in raw_json.pledged_delta / pledged_history — monthly
growth is a signal for scoring.
"""
import csv
import io
import json
import logging
import os
import re
import zipfile
from datetime import date, datetime, timedelta

import requests

from aggregator.fit import PHYSICAL_KEYWORDS
from storage import RawSignal

log = logging.getLogger(__name__)

INDEX_URL = "https://webrobots.io/kickstarter-datasets/"
STATE_FILE = os.environ.get("KICKSTARTER_STATE_FILE", "./kickstarter_state.json")
CATEGORY_WHITELIST = {"Apps", "Software", "Web", "Webcomics", "Video Games"}
MAIN_CATEGORY_WHITELIST = {"Technology", "Games"}
LIVE_WINDOW_DAYS = 365  # keep projects launched within a year of the snapshot

_LINK_RE = re.compile(
    r"https://s3\.amazonaws\.com/weruns/forfun/Kickstarter/Kickstarter_"
    r"(\d{4}-\d{2}-\d{2})T[\d_]+Z\.zip")


def latest_dataset_url():
    html = requests.get(INDEX_URL, timeout=30).text
    links = _LINK_RE.findall(html)
    urls = re.findall(
        r"https://s3\.amazonaws\.com/weruns/forfun/Kickstarter/Kickstarter_[^\">]+\.zip", html)
    if not urls or not links:
        return None, None
    # page is newest-first; pick max by date anyway
    best = max(zip(links, urls), key=lambda t: t[0])
    return best[1], best[0]


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _download(url, dest, max_bytes=300_000_000):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("dataset too large, aborting")
                f.write(chunk)
    return total


def _parse_category(raw):
    """category column is a JSON blob with name/parent_name."""
    try:
        d = json.loads(raw or "{}")
        return (d.get("parent_name") or "").strip(), (d.get("name") or "").strip()
    except (ValueError, AttributeError):
        return "", (raw or "").strip()


def _parse_urls(raw):
    try:
        return ((json.loads(raw or "{}").get("web") or {}).get("project"))
    except (ValueError, AttributeError):
        return None


def _parse_location_state(raw):
    try:
        return json.loads(raw or "{}").get("state")
    except (ValueError, AttributeError):
        return None


def _looks_software(name, blurb, category, main_category):
    if category in CATEGORY_WHITELIST:
        return True
    if main_category not in MAIN_CATEGORY_WHITELIST:
        return False
    text = f"{name} {blurb}".lower()
    if any(k in text for k in ("app", "software", "saas", "platform", "tool")):
        return not (set(text.split()) & PHYSICAL_KEYWORDS)
    return False


def _to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0, "updated": 0}

    url, ds_date = latest_dataset_url()
    stats["requests"] += 1
    if not url:
        log.error("kickstarter: no dataset link found on index page")
        stats["failed"] = 1
        return stats
    state = _load_state()
    if state.get("dataset_month") == ds_date[:7]:
        log.info("kickstarter: dataset %s already processed, skip", ds_date[:7])
        return stats
    log.info("kickstarter: downloading dataset %s from %s", ds_date, url)

    tmp = "/tmp/kickstarter_dataset.zip"
    try:
        size = _download(url, tmp)
        stats["requests"] += 1
        log.info("downloaded %.1f MB", size / 1e6)
    except Exception as e:
        log.error("download failed: %s", e)
        stats["failed"] = 1
        return stats

    now = datetime.utcnow()
    min_launched = (datetime.strptime(ds_date, "%Y-%m-%d")
                    - timedelta(days=LIVE_WINDOW_DAYS)).timestamp()

    zf = zipfile.ZipFile(tmp)
    existing = {r.source_id: r for r in
                session.query(RawSignal).filter_by(source="kickstarter").all()}
    new_records = []
    updates = 0
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        with zf.open(name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
            for row in reader:
                main_cat, cat = _parse_category(row.get("category"))
                rname = row.get("name") or ""
                blurb = row.get("blurb") or ""
                if not _looks_software(rname, blurb, cat, main_cat):
                    continue
                ks_state = row.get("state") or ""
                # suspended/canceled/failed are never payment-intent evidence.
                # Note: the monthly snapshot can lag KS moderation actions — a
                # project suspended mid-campaign may still read "live" here
                # (observed 2026-10-10). The quality floor below catches the
                # worst of these ($90 / 1 backer class of dead projects).
                if ks_state not in ("live", "successful"):
                    continue
                pledged = _to_float(row.get("usd_pledged") or row.get("converted_pledged_amount")
                                    or row.get("pledged"))
                backers = int(float(row.get("backers_count") or 0))
                if ks_state == "live" and (pledged or 0) < 500 and backers < 5:
                    continue  # dead/traction-free live project = noise
                try:
                    launched = float(row.get("launched_at") or 0)
                except ValueError:
                    launched = 0
                if launched < min_launched:
                    continue
                pid = str(row.get("id") or "").strip()
                if not pid:
                    continue
                proj_url = _parse_urls(row.get("urls"))
                state_code = _parse_location_state(row.get("location"))
                # signal time: live projects are still raising until their
                # deadline — use min(deadline, now); finished ones keep launch
                if ks_state == "live":
                    try:
                        deadline = float(row.get("deadline") or 0)
                    except ValueError:
                        deadline = 0
                    posted_at = datetime.utcfromtimestamp(min(deadline, now.timestamp())) \
                        if deadline else datetime.strptime(ds_date, "%Y-%m-%d")
                else:
                    posted_at = datetime.utcfromtimestamp(launched) if launched else None

                prev = existing.get(pid)
                if prev:
                    prev_amount = prev.amount or 0
                    delta = (pledged or 0) - prev_amount
                    if delta != 0:
                        prev.amount = pledged
                        raw = json.loads(prev.raw_json or "{}")
                        hist = raw.get("pledged_history") or {}
                        hist[ds_date] = pledged
                        raw["pledged_history"] = hist
                        raw["pledged_delta"] = round(delta, 2)
                        raw["backers_count"] = backers
                        raw["dataset_date"] = ds_date
                        prev.raw_json = json.dumps(raw, ensure_ascii=False)
                        prev.fetched_at = now
                        updates += 1
                    else:
                        stats["skipped"] += 1
                    continue

                new_records.append({
                    "source": "kickstarter",
                    "source_id": pid,
                    "signal_type": "众筹预售",
                    "title": rname,
                    "description": blurb[:5000],
                    "amount": pledged,
                    "naics": None,
                    "state": state_code,
                    "posted_at": posted_at,
                    "fetched_at": now,
                    "url": proj_url,
                    "rating": None,
                    "raw_json": json.dumps({
                        "goal": _to_float(row.get("goal")),
                        "pledged": pledged,
                        "backers_count": backers,
                        "category": cat,
                        "main_category": main_cat,
                        "status": row.get("state"),
                        "country": row.get("country"),
                        "slug": row.get("slug"),
                        "dataset_date": ds_date,
                        "pledged_history": {ds_date: pledged},
                    }, ensure_ascii=False),
                })
    zf.close()
    os.remove(tmp)

    ins, _ = upsert_signals(session, new_records)
    stats["inserted"] = ins
    stats["updated"] = updates
    session.commit()
    state["dataset_month"] = ds_date[:7]
    _save_state(state)
    log.info("kickstarter: %d new, %d updated (delta), %d unchanged",
             ins, updates, stats["skipped"])
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Kickstarter collector (Webrobots monthly CSV)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--force", action="store_true", help="reprocess even if this month was done")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to
    if args.force and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Kickstarter {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
