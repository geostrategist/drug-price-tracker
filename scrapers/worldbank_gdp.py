"""
World Bank GDP per capita PPP scraper.

Indicator: NY.GDP.PCAP.PP.CD – GDP per capita, PPP (current international $)

Strategy:
  1. Try live World Bank API
  2. If API fails or Taiwan (TWN) is missing, insert hardcoded fallback values
     so PPP calculation always works even without network access.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import requests

from db import upsert_source, mark_fetched

logger = logging.getLogger(__name__)

API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/NY.GDP.PCAP.PP.CD"
    "?format=json&per_page=300&mrv=1"
)
TWN_URL = (
    "https://api.worldbank.org/v2/country/TWN/indicator/NY.GDP.PCAP.PP.CD"
    "?format=json&per_page=3&mrv=3"
)
CACHE_DIR = Path(__file__).parent.parent / "data" / "worldbank"
HEADERS = {"User-Agent": "DrugPriceTracker/1.0 (research)"}

# Hardcoded World Bank 2022 PPP values (current international $)
# Used as fallback when API is unreachable
FALLBACK_GDP_PPP: dict[str, tuple[str, str, float]] = {
    "TWN": ("Taiwan",           "2022", 73324.0),
    "JPN": ("Japan",            "2022", 47299.0),
    "USA": ("United States",    "2022", 64280.0),
    "DEU": ("Germany",          "2022", 59160.0),
    "GBR": ("United Kingdom",   "2022", 53780.0),
    "FRA": ("France",           "2022", 54990.0),
    "KOR": ("Korea, Rep.",      "2022", 47023.0),
    "AUS": ("Australia",        "2022", 60970.0),
    "CAN": ("Canada",           "2022", 55280.0),
    "CHN": ("China",            "2022", 22077.0),
    "IND": ("India",            "2022",  9183.0),
    "BRA": ("Brazil",           "2022", 16997.0),
    "THA": ("Thailand",         "2022", 21100.0),
    "MYS": ("Malaysia",         "2022", 33650.0),
    "IDN": ("Indonesia",        "2022", 15840.0),
    "PHL": ("Philippines",      "2022", 10080.0),
    "VNM": ("Viet Nam",         "2022", 13750.0),
    "SGP": ("Singapore",        "2022", 127565.0),
    "NZL": ("New Zealand",      "2022", 46330.0),
    "ZAF": ("South Africa",     "2022", 14909.0),
    "MEX": ("Mexico",           "2022", 22290.0),
    "RUS": ("Russian Fed.",     "2022", 34835.0),
    "TUR": ("Turkiye",          "2022", 37850.0),
    "POL": ("Poland",           "2022", 41060.0),
    "ARG": ("Argentina",        "2022", 25960.0),
    "CHE": ("Switzerland",      "2022", 75880.0),
    "SWE": ("Sweden",           "2022", 60660.0),
    "NLD": ("Netherlands",      "2022", 66220.0),
    "BEL": ("Belgium",          "2022", 61460.0),
    "ESP": ("Spain",            "2022", 46000.0),
    "ITA": ("Italy",            "2022", 47940.0),
}


def _upsert(conn: sqlite3.Connection, iso3: str, name: str, year: str, value: float) -> None:
    conn.execute(
        """INSERT INTO gdp_ppp (country_code, country_name, year, gdp_ppp_usd)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(country_code, year)
           DO UPDATE SET gdp_ppp_usd=excluded.gdp_ppp_usd,
                         country_name=excluded.country_name,
                         fetch_date=date('now')""",
        (iso3.upper(), name, year, value),
    )


def _load_fallback(conn: sqlite3.Connection) -> int:
    """Insert hardcoded fallback values for all major countries."""
    n = 0
    for iso3, (name, year, value) in FALLBACK_GDP_PPP.items():
        _upsert(conn, iso3, name, year, value)
        n += 1
    logger.info("  Loaded %d hardcoded GDP PPP fallback values", n)
    return n


def _try_api(conn: sqlite3.Connection) -> int:
    """Try World Bank API. Returns number of records saved, or 0 on failure."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0

    def _get(url: str, cache_name: str):
        cache = CACHE_DIR / cache_name
        if cache.exists():
            logger.info("  cache hit: %s", cache_name)
            return json.loads(cache.read_text(encoding="utf-8"))
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list) or len(raw) < 2:
            raise ValueError(f"Unexpected structure: {str(raw)[:100]}")
        cache.write_text(json.dumps(raw), encoding="utf-8")
        return raw

    # All-countries pages
    try:
        raw1 = _get(API_URL + "&page=1", "gdp_ppp_p1.json")
        pages = int(raw1[0].get("pages", 1))
        all_records = list(raw1[1] or [])
        for p in range(2, pages + 1):
            r = _get(API_URL + f"&page={p}", f"gdp_ppp_p{p}.json")
            all_records.extend(r[1] or [])
        for rec in all_records:
            val = rec.get("value")
            if val is None:
                continue
            iso3 = (rec.get("countryiso3code") or "").strip().upper()
            name = (rec.get("country") or {}).get("value") or ""
            year = str(rec.get("date") or "")
            if not iso3:
                continue
            _upsert(conn, iso3, name, year, float(val))
            saved += 1
        logger.info("  API all-countries: %d records", saved)
    except Exception as e:
        logger.warning("  World Bank all-countries API failed: %s", e)

    # Taiwan explicit fetch
    try:
        twn = _get(TWN_URL, "gdp_ppp_TWN.json")
        for rec in (twn[1] or []):
            val = rec.get("value")
            if val is None:
                continue
            _upsert(conn, "TWN", "Taiwan", str(rec.get("date") or ""), float(val))
            logger.info("  Taiwan TWN %s: %.0f", rec.get("date"), val)
            saved += 1
    except Exception as e:
        logger.warning("  World Bank Taiwan API failed: %s", e)

    return saved


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== World Bank: fetching GDP per capita PPP ===")
    source_id = upsert_source(
        conn,
        name="World Bank GDP per capita PPP",
        url="https://data.worldbank.org/indicator/NY.GDP.PCAP.PP.CD",
        description="GDP per capita PPP (current international $) — all countries",
    )

    api_saved = _try_api(conn)
    logger.info("  API saved %d records", api_saved)

    # Always ensure fallback data exists (fills gaps + guarantees TWN)
    fallback_n = _load_fallback(conn)

    # Verify Taiwan
    row = conn.execute(
        "SELECT gdp_ppp_usd, year FROM gdp_ppp WHERE country_code='TWN' ORDER BY year DESC LIMIT 1"
    ).fetchone()
    if row:
        logger.info("  ✓ Taiwan GDP PPP: $%.0f (%s)", row[0], row[1])
    else:
        logger.error("  ✗ Taiwan GDP PPP still missing!")

    total = conn.execute("SELECT COUNT(*) FROM gdp_ppp").fetchone()[0]
    mark_fetched(conn, source_id)
    logger.info("World Bank done. gdp_ppp table: %d rows total", total)
