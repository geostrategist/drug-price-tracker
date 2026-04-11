"""
U.S. Drug Data scraper — two-layer approach
============================================

Layer 1 (always accessible):
  FDA Orange Book — drug metadata (generic name, brand name, strength,
  dosage form, manufacturer) for all FDA-approved drugs.
  Source: https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files
  Format: ZIP → products.txt (pipe-delimited)

Layer 2 (requires CMS server access — blocked in some regions):
  CMS NADAC — National Average Drug Acquisition Cost (USD/unit, weekly).
  Source: https://data.medicaid.gov  (Socrata API, UID: a4y5-998d)
  Falls back gracefully if the server is unreachable.

Usage:
  python main.py fetch us              # Orange Book only if CMS is blocked
  python main.py fetch us --nadac      # also attempt NADAC price fetch

Country code: USA  |  Currency: USD
"""
from __future__ import annotations

import io
import logging
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

# ── Orange Book ───────────────────────────────────────────────────────────────
OB_ZIP_URL   = "https://www.fda.gov/media/76860/download"
OB_SOURCE    = "https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files"
CACHE_DIR    = Path(__file__).parent.parent / "data" / "us_fda"

# ── CMS NADAC ─────────────────────────────────────────────────────────────────
NADAC_BASE   = "https://data.medicaid.gov/resource/a4y5-998d.csv"
NADAC_SOURCE = "https://data.medicaid.gov/datasets/national-average-drug-acquisition-cost-nadac"
NADAC_CACHE  = Path(__file__).parent.parent / "data" / "us_nadac"
PAGE_SIZE    = 50_000

HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (academic research)",
    "Accept": "text/csv,application/json,*/*",
}


# ─────────────────────────────────────────────────────────────────────────────
# Orange Book helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ob_cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "ob_products.txt"


def _download_orange_book(force_fresh: bool = False) -> pd.DataFrame:
    cache = _ob_cache_path()
    if cache.exists() and not force_fresh:
        logger.info("  OB cache hit: %s (%.1f MB)", cache.name,
                    cache.stat().st_size / 1_048_576)
        return pd.read_csv(cache, sep="~", encoding="latin-1", dtype=str,
                           on_bad_lines="skip")

    logger.info("  Downloading FDA Orange Book ZIP …")
    resp = requests.get(OB_ZIP_URL, timeout=60, stream=True,
                        headers={"User-Agent": HEADERS["User-Agent"]})
    resp.raise_for_status()

    data = resp.content
    z = zipfile.ZipFile(io.BytesIO(data))
    raw = z.read("products.txt")
    cache.write_bytes(raw)
    logger.info("  Orange Book saved: %d bytes", len(raw))
    return pd.read_csv(io.BytesIO(raw), sep="~", encoding="latin-1",
                       dtype=str, on_bad_lines="skip")


def _process_orange_book(df: pd.DataFrame, source_id: int,
                          conn: sqlite3.Connection) -> int:
    """
    Insert FDA Orange Book drug entries.
    Columns: Ingredient, DF;Route, Trade_Name, Applicant,
             Strength, Appl_Type, Appl_No, Product_No, Type,
             Applicant_Full_Name, ...
    No price data — drugs are inserted without a price row.
    """
    saved = 0
    # Normalise column names
    df.columns = [c.strip() for c in df.columns]

    # Split dosage form and route ("TABLET;ORAL" → "TABLET", "ORAL")
    if "DF;Route" in df.columns:
        split = df["DF;Route"].str.split(";", n=1, expand=True)
        df["dosage_form"] = split[0].str.strip()
        df["route"]       = split[1].str.strip() if split.shape[1] > 1 else None
    else:
        df["dosage_form"] = None
        df["route"]       = None

    for row in df.itertuples(index=False):
        generic = str(getattr(row, "Ingredient",  "") or "").strip() or None
        brand   = str(getattr(row, "Trade_Name",  "") or "").strip() or None
        mfr     = str(getattr(row, "Applicant_Full_Name", "") or
                      getattr(row, "Applicant", "") or "").strip() or None
        strength= str(getattr(row, "Strength",    "") or "").strip() or None
        dform   = str(getattr(row, "dosage_form", "") or "").strip() or None
        # drug_type: "RX" / "OTC" / "DISCN"
        dtype   = str(getattr(row, "Type",        "") or "").strip() or None

        if not generic and not brand:
            continue

        insert_drug(
            conn,
            name_en=brand or generic,
            name_ja=None,
            generic_name=generic,
            atc_code=None,          # Orange Book uses NDA, not ATC
            dosage_form=dform,
            strength=strength,
            manufacturer=mfr,
            source_id=source_id,
        )
        # No price row — price data comes from NADAC layer
        saved += 1

    return saved


# ─────────────────────────────────────────────────────────────────────────────
# NADAC helpers  (optional — may be blocked outside the US)
# ─────────────────────────────────────────────────────────────────────────────

