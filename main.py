#!/usr/bin/env python3
"""
Drug Price Tracking System
==========================
Fetches drug pricing data from:
  - Japan MHLW 薬価基準 (NHI drug price standards)
  - WHO/HAI Medicine Prices and Availability survey
  - OECD Health Statistics – Pharmaceutical Expenditure
  - World Bank GDP per capita PPP (all countries)
  - Taiwan NHI 藥品給付支付標準
  - U.S. CMS NADAC (National Average Drug Acquisition Cost, USD/unit)

Usage:
  python main.py fetch                    # fetch all sources
  python main.py fetch mhlw who oecd      # fetch specific sources
  python main.py fetch worldbank          # fetch World Bank GDP PPP
  python main.py fetch nhi                # fetch Taiwan NHI prices
  python main.py fetch us                 # fetch U.S. CMS NADAC prices
  python main.py show                     # summary of stored data
  python main.py search <name>            # search for a drug by name
  python main.py ppp                      # PPP-corrected comparison table
  python main.py ppp --drug aspirin       # filter by drug name
  python main.py ppp --alpha 0.5          # custom elasticity exponent
  python main.py ppp --rate 32.0          # override USD/TWD exchange rate
  python main.py ppp --out table.csv      # save table to CSV
  python main.py export <file.csv>        # export all raw prices to CSV
"""
import argparse
import logging
import sys

import pandas as pd

import db
import ppp as ppp_module
from scrapers import japan_mhlw, who_ems, oecd_health
from scrapers import worldbank_gdp, taiwan_nhi, us_cms_nadac

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRAPERS = {
    "mhlw":      japan_mhlw.fetch,
    "who":       who_ems.fetch,
    "oecd":      oecd_health.fetch,
    "worldbank": worldbank_gdp.fetch,
    "nhi":       taiwan_nhi.fetch,
    "us":        us_cms_nadac.fetch,
}


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_fetch(args: argparse.Namespace) -> None:
    db.init_db()
    targets = args.source or list(SCRAPERS.keys())
    unknown = [t for t in targets if t not in SCRAPERS]
    if unknown:
        print(f"Unknown source(s): {unknown}. Available: {list(SCRAPERS)}", file=sys.stderr)
        sys.exit(1)

    with db.get_conn() as conn:
        for name in targets:
            try:
                SCRAPERS[name](conn)
            except Exception as e:
                logger.error("Scraper '%s' failed: %s", name, e, exc_info=True)

    print("Done.")


def cmd_show(args: argparse.Namespace) -> None:
    db.init_db()
    with db.get_conn() as conn:
        sources = conn.execute(
            "SELECT name, last_fetched, "
            "(SELECT COUNT(*) FROM prices WHERE source_id = sources.id) AS n_prices, "
            "(SELECT COUNT(*) FROM drugs  WHERE source_id = sources.id) AS n_drugs "
            "FROM sources ORDER BY name"
        ).fetchall()

        if not sources:
            print("No data yet. Run: python main.py fetch")
            return

        print(f"\n{'Source':<45} {'Last fetched':<14} {'Drugs':>7} {'Prices':>8}")
        print("-" * 80)
        for s in sources:
            print(f"{s['name']:<45} {s['last_fetched'] or 'never':<14} {s['n_drugs']:>7} {s['n_prices']:>8}")

        totals = conn.execute(
            "SELECT COUNT(*) AS d FROM drugs UNION ALL SELECT COUNT(*) FROM prices"
        ).fetchall()
        print("-" * 80)
        print(f"{'TOTAL':<45} {'':14} {totals[0][0]:>7} {totals[1][0]:>8}\n")


