"""
Lens.org Patent Data Retriever
================================
Part of: Prior Art Report Generation Project
Role: Database Integration & Data Retrieval

Setup:
    1. pip3 install requests python-dotenv
    2. Create a .env file in the same folder:
           LENS_API_KEY=your_key_here
    3. Run:
           python3 lens_patent_retriever.py --keywords "solar energy" --limit 5
           python3 lens_patent_retriever.py --cpc H02S40/22 --limit 10
           python3 lens_patent_retriever.py --ipc H04L29/06 --limit 10
           python3 lens_patent_retriever.py --keywords "solar energy" --limit 5 --debug

Get your free API key at: https://www.lens.org/lens/user/subscriptions
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

LENS_API_URL   = "https://api.lens.org/patent/search"
DEFAULT_OUTPUT = "patents_output.csv"
MAX_PAGE_SIZE  = 100
DEFAULT_LIMIT  = 500
REQUEST_DELAY  = 1.0

OUTPUT_FIELDS = [
    "patent_id",
    "title",
    "abstract",
    "ipc_codes",
    "cpc_codes",
    "citations",
    "publication_year",
]

# ---------------------------------------------------------------------------
# Sort strategy
# ---------------------------------------------------------------------------
def build_sort(has_keywords):
    """
    Choose sort order based on what filters are active.

    - Keywords present  -> sort by relevance score first, then recency as tiebreaker.
    - CPC/IPC only      -> sort by recency only.
    """
    if has_keywords:
        return [
            {"_score": "desc"},
            {"date_published": "desc"}
        ]
    else:
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
                "query": escaped,
                "fields": ["class_cpc.symbol"]
            }
        })

    for code in ipc:
        escaped = code.upper().replace("/", "\\/")
        must_clauses.append({
            "query_string": {
                "query": escaped,
                "fields": ["class_ipc.symbol"]
            }
        })

    if not must_clauses:
        raise ValueError("Provide at least one of: --keywords, --cpc, --ipc")

    return {"bool": {"must": must_clauses}}


def build_request_body(query, offset, size, sort, year_from=None, year_to=None):
    body = {
        "query": query,
        "size": size,
        "from": offset,
        "sort": sort,
        # Lens.org only returns fields listed here; references_cited is NOT
        # included by default so citations would be empty without this.
        "include": [
            "lens_id",
            "date_published",
            "abstract",
            "biblio.invention_title",
            "biblio.classifications_ipcr",
            "biblio.classifications_cpc",
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
                "must": [query],
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
        "Content-Type": "application/json",
    }

    all_records = []
    offset = 0

    print(f"\nFetching up to {total_limit} patents from Lens.org ...\n")

    while len(all_records) < total_limit:
        batch_size = min(MAX_PAGE_SIZE, total_limit - len(all_records))
        body = build_request_body(query, offset, batch_size, sort,
                                  year_from=year_from, year_to=year_to)

        try:
            response = requests.post(
                LENS_API_URL,
                headers=headers,
                json=body,
                timeout=30,
            )
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

        data = response.json()
        hits = data.get("data", [])
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
#
# citations column: semicolon-separated lens_ids of patents that THIS patent
# cites (its reference list).
#
# Field path in the raw API response:
#   reference_cited.patents[i].lens_id
#
# This is available in the primary response — no second API call needed.
# Note: some patents have no lens_id on their references (older/non-Lens
# records); those entries are simply skipped.
# ---------------------------------------------------------------------------
def parse_patent(raw):
    biblio = raw.get("biblio", {})
    lens_id = raw.get("lens_id", "")

    # --- Title ---
    invention_title = biblio.get("invention_title", [])
    if isinstance(invention_title, list) and invention_title:
        en = [t.get("text", "") for t in invention_title if t.get("lang") == "en"]
        title = en[0] if en else invention_title[0].get("text", "")
    elif isinstance(invention_title, dict):
        title = invention_title.get("text", "")
    else:
        title = str(invention_title) if invention_title else ""

    # --- Abstract ---
    abstract_list = raw.get("abstract", [])
    if isinstance(abstract_list, list) and abstract_list:
        en_abs = [a.get("text", "") for a in abstract_list if a.get("lang") == "en"]
        abstract = en_abs[0] if en_abs else abstract_list[0].get("text", "")
    elif isinstance(abstract_list, dict):
        abstract = abstract_list.get("text", "")
    else:
        abstract = ""

    # --- IPC codes ---
    ipcr = biblio.get("classifications_ipcr", {})
    ipc_classifications = ipcr.get("classifications", []) if isinstance(ipcr, dict) else []
    ipc_codes = "; ".join(
        c.get("symbol", "") for c in ipc_classifications if c.get("symbol")
    )

    # --- CPC codes ---
    cpc_block = biblio.get("classifications_cpc", {})
    cpc_classifications = cpc_block.get("classifications", []) if isinstance(cpc_block, dict) else []
    cpc_codes = "; ".join(
        c.get("symbol", "") for c in cpc_classifications if c.get("symbol")
    )

    # --- Citations: lens_ids of patents that cite THIS patent (forward citations) ---
    # Field lives at biblio.cited_by.patents[i].lens_id
    # Note: biblio.references_cited (backward) is often absent from Lens responses;
    # biblio.cited_by is consistently populated and contains actual lens_ids.
    cited_by = biblio.get("cited_by", {})
    cited_ids = []
    if isinstance(cited_by, dict):
        for p in cited_by.get("patents", []):
            cid = p.get("lens_id", "")
            if cid:
                cited_ids.append(cid)
    citations = "; ".join(cited_ids)

    # --- Publication year ---
    date_published = raw.get("date_published", "")
    publication_year = date_published[:4] if date_published else ""

    return {
        "patent_id":        lens_id,
        "title":            title,
        "abstract":         abstract,
        "ipc_codes":        ipc_codes,
        "cpc_codes":        cpc_codes,
        "citations":        citations,
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
    print(f"\nSaved {len(records)} patents to: {path.resolve()}")

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
        description="Retrieve patent data from Lens.org and export to CSV."
    )
    parser.add_argument("--api-key",  default=None,
                        help="Lens.org API token. Reads LENS_API_KEY from .env if omitted.")
    parser.add_argument("--keywords", nargs="+",
                        help='e.g. --keywords "solar energy"')
    parser.add_argument("--cpc",      nargs="+", help='e.g. --cpc H02S40/22')
    parser.add_argument("--ipc",      nargs="+", help='e.g. --ipc H04L29/06')
    parser.add_argument("--limit",    type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output",   default=DEFAULT_OUTPUT)
    parser.add_argument("--year-from", type=int, default=None,
                        help="Filter patents from this year e.g. --year-from 2010")
    parser.add_argument("--year-to",   type=int, default=None,
                        help="Filter patents up to this year e.g. --year-to 2023")
    parser.add_argument("--debug",    action="store_true",
                        help="Print raw first API record to inspect field names")
    return parser.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    api_key = args.api_key or os.getenv("LENS_API_KEY")
    if not api_key:
        print("No API key found. Add LENS_API_KEY=your_key to .env file.")
        sys.exit(1)

    if not any([args.keywords, args.cpc, args.ipc]):
        print("Provide at least one filter: --keywords, --cpc, or --ipc")
        sys.exit(1)

    query = build_query(
        keywords=args.keywords or [],
        cpc=args.cpc or [],
        ipc=args.ipc or [],
    )

    has_keywords = bool(args.keywords)
    sort = build_sort(has_keywords)
    sort_label = "relevance + recency" if has_keywords else "recency only"

    print("Query summary:")
    if args.keywords: print(f"  Keywords : {args.keywords}")
    if args.cpc:      print(f"  CPC codes: {args.cpc}")
    if args.ipc:      print(f"  IPC codes: {args.ipc}")
    print(f"  Sort by  : {sort_label}")
    print(f"  Limit    : {args.limit}")
    print(f"  Year from: {args.year_from or 'any'}")
    print(f"  Year to  : {args.year_to or 'any'}")
    print(f"  Output   : {args.output}")

    raw_records = fetch_patents(api_key, query, args.limit, sort,
                                year_from=args.year_from,
                                year_to=args.year_to)

    if not raw_records:
        print("No patents found.")
        sys.exit(0)

    if args.debug:
        debug_print(raw_records)

    parsed = [parse_patent(r) for r in raw_records]
    save_to_csv(parsed, args.output)

    years = [p["publication_year"] for p in parsed if p["publication_year"]]
    if years:
        print(f"Year range: {min(years)} - {max(years)}")
    print(f"Total records written: {len(parsed)}")


if __name__ == "__main__":
    main()