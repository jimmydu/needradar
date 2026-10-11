"""Daily entrypoint: run all collectors sequentially and print a summary.

Each collector runs in its own try/except so one failure never blocks the
rest. Later wiring to Prefect: each collector's `collect()` is already a pure
(date_from, date_to, session) function, ready to become a Prefect task.
"""
import argparse
import logging
from datetime import date, datetime, timedelta

from collectors import (appstore, appstore_charts, betalist, cfpb, freelancer,
                        github, google_trends, googleplay_charts, hf_datasets,
                        indiegogo, kaggle_ds, kickstarter, news_rss, reddit,
                        reddit_rss, remotejobs, sam_gov, shopify_apps,
                        stackexchange, techcrunch, usaspending, youtube)
from storage import get_session

log = logging.getLogger(__name__)

COLLECTORS = [
    ("sam_gov", sam_gov),
    ("usaspending", usaspending),
    ("appstore", appstore),
    ("cfpb", cfpb),
    ("reddit_rss", reddit_rss),
    ("reddit", reddit),
    ("freelancer", freelancer),
    ("indiegogo", indiegogo),
    ("kickstarter", kickstarter),
    ("youtube", youtube),
    ("news", news_rss),
    ("appstore_charts", appstore_charts),
    ("betalist", betalist),
    ("gplay_charts", googleplay_charts),
    ("remotejobs", remotejobs),
    ("shopify_apps", shopify_apps),
    ("stackexchange", stackexchange),
    ("techcrunch", techcrunch),
    ("github", github),
    ("google_trends", google_trends),
    ("kaggle", kaggle_ds),
    ("hf_datasets", hf_datasets),
]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="NeedRadar daily collection")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--source", choices=[n for n, _ in COLLECTORS] + ["all"], default="all")
    p.add_argument("--skip", default="", help="逗号分隔的源名，本次跳过")
    args = p.parse_args()
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").date() if args.date_to else date.today() - timedelta(days=1)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else date_to

    session = get_session()
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    summary = {}
    for name, mod in COLLECTORS:
        if args.source not in (name, "all") or name in skip:
            continue
        try:
            summary[name] = mod.collect(date_from, date_to, session)
        except Exception as e:
            log.error("collector %s crashed: %s", name, e)
            summary[name] = {"inserted": 0, "skipped": 0, "failed": 1, "requests": 0}

    print("\n===== NeedRadar collection summary =====")
    print(f"window: {date_from} .. {date_to}")
    for src, s in summary.items():
        print(f"{src:12s} inserted={s['inserted']:6d} skipped={s['skipped']:6d} "
              f"failed={s['failed']} requests={s['requests']}")


if __name__ == "__main__":
    main()
