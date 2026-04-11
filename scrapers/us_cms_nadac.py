"""
U.S. CMS NADAC – National Average Drug Acquisition Cost scraper.

Source:  Centers for Medicare & Medicaid Services (CMS)
Dataset: National Average Drug Acquisition Cost (NADAC)
URL:     https://data.medicaid.gov/datasets/national-average-drug-acquisition-cost-nadac

Prices represent retail-pharmacy drug acquisition costs in USD per unit
(tablet, capsule, mL, etc.), published weekly by CMS.  This is the closest
U.S. public equivalent to Taiwan NHI reimbursement prices and Japan 薬価.

API:  Socrata Open Data API (SODA) — returns JSON or CSV.
      We fetch only the most recent effective-date snapshot to keep the
      dataset manageable (~10-20k unique drugs per week).

Country code stored: USA
Currency stored:     USD
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

# ── Socrata SODA endpoint ────────────────────────────────────────────────────
# Dataset UID: a4y5-998d  (NADAC weekly)
# We request CSV directly to avoid JSON memory overhead.
NADAC_BASE = "https://data.medicaid.gov/resource/a4y5-998d.csv"

# Limit rows per page; SODA max is 50 000.
PAGE_SIZE = 50_000

HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (academic research; github.com)",
    "Accept": "text/csv,application/json,*/*",
}

CACHE_DIR = Path(__file__).parent.parent / "data" / "us_nadac"
SOURCE_URL = "https://data.medicaid.gov/datasets/national-average-drug-acquisition-cost-nadac"

# NADAC CSV column → internal field
COL_MAP = {
    "ndc_description":  "name_en",        # e.g. "ATORVASTATIN CALCIUM 10 MG TAB"
    "nadac_per_unit":   "price_usd",       # USD per pricing unit
    "effective_date":   "effective_date",
    "dosage_form":      "dosage_form",     # TAB, CAP, SOL, …
    "route":            "route",
    "pricing_unit":     "unit",            # e.g. "ML", "EA"
    "as_of_date":       "as_of_date",      # date record was published
    # Some API versions use these alternate names:
    "drug_name":        "name_en",
    "price_per_unit":   "price_usd",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _latest_effective_date() -> str | None:
    """Query the API for the single most recent effective_date value."""
    try:
        resp = requests.get(
            NADAC_BASE,
            params={"$select": "effective_date",
                    "$order":  "effective_date DESC",
                    "$limit":  "1"},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        df = pd.read_csv(pd.io.common.StringIO(resp.text))
        if not df.empty and "effective_date" in df.columns:
            return str(df.iloc[0]["effective_date"]).strip()
    except Exception as e:
        logger.warning("Could not determine latest NADAC date: %s", e)
    return None


def _fetch_page(effective_date: str, offset: int) -> pd.DataFrame:
    """Fetch one page of NADAC records for the given effective_date."""
    params = {
        "$where":  f"effective_date='{effective_date}'",
        "$limit":  str(PAGE_SIZE),
        "$offset": str(offset),
        "$order":  "ndc_description",
    }
    resp = requests.get(NADAC_BASE, params=params, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return pd.read_csv(pd.io.common.StringIO(resp.text), low_memory=False)


def _cache_path(effective_date: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_date = effective_date.replace("/", "-").replace(" ", "_")
    return CACHE_DIR / f"nadac_{safe_date}.csv"


def _download(effective_date: str, force_fresh: bool = False) -> pd.DataFrame:
    """Download all pages for effective_date; use local cache if available."""
    cache = _cache_path(effective_date)
    if cache.exists() and not force_fresh:
        logger.info("  cache hit: %s (%.1f MB)", cache.name,
                    cache.stat().st_size / 1_048_576)
        return pd.read_csv(cache, low_memory=False)

    frames: list[pd.DataFrame] = []
    offset = 0
    while True:
        logger.info("  fetching offset=%d …", offset)
        df = _fetch_page(effective_date, offset)
        if df.empty:
            break
        frames.append(df)
        if len(df) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(cache, index=False)
    logger.info("  cached %d rows → %s", len(combined), cache.name)
    return combined


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to internal schema; derive generic_name from name_en."""
    # Lowercase column names for robust matching
    df.columns = [c.lower().strip() for c in df.columns]
    rename = {k: v for k, v in COL_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Deduplicate renamed targets (some API versions have both aliases)
    df = df.loc[:, ~df.columns.duplicated()]

    # Derive a simple generic_name: first word(s) before numeric strength
    # e.g. "ATORVASTATIN CALCIUM 10 MG TAB" → "ATORVASTATIN CALCIUM"
    if "name_en" in df.columns:
        import re
        df["generic_name"] = df["name_en"].str.extract(
            r"^([A-Z][A-Z /\-]+?)(?=\s+\d|\s+\(|\s*$)", expand=False
        ).str.strip()

    # Normalise price
    if "price_usd" in df.columns:
        df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")

    # Normalise effective_date to ISO format
    if "effective_date" in df.columns:
        df["effective_date"] = pd.to_datetime(
            df["effective_date"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")

    return df


# ── main entry point ─────────────────────────────────────────────────────────

def fetch(conn: sqlite3.Connection, force_fresh: bool = False) -> None:
    logger.info("=== U.S. CMS NADAC: fetching drug acquisition prices ===")

    source_id = upsert_source(
        conn,
        name="U.S. CMS NADAC (National Average Drug Acquisition Cost)",
        url=SOURCE_URL,
        description="U.S. retail pharmacy drug acquisition costs in USD per unit "
                    "(CMS Medicaid, weekly; most recent effective date only).",
    )

    # 1. Determine most-recent effective date
    effective_date = _latest_effective_date()
    if effective_date:
        logger.info("Most recent NADAC effective date: %s", effective_date)
    else:
        # Fallback: use today's date string in the query; SODA will return nothing
        # wrong, so we at least log it.
        logger.warning("Could not determine effective date; will attempt generic fetch.")
        effective_date = datetime.today().strftime("%Y-%m-%d")

    # 2. Download (paginated, cached)
    try:
        df = _download(effective_date, force_fresh=force_fresh)
    except Exception as e:
        logger.error("NADAC download failed: %s", e)
        mark_fetched(conn, source_id)
        return

    if df.empty:
        logger.warning("NADAC returned no data for date %s.", effective_date)
        mark_fetched(conn, source_id)
        return

    # 3. Normalise
    df = _normalise(df)

    required = {"name_en", "price_usd"}
    if not required.issubset(df.columns):
        logger.error("NADAC missing required columns after normalisation. "
                     "Got: %s", list(df.columns))
        mark_fetched(conn, source_id)
        return

    # 4. Drop rows with no price
    df = df[df["price_usd"].notna() & (df["price_usd"] > 0)]
    logger.info("Inserting %d NADAC rows …", len(df))

    # 5. Insert into DB
    saved = 0
    BATCH = 500
    for start in range(0, len(df), BATCH):
        chunk = df.iloc[start:start + BATCH]
        for row in chunk.itertuples(index=False):
            name_en = str(getattr(row, "name_en", "") or "").strip() or None
            if not name_en:
                continue

            drug_id = insert_drug(
                conn,
                name_en=name_en,
                name_ja=None,
                generic_name=str(getattr(row, "generic_name", "") or "").strip() or None,
                atc_code=None,          # NADAC does not provide ATC codes
                dosage_form=str(getattr(row, "dosage_form", "") or "").strip() or None,
                strength=None,          # strength is embedded in name_en
                manufacturer=None,      # NADAC does not include manufacturer
                source_id=source_id,
            )
            insert_price(
                conn,
                drug_id=drug_id,
                source_id=source_id,
                country="USA",
                price=float(row.price_usd),
                currency="USD",
                unit=str(getattr(row, "unit", "") or "per unit").strip(),
                price_usd=float(row.price_usd),
                effective_date=str(getattr(row, "effective_date", "") or ""),
            )
            saved += 1

        if start % (BATCH * 20) == 0 and start > 0:
            conn.commit()
            logger.info("  %d / %d inserted …", saved, len(df))

    conn.commit()
    mark_fetched(conn, source_id)
    logger.info("U.S. CMS NADAC done. Saved %d entries (date: %s).",
                saved, effective_date)
