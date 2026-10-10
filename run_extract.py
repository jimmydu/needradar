"""Signal extraction entrypoint.

Examples:
    python run_extract.py --dry-run --limit 50          # rules only, no writes
    python run_extract.py --source appstore --limit 50  # rules + LLM (<=3* reviews)
    python run_extract.py --no-llm                      # rules only, write results
"""
import argparse
import logging

from extractor import llm
from extractor.pipeline import extract_batch
from storage import get_session


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="NeedRadar signal extraction")
    p.add_argument("--source", help="只处理某个 source（如 appstore）")
    p.add_argument("--limit", type=int, default=0, help="最多处理条数，0=不限")
    p.add_argument("--force", action="store_true", help="重抽已有 extraction 的信号")
    p.add_argument("--dry-run", action="store_true", help="只跑规则、打印结果，不写库不调 LLM")
    p.add_argument("--no-llm", action="store_true", help="只跑规则抽取并写库")
    args = p.parse_args()

    session = get_session()
    stats = extract_batch(
        session,
        source=args.source,
        limit=args.limit or None,
        force=args.force,
        dry_run=args.dry_run,
        use_llm=not args.no_llm,
    )
    print("\n===== extraction summary =====")
    for k, v in stats.items():
        print(f"{k:22s} {v}")
    if not args.no_llm and not args.dry_run:
        cfg = llm.resolve_config("heavy")
        print(f"{'model':22s} {cfg[1] if cfg else '(none)'}")


if __name__ == "__main__":
    main()
