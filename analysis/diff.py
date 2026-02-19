"""
Diff engine: compares two snapshots, computes change %, extracts
added/removed sentences, flags significant changes.
"""

import difflib
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for a meaningful human-readable diff."""
    # Simple sentence splitter — splits on . ! ? followed by whitespace/end
    raw = re.split(r"(?<=[.!?])\s+", text or "")
    return [s.strip() for s in raw if s.strip()]


def compute_diff(old_text: str, new_text: str) -> dict:
    """
    Compare two clean text strings.

    Returns:
        change_pct   (float) — % of characters changed
        added_text   (str)   — sentences present in new but not old
        removed_text (str)   — sentences present in old but not new
        is_significant (bool) — True if change_pct > threshold
    """
    old_text = old_text or ""
    new_text = new_text or ""

    # ── Character-level change % ──────────────────────────────────────────────
    if not old_text and not new_text:
        return {
            "change_pct": 0.0,
            "added_text": "",
            "removed_text": "",
            "is_significant": False,
        }

    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    # ratio() = 2 * M / T  (M = matching chars, T = total chars both strings)
    similarity = matcher.ratio()
    change_pct = round((1.0 - similarity) * 100, 2)

    # ── Sentence-level added / removed ────────────────────────────────────────
    old_sentences = _split_sentences(old_text)
    new_sentences = _split_sentences(new_text)

    old_set = set(old_sentences)
    new_set = set(new_sentences)

    added   = [s for s in new_sentences if s not in old_set]
    removed = [s for s in old_sentences if s not in new_set]

    # Cap to keep DB entries readable (first 50 sentences each direction)
    added_text   = "\n".join(added[:50])
    removed_text = "\n".join(removed[:50])

    logger.debug(
        "Diff: %.1f%% change | +%d sentences | -%d sentences",
        change_pct, len(added), len(removed),
    )

    return {
        "change_pct":     change_pct,
        "added_text":     added_text,
        "removed_text":   removed_text,
        "is_significant": False,  # caller sets this based on threshold
    }


def is_significant_change(change_pct: float, threshold_pct: float = 5.0) -> bool:
    return change_pct >= threshold_pct


def unified_diff_text(old_text: str, new_text: str, context_lines: int = 3) -> str:
    """Return a unified diff string for display / logging purposes."""
    old_lines = (old_text or "").splitlines(keepends=True)
    new_lines = (new_text or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="old",
        tofile="new",
        n=context_lines,
    )
    return "".join(diff)


def run_diff_for_page(
    page_id: int,
    threshold_pct: float = 5.0,
) -> Optional[dict]:
    """
    Pull the two latest snapshots for page_id, compute diff, persist result.
    Returns the diff dict (with `is_significant` set) or None if < 2 snapshots.
    """
    from storage.snapshots import get_latest_snapshots, save_diff

    snapshots = get_latest_snapshots(page_id, n=2)
    if len(snapshots) < 2:
        logger.debug("page_id=%d has fewer than 2 snapshots — skipping diff", page_id)
        return None

    new_snap, old_snap = snapshots[0], snapshots[1]  # newest first

    result = compute_diff(old_snap["clean_text"], new_snap["clean_text"])
    result["is_significant"] = is_significant_change(result["change_pct"], threshold_pct)
    result["page_id"]         = page_id
    result["snapshot_old_id"] = old_snap["id"]
    result["snapshot_new_id"] = new_snap["id"]
    result["old_word_count"]  = old_snap["word_count"]
    result["new_word_count"]  = new_snap["word_count"]

    return result
