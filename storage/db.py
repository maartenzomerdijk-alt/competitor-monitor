"""
SQLite database setup and connection management.
"""

import sqlite3
import os
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "competitor_monitor.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they don't exist."""
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT    NOT NULL UNIQUE,
                site        TEXT    NOT NULL CHECK(site IN ('mine', 'competitor')),
                page_slug   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id          INTEGER NOT NULL REFERENCES pages(id),
                scraped_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                raw_html         TEXT,
                clean_text       TEXT,
                word_count       INTEGER NOT NULL DEFAULT 0,
                title            TEXT,
                h1               TEXT,
                meta_description TEXT,
                headings         TEXT,   -- JSON array of {level, text}
                internal_links   TEXT    -- JSON array of href strings
            );

            CREATE TABLE IF NOT EXISTS diffs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id         INTEGER NOT NULL REFERENCES pages(id),
                snapshot_old_id INTEGER NOT NULL REFERENCES snapshots(id),
                snapshot_new_id INTEGER NOT NULL REFERENCES snapshots(id),
                change_pct      REAL    NOT NULL DEFAULT 0,
                added_text      TEXT,
                removed_text    TEXT,
                ai_summary      TEXT,
                detected_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_page   ON snapshots(page_id, scraped_at DESC);
            CREATE INDEX IF NOT EXISTS idx_diffs_page       ON diffs(page_id, detected_at DESC);
        """)
    logger.info("Database initialised at %s", DB_PATH)


def seed_pages(pages_config: list) -> None:
    """Upsert page records from config."""
    with db_conn() as conn:
        for entry in pages_config:
            slug = entry["slug"]
            for site, url_key in [("mine", "my_url"), ("competitor", "competitor_url")]:
                url = entry[url_key]
                conn.execute(
                    """
                    INSERT INTO pages (url, site, page_slug)
                    VALUES (?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        site      = excluded.site,
                        page_slug = excluded.page_slug
                    """,
                    (url, site, slug),
                )
    logger.info("Seeded %d page pairs into DB", len(pages_config))
