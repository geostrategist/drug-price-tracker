"""
WHO / HAI Medicine Prices & Availability scraper.

Primary source:
  WHO-HAI Medicine Prices and Availability survey data (publicly available Excel).
  URL: https://www.who.int/medicines/areas/access/OMS_Medicine_prices.xls

Fallback source:
  WHO Essential Medicines List (EML) – extracts drug names from the official
  JSON endpoint used by https://list.essentialmedicines.org/
  URL: https://list.essentialmedicines.org/api/v1/medicines

Note: WHO does not provide a single centralised real-time pricing API.
The HAI dataset is the most complete public survey of medicine retail/
procurement prices across low- and middle-income countries.
"""
import logging
import sqlite3
from io import BytesIO
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

HAI_URL = "https://www.who.int/medicines/areas/access/OMS_Medicine_prices.xls"
# Fallback 1: WHO Global Price Reporting Mechanism open dataset (CSV)
WHO_GPRM_URL = "https://www.who.int/docs/default-source/medicines/global-price-reporting/gprm-2022-public.xlsx"
# Fallback 2: WHO EML JSON API
EML_API = "https://list.essentialmedicines.org/api/v1/medicines"
CACHE_DIR = Path(__file__).parent.parent / "data" / "who"

HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (research; contact: github.com)",
}


