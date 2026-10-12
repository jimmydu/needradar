"""Discord public-mirror collector via AnswerOverflow threads, transported
through the Wayback Machine CDX API (both answeroverflow.com and Linen are
behind Vercel security checkpoints that block plain HTTP from this host;
Wayback archived snapshots are a clean, auth-free channel).

Degraded by nature: only threads that were archived, sparse and not fresh.
Daily cadence is harmless (CDX query is cheap); collects threads archived in
the window.
"""
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

UA = "needradar/0.1 (contact: local research project)"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
SNAP_URL = "https://web.archive.org/web/{ts}/{url}"
# AO thread pages: /m/{message_id}; community name is in <title> after " - "
HOSTS = ["www.answeroverflow.com", "questions.answeroverflow.com"]
INTERVAL = 3.0
MAX_SNAPSHOTS = 50          # per day-window per host
STATE_FILE = os.environ.get("DISCORD_AO_STATE_FILE", "./discord_ao_state.json")
PAIN_WORDS = ("help", "how do i", "how to", "frustrating", "doesn't work",
              "doesnt work", "broken", "issue", "error", "problem", "stuck",
              "not working", "fails", "crash")
CONSUMER_HINTS = ("game", "gaming", "minecraft", "mod", "server", "chat",
                  "community", "fan", "music", "anime", "art")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_OGD_RE = re.compile(r'og:description" content="([^"]*)')


def _cdx_threads(host, from_date, to_date, backfill=False):
    params = {
        "url": f"{host}/m/*", "output": "json",
        "filter": "statuscode:200", "limit": MAX_SNAPSHOTS, "collapse": "urlkey",
    }
    if backfill:
        # Wayback 存档稀疏且滞后：首次回填取最近存档的快照（sort=reverse 倒序），
        # 不看窗口；后续增量再按 from/to 过滤
        params["sort"] = "reverse"
    else:
        params["from"] = from_date.strftime("%Y%m%d")
        params["to"] = to_date.strftime("%Y%m%d")
    for attempt in range(3):
        try:
            resp = requests.get(CDX_URL, params=params,
                                headers={"User-Agent": UA}, timeout=40)
            if resp.status_code == 200:
                rows = resp.json()
                return rows[1:] if len(rows) > 1 else []
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            log.warning("CDX HTTP %d", resp.status_code)
            return []
        except (requests.RequestException, ValueError) as e:
            log.warning("CDX error (%s), retry %d/3", e, attempt + 1)
            time.sleep(2 ** attempt)
    return []


def _fetch_snapshot(ts, url):
    for attempt in range(3):
        try:
            resp = requests.get(SNAP_URL.format(ts=ts, url=url),
                                headers={"User-Agent": UA}, timeout=40)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 503):
                wait = 10 * (attempt + 1)  # Wayback 对连续快照抓取会 503，长退避
                log.warning("snapshot HTTP %d, backing off %ds", resp.status_code, wait)
                time.sleep(wait)
                continue
            return None
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return None


def _parse_thread(html):
    t = _TITLE_RE.search(html)
    title = t.group(1).strip() if t else None
    if not title or " - " not in title:
        return None, None, None
    question, community = title.rsplit(" - ", 1)
    d = _OGD_RE.search(html)
    desc = d.group(1).strip() if d else None
    return question.strip(), community.strip(), desc


def collect(date_from, date_to, session):
    from storage import upsert_signals

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "requests": 0}
    now = datetime.utcnow()
    records = []

    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    backfill = not state.get("last_archived_ts")

    for host in HOSTS:
        rows = _cdx_threads(host, date_from, date_to, backfill=backfill)
        stats["requests"] += 1
        log.info("CDX %s: %d snapshots (%s)", host, len(rows),
                 "backfill" if backfill else "window")
        newest_ts = state.get("last_archived_ts")
        for row in rows:
            ts, orig = row[1], row[2]
            if newest_ts and ts <= newest_ts:
                continue  # 增量：只取比上次存档时间新的
            html = _fetch_snapshot(ts, orig)
            stats["requests"] += 1
            if not html:
                stats["failed"] += 1
                time.sleep(INTERVAL)
                continue
            question, community, desc = _parse_thread(html)
            if not question:
                stats["skipped"] += 1
                time.sleep(INTERVAL)
                continue
            text = f"{question} {desc or ''}".lower()
            pain = any(w in text for w in PAIN_WORDS)
            consumer = any(h in community.lower() for h in CONSUMER_HINTS)
            archived_at = datetime.strptime(ts, "%Y%m%d%H%M%S")
            records.append({
                "source": "discord_ao",
                "source_id": orig.rsplit("/", 1)[-1],
                "signal_type": "post",
                "title": question,
                "description": (desc or "")[:5000] or None,
                "amount": None, "naics": None, "state": None,
                "posted_at": archived_at,
                "fetched_at": now,
                "url": orig,
                "rating": None,
                "raw_json": json.dumps({
                    "community": community, "host": host,
                    "archived_at": ts, "wayback_url": SNAP_URL.format(ts=ts, url=orig),
                    "pain_hint": pain,
                    "consumer_product": consumer,
                    "degraded": "via Wayback Machine archive (live site Vercel-blocked)",
                }, ensure_ascii=False),
            })
            state["last_archived_ts"] = max(ts, state.get("last_archived_ts") or "")
            time.sleep(INTERVAL)
        json.dump(state, open(STATE_FILE, "w"))

    ins, skip = upsert_signals(session, records)
    stats["inserted"] = ins
    stats["skipped"] = skip
    log.info("discord_ao: %d threads, %d inserted", len(records), ins)
    return stats


def main():
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Discord AO mirror collector (via Wayback)")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to - timedelta(days=7)

    from storage import get_session
    stats = collect(date_from, date_to, get_session())
    print(f"Discord AO {date_from}..{date_to}: {stats}")


if __name__ == "__main__":
    main()
