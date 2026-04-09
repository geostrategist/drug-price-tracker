"""
World Bank GDP per capita PPP scraper.

Indicator: NY.GDP.PCAP.PP.CD – GDP per capita, PPP (current international $)
API docs:  https://datahelpdesk.worldbank.org/knowledgebase/articles/898599

Fetches the most recent value for every country and stores it in the
`gdp_ppp` table so the PPP correction module can look up any country.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/NY.GDP.PCAP.PP.CD"
    "?format=json&per_page=300&mrv=1"
)
CACHE_DIR = Path(__file__).parent.parent / "data" / "worldbank"

HEADERS = {"User-Agent": "DrugPriceTracker/1.0 (research)"}


def _cache_path(page: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"gdp_ppp_p{page}.json"


def _fetch_page(page: int) -> tuple[dict, list]:
    """Fetch one page from the World Bank API, using local cache if present."""
    import json

    cache = _cache_path(page)
    if cache.exists():
        logger.info("  cache hit: page %d", page)
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return raw[0], raw[1] or []

    url = API_URL + f"&page={page}"
    logger.info("  World Bank API: page %d – %s", page, url)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    raw = resp.json()
    # Validate structure: [metadata_dict, data_list]
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(f"Unexpected World Bank response shape: {type(raw)}")

    cache.write_text(json.dumps(raw), encoding="utf-8")
    return raw[0], raw[1] or []


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== World Bank: fetching GDP per capita PPP ===")

    try:
        meta, records = _fetch_page(1)
    except Exception as e:
        logger.error("World Bank page 1 failed: %s", e)
        return

    total_pages = int(meta.get("pages", 1))
    logger.info("  %d page(s), %d total records", total_pages, meta.get("total", "?"))

    all_records = list(records)
    for page in range(2, total_pages + 1):
        try:
            _, records = _fetch_page(page)
            all_records.extend(records)
        except Exception as e:
            logger.warning("  page %d failed: %s", page, e)

    saved = 0
    for rec in all_records:
        value = rec.get("value")
        if value is None:
            continue  # no data for this country/year

        country = rec.get("country", {})
        iso3    = rec.get("countryiso3code") or ""
        name    = country.get("value") or ""
        year    = str(rec.get("date") or "")

        if not iso3:
            continue

        conn.execute(
            """INSERT INTO gdp_ppp (country_code, country_name, year, gdp_ppp_usd)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(country_code, year)
               DO UPDATE SET gdp_ppp_usd=excluded.gdp_ppp_usd,
                             country_name=excluded.country_name,
                             fetch_date=date('now')""",
            (iso3.upper(), name, year, float(value)),
        )
        saved += 1

    logger.info("World Bank done. Saved %d country GDP PPP records.", saved)
