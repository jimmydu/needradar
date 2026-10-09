"""Daily entrypoint: run all collectors sequentially and print a summary.

Later wiring to Prefect: each collector's `collect()` is already a pure
(date_from, date_to, session) function, ready to become a Prefect task.
"""
import argparse
import logging
from datetime import date, datetime, timedelta

from collectors import appstore, cfpb, freelancer, reddit, reddit_rss, sam_gov, usaspending
from storage import get_session

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="NeedRadar daily collection")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--source", choices=["sam_gov", "usaspending", "appstore", "cfpb",
                                        "reddit_rss", "reddit", "freelancer", "all"], default="all")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    session = get_session()
    summary = {}
    if args.source in ("sam_gov", "all"):
        summary["sam_gov"] = sam_gov.collect(date_from, date_to, session)
    if args.source in ("usaspending", "all"):
        summary["usaspending"] = usaspending.collect(date_from, date_to, session)
    if args.source in ("appstore", "all"):
        summary["appstore"] = appstore.collect(date_from, date_to, session)
    if args.source in ("cfpb", "all"):
        summary["cfpb"] = cfpb.collect(date_from, date_to, session)
    if args.source in ("reddit_rss", "all"):
        summary["reddit_rss"] = reddit_rss.collect(date_from, date_to, session)
    if args.source in ("reddit", "all"):
        summary["reddit"] = reddit.collect(date_from, date_to, session)
    if args.source in ("freelancer", "all"):
        summary["freelancer"] = freelancer.collect(date_from, date_to, session)

    print("\n===== NeedRadar collection summary =====")
    print(f"window: {date_from} .. {date_to}")
    for src, s in summary.items():
        print(f"{src:12s} inserted={s['inserted']:6d} skipped={s['skipped']:6d} "
              f"failed={s['failed']} requests={s['requests']}")


if __name__ == "__main__":
    main()
