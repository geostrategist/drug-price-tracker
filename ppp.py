"""
PPP (Purchasing Power Parity) price correction for drug pricing analysis.

Formula
-------
  Taiwan reference price (USD) = foreign_price_usd × (TW_gdp / country_gdp) ^ alpha

  where alpha = 0.6  (income elasticity of willingness-to-pay, per Shih et al.)

Comparison table columns
------------------------
  藥品名          generic drug name
  來源國          country of the foreign price
  原價_USD        original foreign price in USD
  TW_ref_USD      PPP-adjusted Taiwan reference price (USD)
  TW_ref_TWD      PPP-adjusted Taiwan reference price (TWD)
  健保價_TWD      Taiwan NHI reimbursement price (TWD)  – if available
  差異_%          (健保價 − TW_ref) / TW_ref × 100  (positive = NHI pays more)

Exchange rate
-------------
  Fetched live from open.er-api.com (free, no key required).
  Falls back to the hardcoded constant USD_TWD_FALLBACK if the API is down.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import requests
import pandas as pd

logger = logging.getLogger(__name__)

ALPHA              = 0.6          # income-elasticity exponent
USD_TWD_FALLBACK   = 31.5         # fallback exchange rate
TW_ISO3            = "TWN"        # Taiwan ISO-3 code (World Bank)
ER_API_URL         = "https://open.er-api.com/v6/latest/USD"
HEADERS            = {"User-Agent": "DrugPriceTracker/1.0 (research)"}


# ── exchange rate ─────────────────────────────────────────────────────────────

def get_usd_twd_rate() -> float:
    """Fetch live USD→TWD exchange rate, fall back to constant if unavailable."""
    try:
        resp = requests.get(ER_API_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"]["TWD"]
        logger.info("Exchange rate USD/TWD: %.4f (live)", rate)
        return float(rate)
    except Exception as e:
        logger.warning("Could not fetch exchange rate (%s); using fallback %.2f", e, USD_TWD_FALLBACK)
        return USD_TWD_FALLBACK


# ── PPP formula ───────────────────────────────────────────────────────────────

def ppp_adjust(
    price_usd: float,
    tw_gdp_ppp: float,
    country_gdp_ppp: float,
    alpha: float = ALPHA,
) -> Optional[float]:
    """
    Return the PPP-adjusted Taiwan reference price in USD.
    Returns None if either GDP value is missing or zero.
    """
    if not price_usd or not tw_gdp_ppp or not country_gdp_ppp:
        return None
    return price_usd * (tw_gdp_ppp / country_gdp_ppp) ** alpha


# ── data assembly ─────────────────────────────────────────────────────────────

def _get_tw_gdp(conn: sqlite3.Connection) -> Optional[float]:
    row = conn.execute(
        "SELECT gdp_ppp_usd FROM gdp_ppp WHERE country_code = ? ORDER BY year DESC LIMIT 1",
        (TW_ISO3,),
    ).fetchone()
    if row:
        return row[0]
    logger.warning("Taiwan GDP PPP not found in database. Run: python main.py fetch worldbank")
    return None


def _latest_gdp(conn: sqlite3.Connection) -> dict:
    """Return {iso3_upper: gdp_ppp_usd} for the most recent year per country."""
    rows = conn.execute(
        """SELECT country_code, gdp_ppp_usd
           FROM gdp_ppp g1
           WHERE year = (SELECT MAX(year) FROM gdp_ppp g2 WHERE g2.country_code = g1.country_code)"""
    ).fetchall()
    return {r["country_code"].upper(): r["gdp_ppp_usd"] for r in rows}


def _iso2_to_iso3_map() -> dict:
    """Best-effort mapping of ISO-2 country codes used in price records to ISO-3."""
    return {
        "JP": "JPN", "US": "USA", "DE": "DEU", "FR": "FRA", "GB": "GBR",
        "CN": "CHN", "KR": "KOR", "AU": "AUS", "CA": "CAN", "IN": "IND",
        "BR": "BRA", "MX": "MEX", "RU": "RUS", "ZA": "ZAF", "NG": "NGA",
        "TH": "THA", "VN": "VNM", "ID": "IDN", "MY": "MYS", "PH": "PHL",
    }


def build_comparison_table(
    conn: sqlite3.Connection,
    drug_name: Optional[str] = None,
    alpha: float = ALPHA,
    usd_twd: Optional[float] = None,
    min_price_usd: float = 0.0,
) -> pd.DataFrame:
    """
    Build the PPP-corrected comparison table.

    Parameters
    ----------
    drug_name       : filter to drugs whose name contains this string (case-insensitive)
    alpha           : PPP exponent (default 0.6)
    usd_twd         : override exchange rate (default: fetch live)
    min_price_usd   : exclude rows with price_usd below this threshold

    Returns
    -------
    DataFrame with columns described in the module docstring.
    """
    tw_gdp   = _get_tw_gdp(conn)
    gdp_map  = _latest_gdp(conn)
    iso_map  = _iso2_to_iso3_map()
    usd_rate = usd_twd if usd_twd is not None else get_usd_twd_rate()

    if tw_gdp is None:
        return pd.DataFrame(columns=[
            "藥品名", "來源國", "原價_USD", "TW_ref_USD",
            "TW_ref_TWD", "健保價_TWD", "差異_%",
        ])

    # ── foreign prices (non-TWN, with price_usd) ─────────────────────────────
    name_filter = "%{}%".format(drug_name) if drug_name else "%"
    foreign_rows = conn.execute(
        """
        SELECT d.generic_name, d.name_en, d.name_ja,
               p.country, p.price_usd, p.price, p.currency, p.unit,
               s.name AS source_name
        FROM prices p
        JOIN drugs   d ON d.id = p.drug_id
        JOIN sources s ON s.id = p.source_id
        WHERE p.country != ?
          AND p.price_usd IS NOT NULL
          AND p.price_usd > ?
          AND (
            d.generic_name LIKE ? COLLATE NOCASE OR
            d.name_en      LIKE ? COLLATE NOCASE OR
            d.name_ja      LIKE ?
          )
        ORDER BY d.generic_name, p.country
        """,
        (TW_ISO3, min_price_usd, name_filter, name_filter, name_filter),
    ).fetchall()

    # ── Taiwan NHI prices (TWD) ───────────────────────────────────────────────
    tw_rows = conn.execute(
        """
        SELECT d.generic_name, d.name_en, d.name_ja,
               p.price AS price_twd
        FROM prices p
        JOIN drugs d ON d.id = p.drug_id
        WHERE p.country = ? AND p.currency = 'TWD'
          AND (
            d.generic_name LIKE ? COLLATE NOCASE OR
            d.name_en      LIKE ? COLLATE NOCASE OR
            d.name_ja      LIKE ?
          )
        """,
        (TW_ISO3, name_filter, name_filter, name_filter),
    ).fetchall()

    # Build Taiwan NHI lookup: generic_name (normalised) -> median price TWD
    def norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    tw_lookup: dict[str, float] = {}
    for r in tw_rows:
        key = norm(r["generic_name"]) or norm(r["name_en"]) or norm(r["name_ja"])
        if not key:
            continue
        tw_lookup.setdefault(key, []).append(r["price_twd"])   # type: ignore[arg-type]
    tw_median = {k: sorted(v)[len(v) // 2] for k, v in tw_lookup.items()}

    # ── assemble output ───────────────────────────────────────────────────────
    records = []
    for r in foreign_rows:
        price_usd = r["price_usd"]
        country   = (r["country"] or "").upper()

        # Resolve ISO-3 (prices stored as ISO-2 or ISO-3 depending on source)
        iso3 = iso_map.get(country, country)   # already ISO-3 → unchanged

        country_gdp = gdp_map.get(iso3)
        if country_gdp is None:
            # Try stripping any trailing noise
            for k in gdp_map:
                if k.startswith(iso3[:2]):
                    country_gdp = gdp_map[k]
                    break

        ref_usd = ppp_adjust(price_usd, tw_gdp, country_gdp, alpha) if country_gdp else None
        ref_twd = ref_usd * usd_rate if ref_usd is not None else None

        drug_key = norm(r["generic_name"]) or norm(r["name_en"]) or norm(r["name_ja"])
        nhi_twd  = tw_median.get(drug_key)

        diff_pct = None
        if nhi_twd is not None and ref_twd and ref_twd != 0:
            diff_pct = (nhi_twd - ref_twd) / ref_twd * 100

        records.append({
            "藥品名":      r["generic_name"] or r["name_en"] or r["name_ja"] or "",
            "來源國":      country,
            "來源":        r["source_name"],
            "原價_USD":    round(price_usd, 4),
            "國家GDP_PPP": round(country_gdp, 0) if country_gdp else None,
            "TW_GDP_PPP":  round(tw_gdp, 0),
            "TW_ref_USD":  round(ref_usd, 4) if ref_usd is not None else None,
            "TW_ref_TWD":  round(ref_twd, 2) if ref_twd is not None else None,
            "健保價_TWD":  round(nhi_twd, 2) if nhi_twd is not None else None,
            "差異_%":      round(diff_pct, 1) if diff_pct is not None else None,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # Sort by drug name then descending absolute diff
    df = df.sort_values(
        by=["藥品名", "差異_%"],
        key=lambda col: col.abs() if col.name == "差異_%" else col,
        ascending=[True, False],
        na_position="last",
    )
    return df.reset_index(drop=True)
