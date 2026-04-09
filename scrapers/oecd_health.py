"""
OECD Health Statistics – Pharmaceutical Expenditure scraper.

Dataset: HEALTH_PHMC (Pharmaceutical Market)
API docs: https://data.oecd.org/api/sdmx-json-documentation/

This scraper fetches per-capita pharmaceutical expenditure (USD) and
expenditure as % of GDP across OECD member countries.

Endpoint (SDMX-JSON):
  https://stats.oecd.org/SDMX-JSON/data/HEALTH_PHMC/{filter}/OECD
  ?startTime=2010&endTime=2023&contentType=json
"""
from __future__ import annotations

import logging
import sqlite3
import json
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

OECD_API = (
    "https://stats.oecd.org/SDMX-JSON/data/HEALTH_PHMC/"
    "TOT.PHARMAEXP+PHARMAEXP_TOT.PC_GDP+PCUSD+NBUSD"
    ".../OECD"
    "?startTime=2010&endTime=2023&contentType=json"
)
CACHE_DIR = Path(__file__).parent.parent / "data" / "oecd"

HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (research; contact: github.com)",
    "Accept": "application/json",
}

# Human-readable labels for OECD measure codes
MEASURE_LABELS = {
    "PC_GDP": "Pharmaceutical expenditure as % of GDP",
    "PCUSD":  "Pharmaceutical expenditure per capita (USD)",
    "NBUSD":  "Total pharmaceutical expenditure (million USD)",
}


def _cache_path(fname: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / fname


def _fetch_oecd_json() -> dict | None:
    cache = _cache_path("health_phmc.json")
    if cache.exists():
        logger.info("  cache hit: %s", cache.name)
        return json.loads(cache.read_text(encoding="utf-8"))

    logger.info("  calling OECD SDMX-JSON API …")
    try:
        resp = requests.get(OECD_API, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("  OECD API request failed: %s", e)
        return None

    try:
        data = resp.json()
    except ValueError as e:
        logger.error("  OECD response is not valid JSON: %s", e)
        return None

    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _parse_sdmx_json(data: dict) -> pd.DataFrame:
    """
    Parse OECD SDMX-JSON structure into a flat DataFrame.
    SDMX-JSON layout:
      data.structure.dimensions.observation -> list of dimension descriptors
      data.dataSets[0].observations         -> {key: [value, ...]} mapping
    """
    try:
        dims = data["structure"]["dimensions"]["observation"]
        observations = data["dataSets"][0]["observations"]
    except (KeyError, IndexError) as e:
        logger.error("Unexpected SDMX-JSON structure: %s", e)
        return pd.DataFrame()

    # Build index maps: position -> {code_index -> value}
    index_maps = []
    dim_names  = []
    for dim in dims:
        name = dim["id"]
        values = {i: v["id"] for i, v in enumerate(dim["values"])}
        index_maps.append(values)
        dim_names.append(name)

    rows = []
    for key_str, obs_values in observations.items():
        indices = [int(x) for x in key_str.split(":")]
        row = {dim_names[i]: index_maps[i].get(idx, idx) for i, idx in enumerate(indices)}
        row["value"] = obs_values[0] if obs_values else None
        rows.append(row)

    return pd.DataFrame(rows)


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== OECD: fetching pharmaceutical expenditure data ===")
    source_id = upsert_source(
        conn,
        name="OECD Health Statistics HEALTH_PHMC",
        url="https://stats.oecd.org/Index.aspx?DataSetCode=HEALTH_PHMC",
        description="OECD pharmaceutical market expenditure by country, 2010-2023",
    )

    raw = _fetch_oecd_json()
    if raw is None:
        logger.error("OECD data unavailable.")
        return

    df = _parse_sdmx_json(raw)
    if df.empty:
        logger.error("OECD: parsed DataFrame is empty.")
        return

    logger.info("  parsed %d observations", len(df))
    logger.debug("  columns: %s", list(df.columns))

    # Column name variations across API versions
    country_col = next((c for c in df.columns if c in ("COU", "COUNTRY", "REF_AREA")), None)
    measure_col = next((c for c in df.columns if c in ("VAR", "MEASURE", "INDICATOR")), None)
    time_col    = next((c for c in df.columns if c in ("Year", "TIME_PERIOD", "TIME", "YEAR")), None)

    if not country_col:
        logger.error("OECD: cannot identify country column in %s", list(df.columns))
        return

    # One synthetic "drug" record represents the aggregate expenditure indicator
    saved = 0
    for _, row in df.iterrows():
        value = pd.to_numeric(row.get("value"), errors="coerce")
        if pd.isna(value):
            continue

        country  = str(row.get(country_col) or "").strip()
        measure  = str(row.get(measure_col) or "").strip() if measure_col else ""
        period   = str(row.get(time_col)    or "").strip() if time_col    else ""

        label = MEASURE_LABELS.get(measure, measure) or "Pharmaceutical expenditure"

        drug_id = insert_drug(
            conn,
            name_en=label,
            generic_name="aggregate_expenditure",
            source_id=source_id,
        )
        insert_price(
            conn,
            drug_id=drug_id,
            source_id=source_id,
            country=country,
            price=float(value),
            currency="USD" if "USD" in measure else "PCT_GDP",
            unit=measure,
            price_usd=float(value) if "USD" in measure else None,
            effective_date=period or None,
        )
        saved += 1

    mark_fetched(conn, source_id)
    logger.info("OECD done. Total entries: %d", saved)
