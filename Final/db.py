"""
db.py
=====
All MongoDB operations: connect, cache lookup/store, patent upsert, run logging.
"""

import csv
import io
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne
from pymongo.errors import ConnectionFailure

from config import MONGO_DB, PATENTS_COL, LOGS_COL, CACHE_COL, OUTPUT_FIELDS


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def connect_mongo(uri):
    """
    Connect to MongoDB and ensure indexes exist.
    Returns the database object, or None if connection fails
    (caller continues in CSV-only mode).
    """
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[MONGO_DB]
        db[PATENTS_COL].create_index("patent_id", unique=True)
        db[CACHE_COL].create_index("query_key",  unique=True)
        print(f"Connected to MongoDB — database: '{MONGO_DB}'")
        return db
    except ConnectionFailure as e:
        print(f"MongoDB connection failed: {e}")
        print("Continuing without MongoDB — CSV only.")
        return None


# ---------------------------------------------------------------------------
# Query cache
# ---------------------------------------------------------------------------
def cache_lookup(db, query_key):
    """
    Look up a previous result by query key.

    Returns:
        (list[dict], cache_doc)  if found
        None                     if not found or db is None
    """
    if db is None:
        return None
    doc = db[CACHE_COL].find_one({"query_key": query_key})
    if doc is None:
        return None
    records = list(csv.DictReader(io.StringIO(doc["csv_content"])))
    return records, doc


def cache_store(db, query_key, query_summary, parsed_records, csv_path, run_id):
    """
    Serialise the full result set as a CSV string and upsert into query_cache.
    Also stores the human-readable query_summary so it is visible in MongoDB.
    """
    if db is None:
        return

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_FIELDS)
    writer.writeheader()
    writer.writerows(parsed_records)

    db[CACHE_COL].update_one(
        {"query_key": query_key},
        {"$set": {
            "query_key":     query_key,
            "query_summary": query_summary,
            "csv_content":   buf.getvalue(),
            "csv_path":      str(csv_path),
            "record_count":  len(parsed_records),
            "cached_at":     datetime.now(timezone.utc),
            "last_run_id":   run_id,
        }},
        upsert=True,
    )
    print(f"Query result cached in MongoDB (collection: '{CACHE_COL}')")


# ---------------------------------------------------------------------------
# Patents collection
# ---------------------------------------------------------------------------
def save_patents(db, records, run_id):
    """
    Bulk-upsert parsed patent records into the patents collection.
    Each document is keyed on patent_id; run_id is appended to run_ids list.

    Returns:
        (inserted_count, updated_count)
    """
    if db is None:
        return 0, 0

    ops = [
        UpdateOne(
            {"patent_id": r["patent_id"]},
            {
                "$set":      {**r, "updated_at": datetime.now(timezone.utc)},
                "$addToSet": {"run_ids": run_id},
            },
            upsert=True,
        )
        for r in records
    ]

    if not ops:
        return 0, 0

    result = db[PATENTS_COL].bulk_write(ops, ordered=False)
    return result.upserted_count, result.modified_count


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------
def log_run(db, run_id, query_summary, total_fetched,
            inserted, updated, csv_path, elapsed, cache_hit):
    """Insert a single run-log document recording everything about this execution."""
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
    print(f"Run logged — run_id: {run_id}  |  cache_hit: {cache_hit}")
