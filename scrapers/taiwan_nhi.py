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


def _detect_encoding_sep(data: bytes) -> tuple:
    """Return (encoding, separator) that successfully parses the first 3 rows."""
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5", "latin-1"):
        for sep in (",", "\t", "|"):
            try:
                df = pd.read_csv(
                    io.BytesIO(data[:65536]), encoding=enc, sep=sep,
                    low_memory=False, on_bad_lines="skip", nrows=3,
                )
                if len(df.columns) >= 3 and len(df) > 0:
                    logger.info("  detected: enc=%s sep=%r cols=%d", enc, sep, len(df.columns))
                    return enc, sep
            except Exception:
                continue
    raise ValueError("Cannot detect NHI CSV encoding/separator")


def _norm_col(s: str) -> str:
    return str(s).strip().lower().replace(" ", "").replace("\u3000", "")


def _apply_rename(cols: list) -> dict:
    """Build rename map from raw column list."""
    rename = {}
    for col in cols:
        for alias, field in COL_ALIASES.items():
            if col == _norm_col(alias):
                rename[col] = field
                break
    # Positional fallback if alias match fails
    # Known stable order: [0]異動 [1]藥品代號 [2]英文名 [3]中文名 [4]成份
    # [5]規格量 [6]規格單位 [7]單複方 [8]支付價 ... [12]製造廠名稱 [13]劑型 [16]ATC代碼
    if "name_en" not in rename.values() and len(cols) >= 9:
        logger.warning("  alias match failed; using positional fallback")
        for dst, idx in [("name_en", 2), ("name_zh", 3), ("generic_name", 4),
                         ("price_twd", 8), ("manufacturer", 12),
                         ("dosage_form", 13), ("atc_code", 16)]:
            if idx < len(cols):
                rename[cols[idx]] = dst
    return rename


def _process_chunk(chunk: pd.DataFrame, source_id: int, conn: sqlite3.Connection) -> int:
    """Insert one chunk into DB. Returns number of rows saved."""
    norm = _norm_col
    chunk.columns = [norm(c) for c in chunk.columns]
    rename = _apply_rename(list(chunk.columns))
    chunk = chunk.rename(columns=rename)

    has_name  = "name_zh" in chunk.columns or "name_en" in chunk.columns
    has_price = "price_twd" in chunk.columns
    if not has_name or not has_price:
        return 0

    saved = 0
    # Use vectorised ops instead of iterrows
    chunk["price_twd"] = pd.to_numeric(chunk.get("price_twd"), errors="coerce")
    chunk = chunk[chunk["price_twd"].notna() & (chunk["price_twd"] > 0)]

    for col in ["name_zh", "name_en", "generic_name", "atc_code", "dosage_form",
                "strength", "manufacturer"]:
        if col not in chunk.columns:
            chunk[col] = None
        else:
            chunk[col] = chunk[col].where(chunk[col].notna(), None)

    for row in chunk.itertuples(index=False):
        name_zh = str(getattr(row, "name_zh") or "").strip() or None
        name_en = str(getattr(row, "name_en") or "").strip() or None
        if not name_zh and not name_en:
            continue

        drug_id = insert_drug(
            conn,
            name_ja=name_zh,
            name_en=name_en,
            generic_name=str(getattr(row, "generic_name") or "").strip() or None,
            atc_code=str(getattr(row, "atc_code") or "").strip() or None,
            dosage_form=str(getattr(row, "dosage_form") or "").strip() or None,
            strength=str(getattr(row, "strength") or "").strip() or None,
            manufacturer=str(getattr(row, "manufacturer") or "").strip() or None,
            source_id=source_id,
        )
        insert_price(
            conn,
            drug_id=drug_id,
            source_id=source_id,
            country="TWN",
            price=float(getattr(row, "price_twd")),
            currency="TWD",
            unit="per unit",
        )
        saved += 1
    return saved


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

    # Detect encoding/separator from first 64KB only
    try:
        enc, sep = _detect_encoding_sep(data)
    except Exception as e:
        logger.error("NHI encoding detection failed: %s", e)
        mark_fetched(conn, source_id)
        return

    # Stream-parse in chunks of 5000 rows to stay within memory limits
    CHUNK = 5000
    try:
        reader = pd.read_csv(
            io.BytesIO(data), encoding=enc, sep=sep,
            low_memory=False, on_bad_lines="skip",
            chunksize=CHUNK,
        )
        for i, chunk in enumerate(reader):
            n = _process_chunk(chunk, source_id, conn)
            saved += n
            if i % 10 == 0:
                logger.info("  chunk %d: saved %d so far", i, saved)
                conn.commit()          # commit every 10 chunks
    except Exception as e:
        logger.error("NHI chunked read failed: %s", e)

    mark_fetched(conn, source_id)
    logger.info("Taiwan NHI done. Saved %d entries.", saved)