def cmd_search(args: argparse.Namespace) -> None:
    db.init_db()
    q = f"%{args.name}%"
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.name_en, d.name_ja, d.generic_name, d.strength,
                   p.country, p.price, p.currency, p.unit, p.effective_date,
                   s.name AS source
            FROM drugs d
            JOIN prices p ON p.drug_id = d.id
            JOIN sources s ON s.id = p.source_id
            WHERE d.name_en   LIKE ? COLLATE NOCASE
               OR d.name_ja   LIKE ?
               OR d.generic_name LIKE ? COLLATE NOCASE
            ORDER BY d.name_en, p.country
            LIMIT 100
            """,
            (q, q, q),
        ).fetchall()

    if not rows:
        print(f"No results for '{args.name}'.")
        return

    df = pd.DataFrame([dict(r) for r in rows])
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))


def cmd_ppp(args: argparse.Namespace) -> None:
    db.init_db()
    with db.get_conn() as conn:
        df = ppp_module.build_comparison_table(
            conn,
            drug_name=args.drug,
            alpha=args.alpha,
            usd_twd=args.rate,
        )

    if df.empty:
        print("No data for PPP comparison.")
        print("Make sure you have run:")
        print("  python main.py fetch worldbank   # GDP PPP data")
        print("  python main.py fetch nhi         # Taiwan NHI prices")
        print("  python main.py fetch who         # or mhlw / oecd for foreign prices")
        return

    # Pretty-print to terminal
    pd.set_option("display.max_rows",    500)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width",       200)
    pd.set_option("display.float_format", "{:.2f}".format)

    # Drop helper columns for terminal display
    display_cols = ["藥品名", "來源國", "原價_USD", "TW_ref_USD",
                    "TW_ref_TWD", "健保價_TWD", "差異_%"]
    print("\n=== PPP-Corrected Drug Price Comparison ===")
    print(f"Formula: TW_ref = foreign_USD × (TW_GDP / country_GDP)^{args.alpha}")
    print(f"Exchange rate: 1 USD = {args.rate or '(live)'} TWD\n")
    print(df[[c for c in display_cols if c in df.columns]].to_string(index=False))
    print(f"\nTotal rows: {len(df)}")

    if args.out:
        df.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"Saved → {args.out}")


def cmd_export(args: argparse.Namespace) -> None:
    db.init_db()
    with db.get_conn() as conn:
        df = pd.read_sql_query(
            """
            SELECT d.name_en, d.name_ja, d.generic_name, d.atc_code,
                   d.dosage_form, d.strength, d.manufacturer,
                   p.country, p.price, p.currency, p.unit, p.price_usd,
                   p.effective_date, p.fetch_date,
                   s.name AS source
            FROM drugs d
            JOIN prices  p ON p.drug_id   = d.id
            JOIN sources s ON s.id        = p.source_id
            ORDER BY d.name_en, p.country
            """,
            conn,
        )
    df.to_csv(args.file, index=False, encoding="utf-8-sig")
    print(f"Exported {len(df):,} rows → {args.file}")


# ── CLI setup ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drug_tracker",
        description="Drug price tracking system",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # fetch
    fetch_p = sub.add_parser("fetch", help="Download & store price data")
    fetch_p.add_argument(
        "source",
        nargs="*",
        metavar="SOURCE",
        help=f"One or more sources to fetch: {list(SCRAPERS)}. Omit for all.",
    )
    fetch_p.set_defaults(func=cmd_fetch)

    # show
    show_p = sub.add_parser("show", help="Summary of stored data")
    show_p.set_defaults(func=cmd_show)

    # search
    search_p = sub.add_parser("search", help="Search for a drug by name")
    search_p.add_argument("name", help="Partial drug name (English, Japanese, or generic)")
    search_p.set_defaults(func=cmd_search)

    # ppp
    ppp_p = sub.add_parser("ppp", help="PPP-corrected Taiwan reference price comparison")
    ppp_p.add_argument("--drug",  default=None,
                       help="Filter by drug name (partial match, case-insensitive)")
    ppp_p.add_argument("--alpha", type=float, default=ppp_module.ALPHA,
                       help=f"Income elasticity exponent (default: {ppp_module.ALPHA})")
    ppp_p.add_argument("--rate",  type=float, default=None,
                       help="Override USD/TWD exchange rate (default: fetch live)")
    ppp_p.add_argument("--out",   default=None,
                       help="Save comparison table to this CSV file")
    ppp_p.set_defaults(func=cmd_ppp)

    # export
    export_p = sub.add_parser("export", help="Export all raw prices to CSV")
    export_p.add_argument("file", nargs="?", default="drug_prices_export.csv",
                          help="Output CSV file (default: drug_prices_export.csv)")
    export_p.set_defaults(func=cmd_export)

    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
