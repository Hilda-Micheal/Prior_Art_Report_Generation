"""
main.py
=======
CLI entrypoint — orchestrates the full pipeline:
  1. Parse CLI args
  2. Connect to MongoDB
  3. Check query cache → serve from DB if hit
  4. Fetch from Lens.org API if cache miss
  5. Write CSV
  6. Upsert patents into MongoDB
  7. Store result in query cache
  8. Log the run

Usage:
    python3 main.py --keywords "solar energy" --limit 5
    python3 main.py --cpc H01L --limit 1000
    python3 main.py --ipc H04L29/06 --year-from 2015 --year-to 2022
    python3 main.py --cpc H01L --force          # bypass cache
    python3 main.py --cpc H01L --no-mongo       # CSV only
    python3 main.py --keywords "neural network" --limit 5 --debug
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from config import DEFAULT_LIMIT, DEFAULT_OUTPUT
from db import connect_mongo, cache_lookup, cache_store, save_patents, log_run
from lens_api import build_query, build_sort, fetch_patents
from parser import parse_patent
from utils import make_query_key, save_to_csv, debug_print

load_dotenv()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Retrieve patents from Lens.org with MongoDB caching."
    )
    parser.add_argument("--api-key",   default=None,
                        help="Lens.org API token. Reads LENS_API_KEY from .env if omitted.")
    parser.add_argument("--mongo-uri", default=None,
                        help="MongoDB URI. Reads MONGO_URI from .env, defaults to mongodb://localhost:27017.")
    parser.add_argument("--keywords",  nargs="+", help='e.g. --keywords "solar energy"')
    parser.add_argument("--cpc",       nargs="+", help="e.g. --cpc H01L")
    parser.add_argument("--ipc",       nargs="+", help="e.g. --ipc H04L29/06")
    parser.add_argument("--limit",     type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output",    default=DEFAULT_OUTPUT)
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to",   type=int, default=None)
    parser.add_argument("--no-mongo",  action="store_true",
                        help="Skip MongoDB entirely — CSV output only.")
    parser.add_argument("--force",     action="store_true",
                        help="Ignore cache and always fetch fresh from Lens.org.")
    parser.add_argument("--debug",     action="store_true",
                        help="Print the raw first API record to inspect field names.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args       = parse_args()
    start_time = time.time()
    run_id     = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")

    # --- Validate inputs ---
    api_key = args.api_key or os.getenv("LENS_API_KEY")
    if not api_key:
        print("No API key found. Add LENS_API_KEY=your_key to .env file.")
        sys.exit(1)

    if not any([args.keywords, args.cpc, args.ipc]):
        print("Provide at least one filter: --keywords, --cpc, or --ipc")
        sys.exit(1)

    # --- MongoDB ---
    db = None
    if not args.no_mongo:
        mongo_uri = args.mongo_uri or os.getenv("MONGO_URI", "mongodb://localhost:27017")
        db = connect_mongo(mongo_uri)

    # --- Query summary (stored alongside every cache entry) ---
    has_keywords  = bool(args.keywords)
    sort_label    = "relevance + recency" if has_keywords else "recency only"
    query_summary = {
        "keywords":  args.keywords,
        "cpc_codes": args.cpc,
        "ipc_codes": args.ipc,
        "sort_by":   sort_label,
        "limit":     args.limit,
        "year_from": args.year_from or "any",
        "year_to":   args.year_to   or "any",
        "output":    args.output,
    }

    print(f"\nRun ID : {run_id}")
    print("Query summary:")
    if args.keywords: print(f"  Keywords : {args.keywords}")
    if args.cpc:      print(f"  CPC codes: {args.cpc}")
    if args.ipc:      print(f"  IPC codes: {args.ipc}")
    print(f"  Sort by  : {sort_label}")
    print(f"  Limit    : {args.limit}")
    print(f"  Year from: {args.year_from or 'any'}")
    print(f"  Year to  : {args.year_to or 'any'}")
    print(f"  Output   : {args.output}")

    # --- Cache check ---
    query_key = make_query_key(args.keywords, args.cpc, args.ipc,
                               args.limit, args.year_from, args.year_to)
    cache_hit = False
    parsed    = None

    if not args.force:
        cached = cache_lookup(db, query_key)
        if cached is not None:
            parsed, cache_doc = cached
            cache_hit = True
            print(f"\n✓ Cache hit — serving {len(parsed)} patents from MongoDB")
            print(f"  Originally fetched : "
                  f"{cache_doc['cached_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print("  Query summary stored:")
            for k, v in cache_doc["query_summary"].items():
                print(f"    {k}: {v}")

    # --- Fetch from Lens.org if no cache hit ---
    if parsed is None:
        query       = build_query(args.keywords or [], args.cpc or [], args.ipc or [])
        sort        = build_sort(has_keywords)
        raw_records = fetch_patents(api_key, query, args.limit, sort,
                                    year_from=args.year_from, year_to=args.year_to)

        if not raw_records:
            print("No patents found.")
            sys.exit(0)

        if args.debug:
            debug_print(raw_records)

        parsed = [parse_patent(r) for r in raw_records]

    # --- CSV ---
    csv_path = save_to_csv(parsed, args.output)

    # --- MongoDB: upsert patents + store cache ---
    inserted, updated = save_patents(db, parsed, run_id)
    if db is not None:
        print(f"MongoDB patents — inserted: {inserted}, updated: {updated}")
        if not cache_hit:
            cache_store(db, query_key, query_summary, parsed, csv_path, run_id)

    # --- Log run ---
    elapsed = time.time() - start_time
    log_run(db, run_id, query_summary, len(parsed),
            inserted, updated, csv_path, elapsed, cache_hit)

    years = [p["publication_year"] for p in parsed if p["publication_year"]]
    if years:
        print(f"Year range: {min(years)} - {max(years)}")
    print(f"Total records: {len(parsed)}")
    print(f"Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
