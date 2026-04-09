"""
Japan Ministry of Health, Labour and Welfare (厚生労働省) drug price scraper.

Source: NHI Drug Price Standards (薬価基準)
URL: https://www.mhlw.go.jp/topics/2025/04/tp20250401-01.html
Format: Excel (.xlsx) files linked from the above page.
Update cycle: April each year.

The Excel files contain columns (may vary by year):
  品名, 規格・単位, 薬価（円）, 製造販売業者, 薬効分類番号
"""
from __future__ import annotations

import re
import logging
import sqlite3
from io import BytesIO
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

BASE_URL = "https://www.mhlw.go.jp"
INDEX_URL = "https://www.mhlw.go.jp/topics/2025/04/tp20250401-01.html"
CACHE_DIR = Path(__file__).parent.parent / "data" / "mhlw"

HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (research; contact: github.com)",
    "Accept-Language": "ja,en;q=0.9",
}

JPY_USD_FALLBACK = 150.0   # 1 USD ≈ 150 JPY (update if rate drifts significantly)


def _get_jpy_per_usd() -> float:
    """Fetch live JPY/USD rate; fall back to constant if API is unavailable."""
    try:
        resp = requests.get(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=10,
        )
        rate = float(resp.json()["rates"]["JPY"])
        logger.info("JPY/USD rate: %.2f (live)", rate)
        return rate
    except Exception as e:
        logger.warning("Exchange rate fetch failed (%s); using fallback %.1f", e, JPY_USD_FALLBACK)
        return JPY_USD_FALLBACK


def _download_excel(url: str) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = CACHE_DIR / re.sub(r"[^\w.]", "_", url.split("/")[-1])
    if fname.exists():
        logger.info("  cache hit: %s", fname.name)
        return fname.read_bytes()
    logger.info("  downloading: %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    fname.write_bytes(resp.content)
    return resp.content


def _find_excel_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(xlsx|xls)$", href, re.IGNORECASE):
            if href.startswith("http"):
                links.append(href)
            else:
                links.append(BASE_URL + href)
    return links


def _parse_excel(data: bytes, source_id: int) -> pd.DataFrame:
    """
    Try to parse drug name, dosage form/strength, price, and manufacturer
    from the MHLW Excel format. Column names are in Japanese and vary across
    the different sub-files (内用薬, 注射薬, 外用薬, etc.), so we do a
    best-effort column detection.
    """
    xf = pd.ExcelFile(BytesIO(data), engine="openpyxl")
    rows = []
    for sheet in xf.sheet_names:
        try:
            df = xf.parse(sheet, header=None)
        except Exception as e:
            logger.warning("  skipping sheet %s: %s", sheet, e)
            continue

        # Scan for the header row (contains '品名' or '薬価')
        header_row = None
        for i, row in df.iterrows():
            if any(str(v).strip() in ("品名", "薬価") for v in row.values if pd.notna(v)):
                header_row = i
                break
        if header_row is None:
            continue

        df.columns = [str(c).strip() if pd.notna(c) else f"col_{i}" for i, c in enumerate(df.iloc[header_row])]
        df = df.iloc[header_row + 1:].reset_index(drop=True)
        df = df.dropna(how="all")

        # Map known Japanese column names
        col_map = {
            "品名": "name_ja",
            "一般名": "generic_name",
            "規格・単位": "strength",
            "薬価（円）": "price",
            "薬価": "price",
            "製造販売業者": "manufacturer",
            "薬効分類番号": "atc_code",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        required = {"name_ja", "price"}
        if not required.issubset(set(df.columns)):
            logger.debug("  sheet '%s' missing required columns, skipping", sheet)
            continue

        df["source_id"] = source_id
        for col in ["generic_name", "strength", "manufacturer", "atc_code"]:
            if col not in df.columns:
                df[col] = None

        rows.append(df[["name_ja", "generic_name", "strength", "manufacturer", "atc_code", "price", "source_id"]])

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== Japan MHLW: fetching drug price list ===")
    source_id = upsert_source(
        conn,
        name="Japan MHLW 薬価基準 2025",
        url=INDEX_URL,
        description="日本 NHI 薬価基準（厚生労働省、2025年4月改定）",
    )

    logger.info("Fetching index page: %s", INDEX_URL)
    try:
        resp = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch MHLW index page: %s", e)
        return

    links = _find_excel_links(resp.text)
    if not links:
        logger.warning("No Excel links found on MHLW page; the URL may have changed.")
        logger.warning("Manually check: %s", INDEX_URL)
        return

    logger.info("Found %d Excel file(s)", len(links))
    jpy_per_usd = _get_jpy_per_usd()
    total_rows = 0

    for url in links:
        logger.info("Processing: %s", url.split("/")[-1])
        try:
            data = _download_excel(url)
        except requests.RequestException as e:
            logger.error("  download failed: %s", e)
            continue

        df = _parse_excel(data, source_id)
        if df.empty:
            logger.warning("  no data parsed from %s", url.split("/")[-1])
            continue

        for _, row in df.iterrows():
            price_val = pd.to_numeric(row.get("price"), errors="coerce")
            if pd.isna(price_val):
                continue
            drug_id = insert_drug(
                conn,
                name_ja=str(row.get("name_ja") or "").strip() or None,
                generic_name=str(row.get("generic_name") or "").strip() or None,
                dosage_form=None,
                strength=str(row.get("strength") or "").strip() or None,
                manufacturer=str(row.get("manufacturer") or "").strip() or None,
                atc_code=str(row.get("atc_code") or "").strip() or None,
                source_id=source_id,
            )
            insert_price(
                conn,
                drug_id=drug_id,
                source_id=source_id,
                country="JPN",
                price=float(price_val),
                currency="JPY",
                unit="per unit",
                price_usd=float(price_val) / jpy_per_usd,
                effective_date="2025-04-01",
            )
            total_rows += 1

        logger.info("  saved %d entries", total_rows)

    mark_fetched(conn, source_id)
    logger.info("MHLW done. Total entries: %d", total_rows)
