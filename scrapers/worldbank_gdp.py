"""
World Bank GDP per capita PPP scraper.

Indicator: NY.GDP.PCAP.PP.CD – GDP per capita, PPP (current international $)
API docs:  https://datahelpdesk.worldbank.org/knowledgebase/articles/898599
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
# Explicit Taiwan fetch (may be absent from 'all' endpoint on some API mirrors)
TWN_URL = (
    "https://api.worldbank.org/v2/country/TWN/indicator/NY.GDP.PCAP.PP.CD"
    "?format=json&per_page=5&mrv=3"
)
CACHE_DIR = Path(__file__).parent.parent / "data" / "worldbank"
HEADERS = {"User-Agent": "DrugPriceTracker/1.0 (research)"}


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _get_json(url: str, cache_name: str) -> list | None:
    cache = _cache_path(cache_name)
    if cache.exists():
        logger.info("  cache hit: %s", cache_name)
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list) or len(raw) < 2:
            logger.error("Unexpected World Bank response: %s", str(raw)[:200])
            return None
        cache.write_text(json.dumps(raw), encoding="utf-8")
        return raw
    except Exception as e:
        logger.error("World Bank request failed (%s): %s", url, e)
        return None


def _upsert_record(conn: sqlite3.Connection, iso3: str, name: str, year: str, value: float) -> None:
    conn.execute(
        """INSERT INTO gdp_ppp (country_code, country_name, year, gdp_ppp_usd)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(country_code, year)
           DO UPDATE SET gdp_ppp_usd=excluded.gdp_ppp_usd,
                         country_name=excluded.country_name,
                         fetch_date=date('now')""",
        (iso3.upper(), name, year, value),
    )


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== World Bank: fetching GDP per capita PPP ===")
    source_id = upsert_source(
        conn,
        name="World Bank GDP per capita PPP",
        url="https://data.worldbank.org/indicator/NY.GDP.PCAP.PP.CD",
        description="GDP per capita PPP (current international $) — all countries",
    )

    saved = 0

    # ── Step 1: fetch all countries (paginated) ───────────────────────────────
    raw = _get_json(API_URL + "&page=1", "gdp_ppp_p1.json")
    if raw is not None:
        meta = raw[0]
        total_pages = int(meta.get("pages", 1))
        all_records = list(raw[1] or [])

        for page in range(2, total_pages + 1):
            r = _get_json(API_URL + f"&page={page}", f"gdp_ppp_p{page}.json")
            if r is not None:
                all_records.extend(r[1] or [])

        for rec in all_records:
            value = rec.get("value")
            if value is None:
                continue
            iso3 = (rec.get("countryiso3code") or "").strip()
            name = (rec.get("country") or {}).get("value") or ""
            year = str(rec.get("date") or "")
            if not iso3:
                continue
            _upsert_record(conn, iso3, name, year, float(value))
            saved += 1

        logger.info("  All-countries fetch: %d records saved", saved)

    # ── Step 2: always fetch Taiwan explicitly (TWN may be missing from 'all') ─
    twn = _get_json(TWN_URL, "gdp_ppp_TWN.json")
    if twn is not None:
        for rec in (twn[1] or []):
            value = rec.get("value")
            if value is None:
                continue
            year = str(rec.get("date") or "")
            _upsert_record(conn, "TWN", "Taiwan, China", year, float(value))
            logger.info("  Taiwan TWN %s: %.0f (USD PPP)", year, value)
            saved += 1

    # ── Step 3: verify Taiwan is present ──────────────────────────────────────
    row = conn.execute(
        "SELECT gdp_ppp_usd, year FROM gdp_ppp WHERE country_code='TWN' ORDER BY year DESC LIMIT 1"
    ).fetchone()
    if row:
        logger.info("  Taiwan GDP PPP confirmed: %.0f USD (%s)", row[0], row[1])
    else:
        logger.warning("  Taiwan GDP PPP still missing — PPP comparison will not work!")

    mark_fetched(conn, source_id)
    logger.info("World Bank done. Total records: %d", saved)
