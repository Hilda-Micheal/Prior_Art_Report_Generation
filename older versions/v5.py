"""
Lens.org Patent Data Retriever
================================
Part of: Prior Art Report Generation Project
Role: Database Integration & Data Retrieval

Setup:
    1. pip3 install requests python-dotenv pymongo
    2. Create a .env file in the same folder:
           LENS_API_KEY=your_key_here
           MONGO_URI=mongodb://localhost:27017   (optional, this is the default)
    3. Make sure MongoDB is running locally:
           brew services start mongodb-community   (macOS)
    4. Run:
           python3 lens_patent_retriever.py --keywords "solar energy" --limit 5
           python3 lens_patent_retriever.py --cpc H01L --limit 10
           python3 lens_patent_retriever.py --ipc H04L29/06 --limit 10
           python3 lens_patent_retriever.py --keywords "solar energy" --limit 5 --debug

MongoDB layout:
    Database  : prior_art
    Collections:
        query_cache — one document per unique query (stores full CSV + metadata).
                      On re-run with same query, results are served from here.
        patents     — one document per patent (upserted on lens_id).
        run_logs    — one document per script run with metadata + stats.

Get your free API key at: https://www.lens.org/lens/user/subscriptions
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import ConnectionFailure

load_dotenv()

LENS_API_URL   = "https://api.lens.org/patent/search"
DEFAULT_OUTPUT = "patents_output.csv"
MAX_PAGE_SIZE  = 100
DEFAULT_LIMIT  = 1000
REQUEST_DELAY  = 1.0

MONGO_DB       = "prior_art"
PATENTS_COL    = "patents"
LOGS_COL       = "run_logs"
CACHE_COL      = "query_cache"

OUTPUT_FIELDS = [
    "patent_id",
    "title",
    "abstract",
    "ipc_codes",
    "cpc_codes",
    "cites",
    "cited_by",
    "publication_year",
]


# ---------------------------------------------------------------------------
# Query fingerprint — unique key for a given set of query parameters.
# Same keywords/cpc/ipc/limit/year_from/year_to → same hash → cache hit.
# ---------------------------------------------------------------------------
def make_query_key(keywords, cpc, ipc, limit, year_from, year_to):
    payload = json.dumps({
        "keywords":  sorted(keywords or []),
        "cpc":       sorted(cpc or []),
        "ipc":       sorted(ipc or []),
        "limit":     limit,
        "year_from": year_from,
        "year_to":   year_to,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------
def connect_mongo(uri):
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[MONGO_DB]
        db[PATENTS_COL].create_index("patent_id", unique=True)
        db[CACHE_COL].create_index("query_key", unique=True)
        print(f"Connected to MongoDB — database: '{MONGO_DB}'")
        return db
    except ConnectionFailure as e:
        print(f"MongoDB connection failed: {e}")
        print("Continuing without MongoDB — CSV only.")
        return None


# ---------------------------------------------------------------------------
# Cache lookup — returns (parsed_records, summary_dict) if found, else None
# ---------------------------------------------------------------------------
def cache_lookup(db, query_key):
    if db is None:
        return None
    doc = db[CACHE_COL].find_one({"query_key": query_key})
    if doc is None:
        return None
    # Deserialise the stored CSV back into list-of-dicts
    reader = csv.DictReader(io.StringIO(doc["csv_content"]))
    records = list(reader)
    return records, doc


# ---------------------------------------------------------------------------
# Cache store — saves full CSV text + query summary into query_cache
# ---------------------------------------------------------------------------
def cache_store(db, query_key, query_summary, parsed_records, csv_path, run_id):
    if db is None:
        return

    # Serialise records → CSV string
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_FIELDS)
    writer.writeheader()
    writer.writerows(parsed_records)
    csv_content = buf.getvalue()

    db[CACHE_COL].update_one(
        {"query_key": query_key},
        {"$set": {
            "query_key":     query_key,
            "query_summary": query_summary,   # human-readable dict
            "csv_content":   csv_content,     # full CSV as a string
            "csv_path":      str(csv_path),
            "record_count":  len(parsed_records),
            "cached_at":     datetime.now(timezone.utc),
            "last_run_id":   run_id,
        }},
        upsert=True,
    )
    print(f"Query result cached in MongoDB (collection: '{CACHE_COL}')")


# ---------------------------------------------------------------------------
# Save patents to MongoDB (upsert so re-runs don't duplicate)
# ---------------------------------------------------------------------------
def save_to_mongo(db, records, run_id):
    if db is None:
        return 0, 0

    col = db[PATENTS_COL]
    ops = []
    for r in records:
        doc = dict(r)
        doc["updated_at"] = datetime.now(timezone.utc)
        ops.append(UpdateOne(
            {"patent_id": r["patent_id"]},
            {
                "$set":      {k: v for k, v in doc.items()},
                "$addToSet": {"run_ids": run_id},
            },
            upsert=True,
        ))

    if not ops:
        return 0, 0

    result = col.bulk_write(ops, ordered=False)
    return result.upserted_count, result.modified_count


# ---------------------------------------------------------------------------
# Log the run
# ---------------------------------------------------------------------------
def log_run(db, run_id, query_summary, total_fetched, inserted, updated,
            csv_path, elapsed, cache_hit):
    if db is None:
        return
    db[LOGS_COL].insert_one({
        "run_id":        run_id,
        "timestamp":     datetime.now(timezone.utc),
        "query_summary": query_summary,
        "total_fetched": total_fetched,
        "inserted":      inserted,
        "updated":       updated,
        "csv_path":      str(csv_path),
        "elapsed_sec":   round(elapsed, 2),
        "cache_hit":     cache_hit,
    })
    print(f"Run logged  — run_id: {run_id}  |  cache_hit: {cache_hit}")


# ---------------------------------------------------------------------------
# Sort strategy
# ---------------------------------------------------------------------------
def build_sort(has_keywords):
    if has_keywords:
        return [{"_score": "desc"}, {"date_published": "desc"}]
    return [{"date_published": "desc"}]


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------
def build_query(keywords, cpc, ipc):
    must_clauses = []

    if keywords:
        kw_string = " AND ".join(keywords)
        must_clauses.append({
            "query_string": {
                "query": kw_string,
                "fields": ["title", "abstract"],
                "default_operator": "AND"
            }
        })

    for code in cpc:
        escaped = code.upper().replace("/", "\\/")
        must_clauses.append({
            "query_string": {
                "query": f"{escaped}*",
                "fields": ["class_cpc.symbol"]
            }
        })

    for code in ipc:
        escaped = code.upper().replace("/", "\\/")
        must_clauses.append({
            "query_string": {
                "query": f"{escaped}*",
                "fields": ["class_ipc.symbol"]
            }
        })

    if not must_clauses:
        raise ValueError("Provide at least one of: --keywords, --cpc, --ipc")

    return {"bool": {"must": must_clauses}}


def build_request_body(query, offset, size, sort, year_from=None, year_to=None):
    body = {
        "query":   query,
        "size":    size,
        "from":    offset,
        "sort":    sort,
        "include": [
            "lens_id",
            "date_published",
            "abstract",
            "biblio.invention_title",
            "biblio.classifications_ipcr",
            "biblio.classifications_cpc",
            "biblio.references_cited",
            "biblio.cited_by",
        ],
    }

    if year_from or year_to:
        date_range = {}
        if year_from:
            date_range["gte"] = f"{year_from}-01-01"
        if year_to:
            date_range["lte"] = f"{year_to}-12-31"
        body["query"] = {
            "bool": {
                "must":   [query],
                "filter": [{"range": {"date_published": date_range}}]
            }
        }

    return body


# ---------------------------------------------------------------------------
# API caller
# ---------------------------------------------------------------------------
def fetch_patents(api_key, query, total_limit, sort, year_from=None, year_to=None):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    all_records = []
    offset = 0

    print(f"\nFetching up to {total_limit} patents from Lens.org ...\n")

    while len(all_records) < total_limit:
        batch_size = min(MAX_PAGE_SIZE, total_limit - len(all_records))
        body = build_request_body(query, offset, batch_size, sort,
                                  year_from=year_from, year_to=year_to)

        try:
            response = requests.post(LENS_API_URL, headers=headers,
                                     json=body, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}")
            break

        if response.status_code == 401:
            print("Authentication failed. Check your API key.")
            sys.exit(1)
        elif response.status_code == 429:
            print("Rate limited. Waiting 10 seconds ...")
            time.sleep(10)
            continue
        elif response.status_code != 200:
            print(f"API error {response.status_code}: {response.text[:500]}")
            break

        data            = response.json()
        hits            = data.get("data", [])
        total_available = data.get("total", 0)

        if not hits:
            print("No more results.")
            break

        all_records.extend(hits)
        offset += len(hits)

        print(f"  Fetched {len(all_records)} / {min(total_limit, total_available)} "
              f"(total available on Lens: {total_available})")

        if offset >= total_available:
            break

        time.sleep(REQUEST_DELAY)

    return all_records


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_patent(raw):
    biblio  = raw.get("biblio", {})
    lens_id = raw.get("lens_id", "")

    # Title
    invention_title = biblio.get("invention_title", [])
    if isinstance(invention_title, list) and invention_title:
        en    = [t.get("text", "") for t in invention_title if t.get("lang") == "en"]
        title = en[0] if en else invention_title[0].get("text", "")
    elif isinstance(invention_title, dict):
        title = invention_title.get("text", "")
    else:
        title = str(invention_title) if invention_title else ""

    # Abstract
    abstract_list = raw.get("abstract", [])
    if isinstance(abstract_list, list) and abstract_list:
        en_abs   = [a.get("text", "") for a in abstract_list if a.get("lang") == "en"]
        abstract = en_abs[0] if en_abs else abstract_list[0].get("text", "")
    elif isinstance(abstract_list, dict):
        abstract = abstract_list.get("text", "")
    else:
        abstract = ""

    # IPC
    ipcr                = biblio.get("classifications_ipcr", {})
    ipc_classifications = ipcr.get("classifications", []) if isinstance(ipcr, dict) else []
    ipc_codes           = "; ".join(c.get("symbol", "") for c in ipc_classifications if c.get("symbol"))

    # CPC
    cpc_block           = biblio.get("classifications_cpc", {})
    cpc_classifications = cpc_block.get("classifications", []) if isinstance(cpc_block, dict) else []
    cpc_codes           = "; ".join(c.get("symbol", "") for c in cpc_classifications if c.get("symbol"))

    # Backward citations (what this patent cites)
    refs_cited = biblio.get("references_cited", {})
    cites_ids  = []
    if isinstance(refs_cited, dict):
        for c in refs_cited.get("citations", []):
            cid = c.get("patcit", {}).get("lens_id", "")
            if cid:
                cites_ids.append(cid)
    cites = "; ".join(cites_ids)

    # Forward citations (who cites this patent)
    cited_by_block = biblio.get("cited_by", {})
    cited_by_ids   = []
    if isinstance(cited_by_block, dict):
        for p in cited_by_block.get("patents", []):
            cid = p.get("lens_id", "")
            if cid:
                cited_by_ids.append(cid)
    cited_by = "; ".join(cited_by_ids)

    date_published   = raw.get("date_published", "")
    publication_year = date_published[:4] if date_published else ""

    return {
        "patent_id":        lens_id,
        "title":            title,
        "abstract":         abstract,
        "ipc_codes":        ipc_codes,
        "cpc_codes":        cpc_codes,
        "cites":            cites,
        "cited_by":         cited_by,
        "publication_year": publication_year,
    }


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def save_to_csv(records, output_path):
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    print(f"Saved {len(records)} patents to CSV: {path.resolve()}")
    return path


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_print(raw_records):
    if not raw_records:
        return
    first = raw_records[0]
    print("\n========== RAW FIRST RECORD ==========")
    print(json.dumps(first, indent=2))
    print("\n========== TOP-LEVEL KEYS ==========")
    for k, v in first.items():
        print(f"  '{k}': {type(v).__name__} = {str(v)[:120]}")
    print("=====================================\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Retrieve patents from Lens.org with MongoDB caching."
    )
    parser.add_argument("--api-key",    default=None)
    parser.add_argument("--mongo-uri",  default=None)
    parser.add_argument("--keywords",   nargs="+")
    parser.add_argument("--cpc",        nargs="+")
    parser.add_argument("--ipc",        nargs="+")
    parser.add_argument("--limit",      type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output",     default=DEFAULT_OUTPUT)
    parser.add_argument("--year-from",  type=int, default=None)
    parser.add_argument("--year-to",    type=int, default=None)
    parser.add_argument("--no-mongo",   action="store_true",
                        help="Skip MongoDB — CSV output only.")
    parser.add_argument("--force",      action="store_true",
                        help="Ignore cache and always fetch fresh from Lens.org.")
    parser.add_argument("--debug",      action="store_true")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args       = parse_args()
    start_time = time.time()
    run_id     = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")

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

    # --- Build query summary (human-readable, stored with every cache entry) ---
    has_keywords = bool(args.keywords)
    sort_label   = "relevance + recency" if has_keywords else "recency only"
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
    query_key  = make_query_key(args.keywords, args.cpc, args.ipc,
                                args.limit, args.year_from, args.year_to)
    cache_hit  = False
    parsed     = None

    if not args.force:
        cached = cache_lookup(db, query_key)
        if cached is not None:
            parsed, cache_doc = cached
            cache_hit = True
            print(f"\n✓ Cache hit — serving {len(parsed)} patents from MongoDB")
            print(f"  Originally fetched : {cache_doc['cached_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"  Query summary stored:")
            for k, v in cache_doc["query_summary"].items():
                print(f"    {k}: {v}")

    # --- Fetch from Lens if no cache hit ---
    if parsed is None:
        query = build_query(
            keywords=args.keywords or [],
            cpc=args.cpc or [],
            ipc=args.ipc or [],
        )
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

    # --- MongoDB: upsert patents + store/update cache ---
    inserted, updated = save_to_mongo(db, parsed, run_id)
    if db is not None:
        print(f"MongoDB patents — inserted: {inserted}, updated: {updated}")
        if not cache_hit:
            cache_store(db, query_key, query_summary, parsed, csv_path, run_id)

    # --- Log ---
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