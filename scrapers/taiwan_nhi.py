"""
Taiwan National Health Insurance (全民健保) drug reimbursement price scraper.

Source: 衛生福利部中央健康保險署 – 藥品給付項目及支付標準
Prices are in TWD (New Taiwan Dollar).

The NHIA API returns JSON metadata with a download URL; we extract it and
fetch the actual CSV. Multiple fallback URLs are tried in sequence.
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path

import requests
import pandas as pd

from db import upsert_source, mark_fetched, insert_drug, insert_price

logger = logging.getLogger(__name__)

# Endpoint that returns JSON metadata (contains a CSV download URL)
NHI_META_URL = (
    "https://data.gov.tw/api/v2/rest/dataset/23715"
)
# Direct download attempts (try each in order)
NHI_DIRECT_URLS = [
    # info.nhi.gov.tw – confirmed working (UTF-8 BOM, ~95 MB)
    "https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001",
    # data.nhi.gov.tw direct download (may be unavailable outside Taiwan)
    "https://data.nhi.gov.tw/Datasets/Download.ashx?rid=A21030000I-E41001-001&l=0",
    # data.gov.tw direct
    "https://data.gov.tw/api/v2/rest/dataset/23715",
]
CACHE_DIR = Path(__file__).parent.parent / "data" / "taiwan_nhi"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/csv,application/octet-stream,application/json,*/*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

COL_ALIASES = {
    # Chinese name
    "中文品名": "name_zh", "中文藥品名稱": "name_zh", "藥品中文名稱": "name_zh",
    # English name
    "英文品名": "name_en", "英文藥品名稱": "name_en", "藥品英文名稱": "name_en",
    "藥品名稱": "name_en",
    # Generic / ingredient
    "一般名稱": "generic_name", "成分名": "generic_name", "學名": "generic_name",
    "成份": "generic_name", "成分": "generic_name",           # ← actual NHI column
    # ATC
    "atc碼": "atc_code", "atc_code": "atc_code", "atc": "atc_code",
    "atc代碼": "atc_code",                                    # ← actual NHI column
    # Price (TWD)
    "健保支付價格": "price_twd", "支付價格": "price_twd",
    "支付價": "price_twd",                                    # ← actual NHI column
    "健保價": "price_twd", "藥價": "price_twd", "price": "price_twd",
    # Dosage form
    "劑型": "dosage_form", "藥品劑型": "dosage_form",
    # Strength
    "規格": "strength", "藥品規格": "strength", "規格量": "strength",
    # Manufacturer
    "製造廠": "manufacturer", "藥商名稱": "manufacturer", "廠商名稱": "manufacturer",
    "製造廠名稱": "manufacturer",                             # ← actual NHI column
}


def _cache_path(fname: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / fname


def _is_csv_content(data: bytes) -> bool:
    """Return True if data looks like CSV, not HTML."""
    try:
        preview = data[:1000].decode("utf-8-sig", errors="ignore")
    except Exception:
        preview = data[:1000].decode("latin-1", errors="ignore")
    if "<html" in preview.lower() or "<!doctype" in preview.lower():
        return False
    # Should have commas or tabs and not start with { (JSON)
    if preview.strip().startswith("{") or preview.strip().startswith("["):
        return False
    return True


def _try_extract_download_url(data: bytes) -> str | None:
    """If response is JSON metadata, try to extract the actual CSV download URL."""
    try:
        text = data.decode("utf-8-sig", errors="ignore")
        obj = json.loads(text)
        # data.gov.tw format: {"result": {"distribution": [{"accessURL": "..."}]}}
        for dist in (obj.get("result") or {}).get("distribution") or []:
            url = dist.get("accessURL") or dist.get("downloadURL") or ""
            if url and (".csv" in url.lower() or "download" in url.lower()):
                return url
        # NHIA API format: {"results": [{"download_url": "..."}]}
        for item in (obj.get("result") or {}).get("records") or []:
            url = item.get("download_url") or item.get("url") or ""
            if url:
                return url
    except Exception:
        pass
    return None


def _download(force_fresh: bool = False) -> bytes:
    """Download NHI drug price CSV. Returns raw bytes."""
    cache = _cache_path("nhi_drug_prices.csv")
    if cache.exists() and not force_fresh:
        logger.info("  cache hit: nhi_drug_prices.csv (%d bytes)", cache.stat().st_size)
        return cache.read_bytes()

    last_err: Exception | None = None

    for url in NHI_DIRECT_URLS:
        try:
            logger.info("  trying: %s", url)
            # Stream download to handle large file (~95 MB)
            resp = requests.get(url, headers=HEADERS, timeout=120,
                                allow_redirects=True, stream=True)
            resp.raise_for_status()

            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=512 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total % (10 * 1024 * 1024) < 512 * 1024:
                    logger.info("  downloaded %.0f MB …", total / 1024 / 1024)
            data = b"".join(chunks)
            logger.info("  total %.1f MB, content-type: %s",
                        len(data) / 1024 / 1024, resp.headers.get("content-type", "?"))

            # Case 1: got CSV directly
            if _is_csv_content(data):
                cache.write_bytes(data)
                logger.info("  CSV downloaded OK")
                return data

            # Case 2: got JSON metadata with a download URL
            dl_url = _try_extract_download_url(data)
            if dl_url:
                logger.info("  following metadata URL: %s", dl_url)
                resp2 = requests.get(dl_url, headers=HEADERS, timeout=120,
                                     allow_redirects=True, stream=True)
                resp2.raise_for_status()
                data2 = b"".join(resp2.iter_content(512 * 1024))
                if _is_csv_content(data2):
                    cache.write_bytes(data2)
                    return data2
            else:
                logger.warning("  not CSV, preview: %s",
                               data[:300].decode("utf-8", errors="replace"))

        except Exception as e:
            logger.warning("  failed (%s): %s", url, e)
            last_err = e

    raise RuntimeError(f"All NHI URLs failed. Last: {last_err}")


def _read_csv(data: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "latin-1"):
        for sep in (",", "\t", "|"):
            try:
                df = pd.read_csv(
                    io.BytesIO(data), encoding=enc, sep=sep,
                    low_memory=False, on_bad_lines="skip",
                )
                if len(df.columns) >= 3 and len(df) > 0:
                    logger.info("  parsed: enc=%s sep=%r shape=%s", enc, sep, df.shape)
                    return df
            except Exception:
                continue
    raise ValueError("Cannot parse NHI CSV")


def fetch(conn: sqlite3.Connection) -> None:
    logger.info("=== Taiwan NHI: fetching drug reimbursement prices ===")
    source_id = upsert_source(
        conn,
        name="Taiwan NHI 藥品給付支付標準",
        url=NHI_DIRECT_URLS[0],
        description="全民健保藥品給付項目及支付標準（中央健康保險署）",
    )

    saved = 0
    try:
        data = _download()
    except Exception as e:
        logger.error("NHI download failed: %s", e)
        mark_fetched(conn, source_id)
        return

    try:
        df = _read_csv(data)
    except Exception as e:
        logger.error("NHI parse failed: %s", e)
        mark_fetched(conn, source_id)
        return

    # Normalise column names
    norm = lambda s: str(s).strip().lower().replace(" ", "").replace("　", "")
    df.columns = [norm(c) for c in df.columns]
    logger.info("  Raw columns: %s", list(df.columns)[:20])

    rename_map = {}
    for col in df.columns:
        for alias, field in COL_ALIASES.items():
            if col == norm(alias):
                rename_map[col] = field
                break
    df = df.rename(columns=rename_map)
    logger.info("  Renamed columns: %s", list(df.columns)[:20])

    has_name  = "name_zh" in df.columns or "name_en" in df.columns
    has_price = "price_twd" in df.columns

    # Positional fallback: NHI CSV has stable column order
    # [0]異動 [1]藥品代號 [2]英文名 [3]中文名 [4]成份 [5]規格量 [6]規格單位
    # [7]單複方 [8]支付價 [9]有效起日 [10]有效迄日 [11]藥廠 [12]製造廠名稱
    # [13]劑型 [14]藥品分類 [15]分類分組名稱 [16]ATC代碼
    if not has_name and len(df.columns) >= 9:
        logger.warning("Column name match failed; using positional fallback")
        cols = list(df.columns)
        pos_map = {cols[2]: "name_en", cols[3]: "name_zh",
                   cols[4]: "generic_name", cols[8]: "price_twd"}
        if len(cols) > 12: pos_map[cols[12]] = "manufacturer"
        if len(cols) > 13: pos_map[cols[13]] = "dosage_form"
        if len(cols) > 16: pos_map[cols[16]] = "atc_code"
        df = df.rename(columns=pos_map)
        has_name  = "name_zh" in df.columns or "name_en" in df.columns
        has_price = "price_twd" in df.columns

    if not has_name or not has_price:
        logger.error(
            "NHI: missing required columns. has_name=%s has_price=%s  cols=%s",
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
        )
        saved += 1

    mark_fetched(conn, source_id)
    logger.info("Taiwan NHI done. Saved %d entries.", saved)
