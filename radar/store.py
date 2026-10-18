"""SQLite storage. The database lives gzipped on the `data` branch (db/radar.sqlite.gz)."""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    shop_id INTEGER PRIMARY KEY,
    shop_name TEXT,
    url TEXT,
    first_seen TEXT,
    source TEXT,
    status TEXT,            -- tracked | rejected | dropped
    reject_reason TEXT,
    pod_share REAL,
    created_ts INTEGER,
    currency TEXT,
    last_evaluated TEXT,
    search_hits INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shop_snap (
    date TEXT, shop_id INTEGER,
    sold INTEGER, favorers INTEGER, review_count INTEGER, review_avg REAL, active_listings INTEGER,
    PRIMARY KEY (date, shop_id)
);
CREATE TABLE IF NOT EXISTS listings (
    listing_id INTEGER PRIMARY KEY,
    shop_id INTEGER,
    title TEXT,
    tags TEXT,
    materials TEXT,
    taxonomy_id INTEGER,
    product_type TEXT,
    created_ts INTEGER,
    url TEXT,
    price REAL,
    currency TEXT,
    price_usd REAL,
    personalizable INTEGER,
    partners TEXT,
    listing_type TEXT,
    is_pod INTEGER,
    ip_risk TEXT,
    first_seen TEXT,
    last_seen TEXT,
    state TEXT,
    image_url TEXT,
    image_count INTEGER,
    has_video INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_listings_shop ON listings(shop_id);
CREATE TABLE IF NOT EXISTS listing_snap (
    date TEXT, listing_id INTEGER, views INTEGER, favorers INTEGER, price_usd REAL,
    PRIMARY KEY (date, listing_id)
);
CREATE INDEX IF NOT EXISTS ix_listing_snap_lid ON listing_snap(listing_id);
CREATE TABLE IF NOT EXISTS reviews (
    review_key TEXT PRIMARY KEY,
    shop_id INTEGER, listing_id INTEGER, rating INTEGER, created_ts INTEGER, first_seen TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_listing ON reviews(listing_id);
CREATE TABLE IF NOT EXISTS search_rank (
    date TEXT, keyword TEXT, rank INTEGER, listing_id INTEGER, shop_id INTEGER,
    PRIMARY KEY (date, keyword, rank)
);
CREATE TABLE IF NOT EXISTS taxonomy (id INTEGER PRIMARY KEY, name TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
    date TEXT PRIMARY KEY, started TEXT, finished TEXT, api_calls INTEGER, errors INTEGER, notes TEXT
);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.gz_path = self.data_dir / "db" / "radar.sqlite.gz"
        self._tmpdir = Path(tempfile.mkdtemp(prefix="radar-db-"))
        self.db_path = self._tmpdir / "radar.sqlite"
        if self.gz_path.exists():
            with gzip.open(self.gz_path, "rb") as src, open(self.db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- helpers
    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def execute(self, sql: str, params: tuple | list = ()):
        return self.conn.execute(sql, params)

    def get_meta(self, key: str, default=None):
        val = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(val) if val is not None else default

    def set_meta(self, key: str, value) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def commit(self) -> None:
        self.conn.commit()

    # --------------------------------------------------------- retention
    def prune(self, today: str, retention: dict) -> None:
        from datetime import date, datetime, time, timedelta, timezone

        d = date.fromisoformat(today)

        def cutoff(days: int) -> str:
            return (d - timedelta(days=int(days))).isoformat()

        self.execute("DELETE FROM listing_snap WHERE date < ?", (cutoff(retention.get("listing_snapshots", 120)),))
        self.execute("DELETE FROM search_rank WHERE date < ?", (cutoff(retention.get("search_ranks", 60)),))
        min_day = d - timedelta(days=int(retention.get("reviews", 400)))
        min_ts = int(datetime.combine(min_day, time(), tzinfo=timezone.utc).timestamp())
        self.execute("DELETE FROM reviews WHERE created_ts < ?", (min_ts,))
        # listings not seen for 60 days and no longer referenced
        self.execute(
            "DELETE FROM listings WHERE last_seen < ? AND listing_id NOT IN (SELECT DISTINCT listing_id FROM listing_snap)",
            (cutoff(60),),
        )
        self.commit()

    # -------------------------------------------------------------- close
    def close(self, save: bool = True) -> None:
        self.conn.commit()
        if save:
            self.conn.execute("VACUUM")
        self.conn.close()
        if save:
            self.gz_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_gz = self.gz_path.with_suffix(".gz.tmp")
            with open(self.db_path, "rb") as src, gzip.open(tmp_gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            tmp_gz.replace(self.gz_path)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