def _cache_path(fname: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / fname


def _fetch_hai(source_id: int, conn: sqlite3.Connection) -> int:
    """Download and parse the WHO/HAI medicine prices Excel."""
    cache = _cache_path("OMS_Medicine_prices.xls")
    if cache.exists():
        logger.info("  cache hit: %s", cache.name)
        data = cache.read_bytes()
    else:
        logger.info("  downloading HAI dataset from WHO …")
        try:
            resp = requests.get(HAI_URL, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            data = resp.content
            cache.write_bytes(data)
        except requests.RequestException as e:
            logger.error("  HAI download failed: %s", e)
            return 0

    try:
        df = pd.read_excel(BytesIO(data), engine="xlrd", header=0)
    except Exception as e:
        logger.error("  failed to parse HAI Excel: %s", e)
        return 0

    logger.debug("  HAI columns: %s", list(df.columns))

    # Normalise column names (the file uses varied capitalisation)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    # Best-effort column mapping for the WHO/HAI format
    col_aliases = {
        "medicine":          "name_en",
        "medicine_name":     "name_en",
        "drug_name":         "name_en",
        "inn":               "generic_name",
        "generic_name":      "generic_name",
        "country":           "country",
        "surveyed_country":  "country",
        "median_price_usd":  "price_usd",
        "price_usd":         "price_usd",
        "unit_price_usd":    "price_usd",
        "unit":              "unit",
        "dosage_form":       "dosage_form",
        "strength":          "strength",
        "year":              "year",
        "survey_year":       "year",
    }
    df = df.rename(columns={k: v for k, v in col_aliases.items() if k in df.columns})

    if "name_en" not in df.columns:
        logger.warning("  HAI: could not identify medicine name column; skipping")
        return 0

    saved = 0
    for _, row in df.iterrows():
        name = str(row.get("name_en") or "").strip()
        if not name or name.lower() in ("nan", ""):
            continue

        price_usd = pd.to_numeric(row.get("price_usd"), errors="coerce")
        country   = str(row.get("country") or "").strip() or "UNKNOWN"
        year      = str(row.get("year") or "").strip()

        drug_id = insert_drug(
            conn,
            name_en=name,
            generic_name=str(row.get("generic_name") or "").strip() or None,
            dosage_form=str(row.get("dosage_form") or "").strip() or None,
            strength=str(row.get("strength") or "").strip() or None,
            source_id=source_id,
        )
        insert_price(
            conn,
            drug_id=drug_id,
            source_id=source_id,
            country=country,
            price=float(price_usd) if pd.notna(price_usd) else None,
            currency="USD",
            unit=str(row.get("unit") or "").strip() or None,
            price_usd=float(price_usd) if pd.notna(price_usd) else None,
            effective_date=year or None,
        )
        saved += 1

    return saved


def _fetch_gprm(source_id: int, conn: sqlite3.Connection) -> int:
    """Try WHO Global Price Reporting Mechanism public dataset."""
    cache = _cache_path("gprm_2022_public.xlsx")
    if cache.exists():
        data = cache.read_bytes()
    else:
        logger.info("  downloading WHO GPRM dataset …")
        try:
            resp = requests.get(WHO_GPRM_URL, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            data = resp.content
            cache.write_bytes(data)
        except requests.RequestException as e:
            logger.error("  GPRM download failed: %s", e)
            return 0

    try:
        df = pd.read_excel(BytesIO(data), engine="openpyxl", header=0)
    except Exception as e:
        logger.error("  GPRM parse failed: %s", e)
        return 0

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    col_aliases = {
        "medicine_name": "name_en", "product_name": "name_en",
        "inn": "generic_name", "generic_name": "generic_name",
        "country": "country", "buyer_country": "country",
        "unit_price_usd": "price_usd", "price_usd": "price_usd",
        "pack_price_usd": "price_usd",
        "dosage_form": "dosage_form", "strength": "strength",
        "year": "year",
    }
    df = df.rename(columns={k: v for k, v in col_aliases.items() if k in df.columns})

    if "name_en" not in df.columns:
        return 0

    saved = 0
    for _, row in df.iterrows():
        name = str(row.get("name_en") or "").strip()
        if not name or name.lower() == "nan":
            continue
        price_usd = pd.to_numeric(row.get("price_usd"), errors="coerce")
        country = str(row.get("country") or "").strip() or "UNKNOWN"
        drug_id = insert_drug(
            conn, name_en=name,
            generic_name=str(row.get("generic_name") or "").strip() or None,
            dosage_form=str(row.get("dosage_form") or "").strip() or None,
            strength=str(row.get("strength") or "").strip() or None,
            source_id=source_id,
        )
        insert_price(
            conn, drug_id=drug_id, source_id=source_id,
            country=country,
            price=float(price_usd) if pd.notna(price_usd) else None,
            currency="USD",
            price_usd=float(price_usd) if pd.notna(price_usd) else None,
            effective_date=str(row.get("year") or "").strip() or None,
        )
        saved += 1
    return saved


def _fetch_eml(source_id: int, conn: sqlite3.Connection) -> int:
    """Fallback: pull drug names from the WHO EML JSON API (no pricing)."""
    logger.info("  falling back to WHO EML API (names only, no pricing) …")
    try:
        resp = requests.get(EML_API, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        medicines = resp.json()
    except Exception as e:
        logger.error("  EML API failed: %s", e)
        return 0

    saved = 0
    for med in medicines if isinstance(medicines, list) else medicines.get("data", []):
        name = (med.get("name") or med.get("inn") or "").strip()
        if not name:
            continue
        insert_drug(
            conn,
            name_en=name,
            generic_name=med.get("inn") or None,
            atc_code=med.get("atcCode") or med.get("atc_code") or None,
            source_id=source_id,
        )
        saved += 1

    return saved


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== WHO: fetching medicine prices ===")
    source_id = upsert_source(
        conn,
        name="WHO/HAI Medicine Prices",
        url=HAI_URL,
        description="WHO-HAI Medicine Prices and Availability survey (multi-country)",
    )

    saved = _fetch_hai(source_id, conn)
    if saved == 0:
        logger.warning("HAI dataset unavailable; trying WHO GPRM …")
        saved = _fetch_gprm(source_id, conn)
    if saved == 0:
        logger.warning("GPRM unavailable; trying WHO EML fallback …")
        saved = _fetch_eml(source_id, conn)

    mark_fetched(conn, source_id)
    logger.info("WHO done. Total entries: %d", saved)
