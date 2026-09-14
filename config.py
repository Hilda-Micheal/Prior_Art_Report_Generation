"""
config.py
=========
Central configuration — all constants in one place.
Edit this file to change API endpoints, limits, MongoDB settings, or CSV fields.
"""

from dotenv import load_dotenv

load_dotenv()

# --- Lens.org API ---
LENS_API_URL  = "https://api.lens.org/patent/search"
MAX_PAGE_SIZE = 100
DEFAULT_LIMIT = 1000
REQUEST_DELAY = 1.0   # seconds between paginated requests

# --- CSV ---
DEFAULT_OUTPUT = "patents_output.csv"
OUTPUT_FIELDS  = [
    "patent_id",
    "title",
    "abstract",
    "ipc_codes",
    "cpc_codes",
    "cites",        # backward: patents this patent references
    "cited_by",     # forward:  patents that cite this patent
    "publication_year",
]

# --- MongoDB ---
MONGO_DB    = "prior_art"
PATENTS_COL = "patents"
LOGS_COL    = "run_logs"
CACHE_COL   = "query_cache"
