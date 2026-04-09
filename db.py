"""
SQLite database schema and connection utilities for drug price tracking.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import date

DB_PATH = Path(__file__).parent / "drug_prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    url         TEXT,
    description TEXT,
    last_fetched TEXT
);

CREATE TABLE IF NOT EXISTS drugs (
    id           INTEGER PRIMARY KEY,
    name_ja      TEXT,
    name_en      TEXT,
    generic_name TEXT,
    atc_code     TEXT,
    dosage_form  TEXT,
    strength     TEXT,
    manufacturer TEXT,
    source_id    INTEGER REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS prices (
    id             INTEGER PRIMARY KEY,
    drug_id        INTEGER REFERENCES drugs(id),
    source_id      INTEGER REFERENCES sources(id),
    country        TEXT NOT NULL,
    price          REAL,
    currency       TEXT,
    unit           TEXT,
    price_usd      REAL,
    effective_date TEXT,
    fetch_date     TEXT DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS gdp_ppp (
    id           INTEGER PRIMARY KEY,
    country_code TEXT NOT NULL,   -- ISO-3 (e.g. TWN, JPN, USA)
    country_name TEXT,
    year         TEXT,
    gdp_ppp_usd  REAL,            -- GDP per capita PPP, current international $
    fetch_date   TEXT DEFAULT (date('now')),
    UNIQUE(country_code, year)
);

CREATE INDEX IF NOT EXISTS idx_drugs_name_en      ON drugs(name_en);
CREATE INDEX IF NOT EXISTS idx_drugs_generic       ON drugs(generic_name);
CREATE INDEX IF NOT EXISTS idx_prices_drug_id      ON prices(drug_id);
CREATE INDEX IF NOT EXISTS idx_prices_source       ON prices(source_id, country);
CREATE INDEX IF NOT EXISTS idx_gdp_ppp_country     ON gdp_ppp(country_code);
"""


def init_db(path: Path = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn(path: Path = DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_source(conn: sqlite3.Connection, name: str, url: str, description: str) -> int:
    conn.execute(
        """INSERT INTO sources (name, url, description)
           VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET url=excluded.url, description=excluded.description""",
        (name, url, description),
    )
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return row["id"]


def mark_fetched(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute(
        "UPDATE sources SET last_fetched = ? WHERE id = ?",
        (str(date.today()), source_id),
    )


def insert_drug(conn: sqlite3.Connection, **kwargs) -> int:
    fields = list(kwargs.keys())
    placeholders = ", ".join("?" * len(fields))
    cols = ", ".join(fields)
    conn.execute(
        f"INSERT INTO drugs ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_price(conn: sqlite3.Connection, **kwargs) -> None:
    fields = list(kwargs.keys())
    placeholders = ", ".join("?" * len(fields))
    cols = ", ".join(fields)
    conn.execute(
        f"INSERT INTO prices ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
