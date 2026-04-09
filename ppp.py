"""
PPP (Purchasing Power Parity) price correction for drug pricing analysis.

Formula
-------
  Taiwan reference price (USD) = foreign_price_usd × (TW_gdp / country_gdp) ^ alpha

  where alpha = 0.6  (income elasticity of willingness-to-pay, per Shih et al.)

Cross-country matching strategy (in priority order)
----------------------------------------------------
  1. ATC code (exact)            — most reliable
  2. generic_name exact match    — case-insensitive
  3. generic_name token match    — first word of NHI generic matches foreign name
  4. name_en partial match       — foreign name_ja/name_en contains NHI name_en

Comparison table columns
------------------------
  藥品名          generic drug name
  比對方式        how the NHI price was matched
  來源國          country of the foreign price
  原價_USD        original foreign price in USD
  TW_ref_USD      PPP-adjusted Taiwan reference price (USD)
  TW_ref_TWD      PPP-adjusted Taiwan reference price (TWD)
  健保價_TWD      Taiwan NHI reimbursement price (TWD)  – if available
  差異_%          (健保價 − TW_ref) / TW_ref × 100  (positive = NHI pays more)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

import requests
import pandas as pd

import drug_bridge as _bridge

logger = logging.getLogger(__name__)

# Build bridge lookup at import time (fast, in-memory)
_BRIDGE_EXACT, _BRIDGE_TOKEN = _bridge.build_index()

ALPHA            = 0.6
USD_TWD_FALLBACK = 31.5
TW_ISO3          = "TWN"
ER_API_URL       = "https://open.er-api.com/v6/latest/USD"
HEADERS          = {"User-Agent": "DrugPriceTracker/1.0 (research)"}


# ── exchange rate ─────────────────────────────────────────────────────────────

def get_usd_twd_rate() -> float:
    try:
        resp = requests.get(ER_API_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["TWD"])
        logger.info("Exchange rate USD/TWD: %.4f (live)", rate)
        return rate
    except Exception as e:
        logger.warning("Exchange rate unavailable (%s); using %.2f", e, USD_TWD_FALLBACK)
        return USD_TWD_FALLBACK


# ── PPP formula ───────────────────────────────────────────────────────────────

def ppp_adjust(price_usd: float, tw_gdp: float,
               country_gdp: float, alpha: float = ALPHA) -> Optional[float]:
    if not price_usd or not tw_gdp or not country_gdp:
        return None
    return price_usd * (tw_gdp / country_gdp) ** alpha


# ── helpers ───────────────────────────────────────────────────────────────────

def _norm(s: Optional[str]) -> str:
    """Lowercase, strip whitespace."""
    return (s or "").strip().lower()


def _first_token(s: str) -> str:
    """Return first word-like token, e.g. 'QUETIAPINE (AS FUMARATE)' -> 'quetiapine'."""
    m = re.match(r"[a-z0-9]+", s.lower())
    return m.group(0) if m else s.split()[0] if s.split() else s


def _translate_jp(jp_generic: Optional[str]) -> Optional[str]:
    """
    Translate Japanese INN name to English using the bridge table.
    Returns English INN or None if not found.
    """
    if not jp_generic:
        return None
    key = _norm(jp_generic)
    # Exact match
    if key in _BRIDGE_EXACT:
        return _BRIDGE_EXACT[key]
    # Token match: first 5 chars of Japanese name
    stripped = re.sub(r"[\s　・（）()0-9０-９]", "", key)
    tok = stripped[:5] if len(stripped) >= 5 else stripped
    if len(tok) >= 3 and tok in _BRIDGE_TOKEN:
        return _BRIDGE_TOKEN[tok]
    return None


def _get_tw_gdp(conn: sqlite3.Connection) -> Optional[float]:
    row = conn.execute(
        "SELECT gdp_ppp_usd FROM gdp_ppp WHERE country_code=? ORDER BY year DESC LIMIT 1",
        (TW_ISO3,),
    ).fetchone()
    if row:
        return row[0]
    logger.warning("Taiwan GDP PPP not found. Run: python main.py fetch worldbank")
    return None


def _latest_gdp(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT country_code, gdp_ppp_usd FROM gdp_ppp g1
           WHERE year = (SELECT MAX(year) FROM gdp_ppp g2 WHERE g2.country_code=g1.country_code)"""
    ).fetchall()
    return {r["country_code"].upper(): r["gdp_ppp_usd"] for r in rows}


