"""
Taiwan National Health Insurance (全民健保) drug reimbursement price scraper.

Source: 衛生福利部中央健康保險署 – 藥品給付項目及支付標準
Prices are in TWD (New Taiwan Dollar).

Tries multiple known URLs in order until one succeeds.
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

# Try these URLs in order — NHIA occasionally changes the endpoint
NHI_URLS = [
    "https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001",
    "https://data.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001",
    "https://data.nhi.gov.tw/Datasets/Download.ashx?rid=A21030000I-E41001-001&l=0",
]
CACHE_DIR = Path(__file__).parent.parent / "data" / "taiwan_nhi"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (DrugPriceTracker/1.0; research)",
    "Accept": "text/csv,application/octet-stream,*/*",
}

COL_ALIASES = {
    "中文品名": "name_zh", "中文藥品名稱": "name_zh", "藥品中文名稱": "name_zh",
    "英文品名": "name_en", "英文藥品名稱": "name_en", "藥品英文名稱": "name_en",
    "藥品名稱": "name_en",
    "一般名稱": "generic_name", "成分名": "generic_name",
    "atc碼": "atc_code", "atc_code": "atc_code", "atc": "atc_code",
    "健保支付價格": "price_twd", "支付價格": "price_twd",
    "健保價": "price_twd", "藥價": "price_twd",
    "劑型": "dosage_form", "藥品劑型": "dosage_form",
    "規格": "strength", "藥品規格": "strength",
    "製造廠": "manufacturer", "藥商名稱": "manufacturer", "廠商名稱": "manufacturer",
}


def _cache_path(fname: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / fname


def _download_csv() -> tuple[bytes, str]:
    """Try each URL and return (content, url_used). Raises if all fail."""
    cache = _cache_path("nhi_drug_prices.csv")
    if cache.exists():
        logger.info("  cache hit: nhi_drug_prices.csv")
        return cache.read_bytes(), NHI_URLS[0]

    last_err = None
    for url in NHI_URLS:
        try:
            logger.info("  trying: %s", url)
            resp = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            # Make sure we got actual data, not an HTML error page
            if "html" in content_type.lower() and not resp.content.startswith(b"\xef\xbb\xbf"):
                # Check if it looks like CSV anyway (has commas / tabs)
                preview = resp.content[:500].decode("utf-8", errors="ignore")
                if "<html" in preview.lower():
                    logger.warning("  got HTML instead of CSV, skipping: %s", url)
                    continue
            cache.write_bytes(resp.content)
            logger.info("  downloaded %d bytes from %s", len(resp.content), url)
            return resp.content, url
        except Exception as e:
            logger.warning("  failed (%s): %s", url, e)
            last_err = e

    raise RuntimeError(f"All Taiwan NHI URLs failed. Last error: {last_err}")


def _read_csv(data: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(data), encoding=enc, low_memory=False, on_bad_lines="skip")
            if len(df.columns) >= 3:
                logger.info("  parsed OK with %s, shape: %s", enc, df.shape)
                return df
        except Exception:
            continue
    raise ValueError("Cannot parse Taiwan NHI CSV with any known encoding")


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== Taiwan NHI: fetching drug reimbursement prices ===")
    source_id = upsert_source(
        conn,
        name="Taiwan NHI 藥品給付支付標準",
        url=NHI_URLS[0],
        description="全民健保藥品給付項目及支付標準（中央健康保險署）",
    )

    # Always call mark_fetched at end even on partial failure
    saved = 0
    try:
        data, used_url = _download_csv()
    except Exception as e:
        logger.error("Taiwan NHI download failed: %s", e)
        mark_fetched(conn, source_id)
        return

    try:
        df = _read_csv(data)
    except Exception as e:
        logger.error("Taiwan NHI parse failed: %s", e)
        mark_fetched(conn, source_id)
        return

    # Normalise column names
    df.columns = [str(c).strip().lower().replace(" ", "").replace("　", "") for c in df.columns]
    rename_map = {}
    for col in df.columns:
        for alias, field in COL_ALIASES.items():
            if col == alias.lower().replace(" ", "").replace("　", ""):
                rename_map[col] = field
                break
    df = df.rename(columns=rename_map)

    logger.info("  Columns: %s", list(df.columns)[:15])

    has_name = "name_zh" in df.columns or "name_en" in df.columns
    has_price = "price_twd" in df.columns

    if not has_name or not has_price:
        logger.error(
            "NHI CSV missing required columns. Has name=%s, has price=%s. All cols: %s",
            has_name, has_price, list(df.columns),
        )
        mark_fetched(conn, source_id)
        return

    for _, row in df.iterrows():
        name_zh = str(row.get("name_zh") or "").strip() or None
        name_en = str(row.get("name_en") or "").strip() or None
        if not name_zh and not name_en:
            continue

        price_twd = pd.to_numeric(row.get("price_twd"), errors="coerce")
        if pd.isna(price_twd) or price_twd <= 0:
            continue

        drug_id = insert_drug(
            conn,
            name_ja=name_zh,
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
