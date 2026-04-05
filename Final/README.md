# Patent Retriever

Fetches patents from Lens.org, exports to CSV, and caches results in MongoDB.

## Project structure

```
patent_retriever/
├── main.py       — CLI entrypoint, orchestrates the full pipeline
├── lens_api.py   — Lens.org API: query building and paginated fetch
├── parser.py     — Transforms raw API hits into clean flat dicts
├── db.py         — MongoDB: connect, cache, upsert patents, log runs
├── utils.py      — CSV export, query fingerprinting, debug printer
├── config.py     — All constants (API URL, limits, field names, DB names)
└── .env          — Your secrets (not committed to git)
```

## Setup

```bash
pip3 install requests python-dotenv pymongo
brew services start mongodb-community   # macOS
```

Create a `.env` file:
```
LENS_API_KEY=your_lens_api_key_here
MONGO_URI=mongodb://localhost:27017     # optional, this is the default
```

## Usage

```bash
python3 main.py --keywords "solar energy" --limit 100
python3 main.py --cpc H01L --limit 1000
python3 main.py --ipc H04L29/06 --year-from 2015 --year-to 2022
python3 main.py --cpc H01L --force        # bypass cache, fetch fresh
python3 main.py --cpc H01L --no-mongo     # CSV only, skip MongoDB
python3 main.py --keywords "neural network" --limit 5 --debug
```

## MongoDB layout  (database: `prior_art`)

| Collection    | Contents |
|---------------|----------|
| `query_cache` | One doc per unique query — full CSV + query summary. Cache hit = no API call. |
| `patents`     | One doc per patent, upserted across all runs. |
| `run_logs`    | One doc per script run — timing, cache hit status, query params. |

## Useful MongoDB queries

```js
use prior_art

// All run logs
db.run_logs.find().sort({ timestamp: -1 }).pretty()

// All cached queries
db.query_cache.find({}, { query_summary: 1, record_count: 1, cached_at: 1 }).pretty()

// Count total patents
db.patents.countDocuments()

// Patents with forward citations
db.patents.find({ cited_by: { $ne: "" } })
```