def _iso2_to_iso3_map() -> dict:
    return {
        "JP": "JPN", "US": "USA", "DE": "DEU", "FR": "FRA", "GB": "GBR",
        "CN": "CHN", "KR": "KOR", "AU": "AUS", "CA": "CAN", "IN": "IND",
        "BR": "BRA", "MX": "MEX", "RU": "RUS", "ZA": "ZAF", "NG": "NGA",
        "TH": "THA", "VN": "VNM", "ID": "IDN", "MY": "MYS", "PH": "PHL",
    }


# ── Taiwan NHI lookup tables ──────────────────────────────────────────────────

class NHILookup:
    """
    Multi-strategy lookup for Taiwan NHI prices.

    Priority:
      1. ATC code  (exact)
      2. generic_name  (exact, case-insensitive)
      3. generic_name first token  (e.g. 'quetiapine')
      4. name_en partial           (NHI English name contained in foreign name)
    """

    def __init__(self, conn: sqlite3.Connection, name_filter: str):
        rows = conn.execute(
            """
            SELECT d.generic_name, d.name_en, d.name_ja, d.atc_code,
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

        # {key -> [prices]}
        self._atc:     dict[str, list[float]] = {}   # ATC code -> prices
        self._generic: dict[str, list[float]] = {}   # generic name -> prices
        self._token:   dict[str, list[float]] = {}   # first token -> prices
        self._name_en: dict[str, list[float]] = {}   # English name -> prices

        for r in rows:
            price = r["price_twd"]
            if price is None or price <= 0:
                continue

            atc = _norm(r["atc_code"])
            # ATC: use full code AND first 5 chars (pharmacological subgroup)
            for atc_key in {atc, atc[:5]} - {""}:
                self._atc.setdefault(atc_key, []).append(price)

            gen = _norm(r["generic_name"])
            if gen:
                self._generic.setdefault(gen, []).append(price)
                tok = _first_token(gen)
                if len(tok) >= 4:
                    self._token.setdefault(tok, []).append(price)

            en = _norm(r["name_en"])
            if en:
                self._name_en.setdefault(en, []).append(price)

        # Pre-compute medians
        self.atc     = {k: self._median(v) for k, v in self._atc.items()}
        self.generic = {k: self._median(v) for k, v in self._generic.items()}
        self.token   = {k: self._median(v) for k, v in self._token.items()}
        self.name_en = {k: self._median(v) for k, v in self._name_en.items()}

    @staticmethod
    def _median(lst: list[float]) -> float:
        s = sorted(lst)
        return s[len(s) // 2]

    def lookup(self, atc_code: Optional[str], generic: Optional[str],
               name_en: Optional[str], name_ja: Optional[str]
               ) -> tuple[Optional[float], str]:
        """
        Return (median_price_twd, match_method) or (None, '') if no match.
        """
        # 1. ATC exact
        atc = _norm(atc_code)
        if atc and atc in self.atc:
            return self.atc[atc], "ATC"

        # 2. ATC subgroup (first 5 chars)
        if len(atc) >= 5 and atc[:5] in self.atc:
            return self.atc[atc[:5]], "ATC-5"

        # 3. Generic exact
        gen = _norm(generic)
        if gen and gen in self.generic:
            return self.generic[gen], "generic"

        # 4. Generic first token
        if gen:
            tok = _first_token(gen)
            if len(tok) >= 4 and tok in self.token:
                return self.token[tok], "generic-token"

        # 5. name_en partial: check if any NHI English name is contained in
        #    the foreign name_en or name_ja
        foreign_text = _norm(name_en) + " " + _norm(name_ja)
        for nhi_name, price in self.name_en.items():
            if len(nhi_name) >= 5 and nhi_name in foreign_text:
                return price, "name_en"

        # 6. Foreign name_en first token vs NHI token index
        en_tok = _first_token(_norm(name_en or name_ja or ""))
        if len(en_tok) >= 4 and en_tok in self.token:
            return self.token[en_tok], "name-token"

        return None, ""


# ── main entry point ──────────────────────────────────────────────────────────

def build_comparison_table(
    conn: sqlite3.Connection,
    drug_name: Optional[str] = None,
    alpha: float = ALPHA,
    usd_twd: Optional[float] = None,
    min_price_usd: float = 0.0,
) -> pd.DataFrame:
    tw_gdp   = _get_tw_gdp(conn)
    gdp_map  = _latest_gdp(conn)
    iso_map  = _iso2_to_iso3_map()
    usd_rate = usd_twd if usd_twd is not None else get_usd_twd_rate()

    if tw_gdp is None:
        return pd.DataFrame(columns=[
            "藥品名", "比對方式", "來源國", "原價_USD",
            "TW_ref_USD", "TW_ref_TWD", "健保價_TWD", "差異_%",
        ])

    name_filter = f"%{drug_name}%" if drug_name else "%"

    foreign_rows = conn.execute(
        """
        SELECT d.generic_name, d.name_en, d.name_ja, d.atc_code,
               p.country, p.price_usd, s.name AS source_name
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
        ORDER BY d.name_ja, p.country
        """,
        (TW_ISO3, min_price_usd, name_filter, name_filter, name_filter),
    ).fetchall()

    if not foreign_rows:
        return pd.DataFrame()

    nhi = NHILookup(conn, name_filter)

    records = []
    for r in foreign_rows:
        price_usd = r["price_usd"]
        country   = (r["country"] or "").upper()
        iso3      = iso_map.get(country, country)

        country_gdp = gdp_map.get(iso3)
        if country_gdp is None:
            for k in gdp_map:
                if k.startswith(iso3[:2]):
                    country_gdp = gdp_map[k]
                    break

        ref_usd = ppp_adjust(price_usd, tw_gdp, country_gdp, alpha) if country_gdp else None
        ref_twd = ref_usd * usd_rate if ref_usd is not None else None

        # Translate Japanese generic name to English for cross-country matching
        generic_en = r["generic_name"]
        if not generic_en or not re.match(r"[a-z]", (generic_en or "").lower()):
            # generic_name is Japanese — translate via bridge table
            translated = _translate_jp(r["generic_name"]) or _translate_jp(r["name_ja"])
            if translated:
                generic_en = translated

        nhi_price, match_method = nhi.lookup(
            r["atc_code"], generic_en, r["name_en"], r["name_ja"]
        )
        if match_method and r["generic_name"] != generic_en:
            match_method = f"bridge→{match_method}"

        diff_pct = None
        if nhi_price is not None and ref_twd and ref_twd != 0:
            diff_pct = (nhi_price - ref_twd) / ref_twd * 100

        records.append({
            "藥品名":      r["name_ja"] or r["name_en"] or r["generic_name"] or "",
            "比對方式":    match_method or "—",
            "來源國":      country,
            "原價_USD":    round(price_usd, 4),
            "國家GDP_PPP": round(country_gdp, 0) if country_gdp else None,
            "TW_GDP_PPP":  round(tw_gdp, 0),
            "TW_ref_USD":  round(ref_usd, 4) if ref_usd is not None else None,
            "TW_ref_TWD":  round(ref_twd, 2) if ref_twd is not None else None,
            "健保價_TWD":  round(nhi_price, 2) if nhi_price is not None else None,
            "差異_%":      round(diff_pct, 1) if diff_pct is not None else None,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    return df.sort_values(by=["差異_%"], ascending=False, na_position="last").reset_index(drop=True)
