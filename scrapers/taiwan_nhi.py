"""
Taiwan National Health Insurance (全民健保) drug reimbursement price scraper.

Source: 衛生福利部中央健康保險署 – 藥品給付項目及支付標準
API:    https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001
Format: CSV (UTF-8 or Big5), updated periodically by NHIA.

Typical CSV columns (column names may change between releases):
  許可證字號, 藥品代碼, 中文品名, 英文品名, 劑型, 規格, 健保支付價格,
  製造廠, 藥商名稱, 管制級別, ATC碼

Prices are in TWD (New Taiwan Dollar).
"""
from __future__ import annotations

import io
import logging
import sqlite3
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

NHI_API_URL = (
    "https://info.nhi.gov.tw/api/iode0000s01/Dataset"
    "?rId=A21030000I-E41001-001"
)
CACHE_DIR = Path(__file__).parent.parent / "data" / "taiwan_nhi"
HEADERS = {
    "User-Agent": "DrugPriceTracker/1.0 (research)",
    "Accept": "text/csv,application/octet-stream",
}

# Column aliases: normalised name -> our field
COL_ALIASES = {
    "中文品名":     "name_zh",
    "中文藥品名稱": "name_zh",
    "英文品名":     "name_en",
    "英文藥品名稱": "name_en",
    "藥品名稱":     "name_en",
    "一般名稱":     "generic_name",
    "atc碼":        "atc_code",
    "atc_code":     "atc_code",
    "健保支付價格": "price_twd",
    "支付價格":     "price_twd",
    "健保價":       "price_twd",
    "劑型":         "dosage_form",
    "規格":         "strength",
    "製造廠":       "manufacturer",
    "藥商名稱":     "manufacturer",
    "藥品代碼":     "drug_code",
    "許可證字號":   "license_no",
}


def _cache_path(fname: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / fname


def _download_csv() -> bytes:
    cache = _cache_path("nhi_drug_prices.csv")
    if cache.exists():
        logger.info("  cache hit: %s", cache.name)
        return cache.read_bytes()

    logger.info("  downloading Taiwan NHI drug prices …")
    resp = requests.get(NHI_API_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    data = resp.content
    cache.write_bytes(data)
    return data


def _read_csv(data: bytes) -> pd.DataFrame:
    """Try UTF-8, then Big5 (CP950) encoding."""
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            df = pd.read_csv(io.BytesIO(data), encoding=enc, low_memory=False)
            logger.info("  parsed with encoding: %s, shape: %s", enc, df.shape)
            return df
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    raise ValueError("Cannot decode Taiwan NHI CSV with any known encoding")


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== Taiwan NHI: fetching drug reimbursement prices ===")
    source_id = upsert_source(
        conn,
        name="Taiwan NHI 藥品給付支付標準",
        url=NHI_API_URL,
        description="全民健保藥品給付項目及支付標準（中央健康保險署）",
    )

    try:
        data = _download_csv()
    except requests.RequestException as e:
        logger.error("Download failed: %s", e)
        return

    try:
        df = _read_csv(data)
    except ValueError as e:
        logger.error("Parse failed: %s", e)
        return

    # Normalise column names
    df.columns = [str(c).strip().lower().replace(" ", "") for c in df.columns]
    rename_map = {}
    for col in df.columns:
        for alias, field in COL_ALIASES.items():
            if col == alias.lower().replace(" ", ""):
                rename_map[col] = field
                break
    df = df.rename(columns=rename_map)
    logger.debug("  NHI columns after rename: %s", list(df.columns))

    if "name_zh" not in df.columns and "name_en" not in df.columns:
        logger.error("NHI CSV: cannot identify drug name column. Columns: %s", list(df.columns))
        return

    saved = 0
    for _, row in df.iterrows():
        name_zh = str(row.get("name_zh") or "").strip() or None
        name_en = str(row.get("name_en") or "").strip() or None
        if not name_zh and not name_en:
            continue

        price_twd = pd.to_numeric(row.get("price_twd"), errors="coerce")
        if pd.isna(price_twd):
            continue

        drug_id = insert_drug(
            conn,
            name_ja=name_zh,          # store Chinese name in name_ja field (re-used)
            name_en=name_en,
            generic_name=str(row.get("generic_name") or "").strip() or None,
            atc_code=str(row.get("atc_code") or "").strip() or None,
            dosage_form=str(row.get("dosage_form") or "").strip() or None,
            strength=str(row.get("strength") or "").strip() or None,
            manufacturer=str(row.get("manufacturer") or "").strip() or None,
            source_id=source_id,
        )
        insert_price(
            conn,
            drug_id=drug_id,
            source_id=source_id,
            country="TWN",
            price=float(price_twd),
            currency="TWD",
            unit="per unit",
            effective_date=None,
        )
        saved += 1

    mark_fetched(conn, source_id)
    logger.info("Taiwan NHI done. Saved %d entries.", saved)
