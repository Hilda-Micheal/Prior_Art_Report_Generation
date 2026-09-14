"""
lens_api.py
===========
All Lens.org API interaction: query building, pagination, raw fetch.
"""

import sys
import time

import requests

from config import LENS_API_URL, MAX_PAGE_SIZE, REQUEST_DELAY


# ---------------------------------------------------------------------------
# Sort strategy
# ---------------------------------------------------------------------------
def build_sort(has_keywords):
    """
    Relevance-first when keywords are present (best semantic match at top).
    Recency-only for classification-code searches (all results score equally).
    """
    if has_keywords:
        return [{"_score": "desc"}, {"date_published": "desc"}]
    return [{"date_published": "desc"}]


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------
def build_query(keywords, cpc, ipc):
    """
    Build a Lens.org bool/must query from keyword, CPC, and IPC filters.
    At least one filter must be provided.
    """
    must_clauses = []

    if keywords:
        must_clauses.append({
            "query_string": {
                "query":            " AND ".join(keywords),
                "fields":           ["title", "abstract"],
                "default_operator": "AND",
            }
        })

    for code in cpc:
        escaped = code.upper().replace("/", "\\/")
        must_clauses.append({
            "query_string": {
                "query":  f"{escaped}*",
                "fields": ["class_cpc.symbol"],
            }
        })

    for code in ipc:
        escaped = code.upper().replace("/", "\\/")
        must_clauses.append({
            "query_string": {
                "query":  f"{escaped}*",
                "fields": ["class_ipc.symbol"],
            }
        })

    if not must_clauses:
        raise ValueError("Provide at least one of: --keywords, --cpc, --ipc")

    return {"bool": {"must": must_clauses}}


def build_request_body(query, offset, size, sort, year_from=None, year_to=None):
    """Assemble the full POST body for one paginated request."""
    body = {
        "query":   query,
        "size":    size,
        "from":    offset,
        "sort":    sort,
        # Explicit projection — only fetch fields we use.
        # references_cited and cited_by require free-tier or paid API access.
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
                "filter": [{"range": {"date_published": date_range}}],
            }
        }

    return body


# ---------------------------------------------------------------------------
# Paginated fetch
# ---------------------------------------------------------------------------
def fetch_patents(api_key, query, total_limit, sort, year_from=None, year_to=None):
    """
    Fetch up to total_limit patents from Lens.org, paging through results.

    Returns:
        list of raw API hit dicts
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    all_records = []
    offset      = 0

    print(f"\nFetching up to {total_limit} patents from Lens.org ...\n")

    while len(all_records) < total_limit:
        batch_size = min(MAX_PAGE_SIZE, total_limit - len(all_records))
        body       = build_request_body(query, offset, batch_size, sort,
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
            print("Rate limited — waiting 10 seconds ...")
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