def _nadac_latest_date() -> str | None:
    try:
        resp = requests.get(
            NADAC_BASE,
            params={"$select": "effective_date",
                    "$order":  "effective_date DESC",
                    "$limit":  "1"},
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        df = pd.read_csv(pd.io.common.StringIO(resp.text))
        if not df.empty and "effective_date" in df.columns:
            return str(df.iloc[0]["effective_date"]).strip()
    except Exception as e:
        logger.warning("NADAC date probe failed: %s", e)
    return None


def _nadac_fetch_page(effective_date: str, offset: int) -> pd.DataFrame:
    resp = requests.get(
        NADAC_BASE,
        params={"$where":  f"effective_date='{effective_date}'",
                "$limit":  str(PAGE_SIZE),
                "$offset": str(offset),
                "$order":  "ndc_description"},
        headers=HEADERS, timeout=60,
    )
    resp.raise_for_status()
    return pd.read_csv(pd.io.common.StringIO(resp.text), low_memory=False)


def _fetch_nadac(conn: sqlite3.Connection, source_id: int) -> int:
    effective_date = _nadac_latest_date()
    if not effective_date:
        logger.warning("Cannot reach CMS NADAC (data.medicaid.gov may be "
                       "blocked in your region). US price data skipped.")
        return 0

    logger.info("NADAC effective date: %s", effective_date)
    NADAC_CACHE.mkdir(parents=True, exist_ok=True)
    cache = NADAC_CACHE / f"nadac_{effective_date.replace('/','-')}.csv"

    if cache.exists():
        logger.info("  NADAC cache hit: %s", cache.name)
        df = pd.read_csv(cache, low_memory=False)
    else:
        frames: list[pd.DataFrame] = []
        offset = 0
        while True:
            logger.info("  NADAC fetch offset=%d", offset)
            chunk = _nadac_fetch_page(effective_date, offset)
            if chunk.empty:
                break
            frames.append(chunk)
            if len(chunk) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        if not frames:
            return 0
        df = pd.concat(frames, ignore_index=True)
        df.to_csv(cache, index=False)

    # Normalise columns
    df.columns = [c.lower().strip() for c in df.columns]
    name_col  = next((c for c in df.columns if "description" in c or
                      "drug_name" in c or "ndc_desc" in c), None)
    price_col = next((c for c in df.columns if "nadac_per_unit" in c or
                      "price_per_unit" in c), None)
    date_col  = next((c for c in df.columns if "effective_date" in c), None)
    unit_col  = next((c for c in df.columns if "pricing_unit" in c), None)

    if not name_col or not price_col:
        logger.error("NADAC missing expected columns. Got: %s", list(df.columns))
        return 0

    df["price_usd"] = pd.to_numeric(df[price_col], errors="coerce")
    df = df[df["price_usd"].notna() & (df["price_usd"] > 0)]

    import re
    saved = 0
    for row in df.itertuples(index=False):
        name_en = str(getattr(row, name_col, "") or "").strip()
        if not name_en:
            continue
        # Derive generic from description  e.g. "ATORVASTATIN 10 MG TAB" → "ATORVASTATIN"
        generic = re.match(r"^([A-Z][A-Z /\-]+?)(?=\s+\d|\s+\(|\s*$)",
                           name_en)
        generic = generic.group(1).strip() if generic else None

        eff_date = str(getattr(row, date_col, "") or "") if date_col else ""
        unit     = str(getattr(row, unit_col, "") or "per unit") if unit_col else "per unit"

        drug_id = insert_drug(
            conn,
            name_en=name_en, name_ja=None,
            generic_name=generic,
            atc_code=None,
            dosage_form=None, strength=None, manufacturer=None,
            source_id=source_id,
        )
        insert_price(
            conn,
            drug_id=drug_id, source_id=source_id,
            country="USA", price=float(row.price_usd),
            currency="USD", unit=unit,
            price_usd=float(row.price_usd),
            effective_date=eff_date,
        )
        saved += 1

    return saved


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def fetch(conn: sqlite3.Connection, force_fresh: bool = False) -> None:
    logger.info("=== U.S. Drug Data: FDA Orange Book + CMS NADAC ===")

    # ── Source 1: Orange Book (drug catalog) ─────────────────────────────────
    ob_source_id = upsert_source(
        conn,
        name="U.S. FDA Orange Book (Approved Drug Products)",
        url=OB_SOURCE,
        description="FDA-approved drug catalog: generic/brand names, strength, "
                    "dosage form, manufacturer. No price data.",
    )

    logger.info("[1/2] FDA Orange Book …")
    try:
        ob_df = _download_orange_book(force_fresh=force_fresh)
        ob_saved = _process_orange_book(ob_df, ob_source_id, conn)
        conn.commit()
        mark_fetched(conn, ob_source_id)
        logger.info("Orange Book done. Inserted %d drug entries.", ob_saved)
    except Exception as e:
        logger.error("Orange Book failed: %s", e)
        mark_fetched(conn, ob_source_id)

    # ── Source 2: NADAC (retail prices — may be blocked outside US) ───────────
    nadac_source_id = upsert_source(
        conn,
        name="U.S. CMS NADAC (National Average Drug Acquisition Cost)",
        url=NADAC_SOURCE,
        description="U.S. retail pharmacy drug acquisition cost in USD/unit "
                    "(CMS Medicaid, weekly snapshot). "
                    "NOTE: CMS servers (data.medicaid.gov) may be unreachable "
                    "outside the United States.",
    )

    logger.info("[2/2] CMS NADAC prices (may be blocked outside US) …")
    try:
        price_saved = _fetch_nadac(conn, nadac_source_id)
        conn.commit()
        mark_fetched(conn, nadac_source_id)
        if price_saved:
            logger.info("NADAC done. Inserted %d price records.", price_saved)
        else:
            logger.warning("NADAC: 0 price records inserted. "
                           "Run from a US IP or use a VPN to fetch prices.")
    except Exception as e:
        logger.error("NADAC failed: %s", e)
        mark_fetched(conn, nadac_source_id)

    logger.info("=== U.S. fetch complete ===")
