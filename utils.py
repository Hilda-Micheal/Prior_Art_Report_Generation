"""
utils.py
========
Shared utilities: CSV export, query fingerprinting, debug output.
"""

import csv
import hashlib
import json
from pathlib import Path

from config import OUTPUT_FIELDS


# ---------------------------------------------------------------------------
# Query fingerprint
# ---------------------------------------------------------------------------
def make_query_key(keywords, cpc, ipc, limit, year_from, year_to):
    """
    SHA-256 hash of the canonical query parameters.
    Same inputs always produce the same key → cache hit.
    """
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
# CSV export
# ---------------------------------------------------------------------------
def save_to_csv(records, output_path):
    """Write parsed patent records to a CSV file. Returns the resolved Path."""
    path = Path(output_path)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    print(f"Saved {len(records)} patents to CSV: {path.resolve()}")
    return path


# ---------------------------------------------------------------------------
# Debug printer
# ---------------------------------------------------------------------------
def debug_print(raw_records):
    """Pretty-print the first raw API record to help inspect field names."""
    import json
    if not raw_records:
        return
    first = raw_records[0]
    print("\n========== RAW FIRST RECORD ==========")
    print(json.dumps(first, indent=2))
    print("\n========== TOP-LEVEL KEYS ==========")
    for k, v in first.items():
        print(f"  '{k}': {type(v).__name__} = {str(v)[:120]}")
    print("=====================================\n")
