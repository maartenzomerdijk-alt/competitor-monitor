"""
CRUD helpers for pages, snapshots and diffs.
"""

import json
import logging
from typing import Optional
from storage.db import db_conn

logger = logging.getLogger(__name__)


# ── Pages ─────────────────────────────────────────────────────────────────────

def get_page_by_url(url: str) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None


def get_all_pages() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM pages ORDER BY page_slug, site").fetchall()
        return [dict(r) for r in rows]


# ── Snapshots ─────────────────────────────────────────────────────────────────

def save_snapshot(
    page_id: int,
    raw_html: str,
    clean_text: str,
    word_count: int,
    title: str,
    h1: str,
    meta_description: str,
    headings: list,
    internal_links: list,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots
                (page_id, raw_html, clean_text, word_count, title, h1,
                 meta_description, headings, internal_links)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                raw_html,
                clean_text,
                word_count,
                title,
                h1,
                meta_description,
                json.dumps(headings),
                json.dumps(internal_links),
            ),
        )
        snap_id = cur.lastrowid
    logger.debug("Saved snapshot %d for page_id=%d (%d words)", snap_id, page_id, word_count)
    return snap_id


def get_latest_snapshots(page_id: int, n: int = 2) -> list[dict]:
    """Return the N most recent snapshots for a page, newest first."""
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM snapshots
            WHERE page_id = ?
            ORDER BY scraped_at DESC
            LIMIT ?
            """,
            (page_id, n),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["headings"]       = json.loads(d["headings"] or "[]")
            d["internal_links"] = json.loads(d["internal_links"] or "[]")
            results.append(d)
        return results


def get_snapshot_by_id(snapshot_id: int) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["headings"]       = json.loads(d["headings"] or "[]")
        d["internal_links"] = json.loads(d["internal_links"] or "[]")
        return d


# ── Diffs ─────────────────────────────────────────────────────────────────────

def save_diff(
    page_id: int,
    snapshot_old_id: int,
    snapshot_new_id: int,
    change_pct: float,
    added_text: str,
    removed_text: str,
    ai_summary: str,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO diffs
                (page_id, snapshot_old_id, snapshot_new_id, change_pct,
                 added_text, removed_text, ai_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (page_id, snapshot_old_id, snapshot_new_id, change_pct,
             added_text, removed_text, ai_summary),
        )
        diff_id = cur.lastrowid
    logger.info(
        "Saved diff %d for page_id=%d (%.1f%% change)", diff_id, page_id, change_pct
    )
    return diff_id


def get_latest_diff(page_id: int) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM diffs
            WHERE page_id = ?
            ORDER BY detected_at DESC
            LIMIT 1
            """,
            (page_id,),
        ).fetchone()
        return dict(row) if row else None
