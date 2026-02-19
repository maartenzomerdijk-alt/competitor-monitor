"""
Side-by-side comparison runner.
Loads the latest snapshot for each URL pair from the DB and calls the AI comparator.
"""

import logging
from typing import Optional

from storage.snapshots import get_all_pages, get_latest_snapshots
from analysis.ai_summary import compare_pages

logger = logging.getLogger(__name__)


def run_comparison_for_slug(slug: str) -> Optional[dict]:
    """
    Load the latest snapshots for the my/competitor pair for a given slug
    and run the AI comparison.

    Returns the comparison dict (from ai_summary.compare_pages) or None
    if either side has no snapshot yet.
    """
    all_pages = get_all_pages()
    pages_for_slug = [p for p in all_pages if p["page_slug"] == slug]

    my_page         = next((p for p in pages_for_slug if p["site"] == "mine"), None)
    competitor_page = next((p for p in pages_for_slug if p["site"] == "competitor"), None)

    if not my_page or not competitor_page:
        logger.warning("Could not find both pages for slug '%s'", slug)
        return None

    my_snaps   = get_latest_snapshots(my_page["id"], n=1)
    comp_snaps = get_latest_snapshots(competitor_page["id"], n=1)

    if not my_snaps:
        logger.warning("No snapshot found for my page (slug=%s)", slug)
        return None
    if not comp_snaps:
        logger.warning("No snapshot found for competitor page (slug=%s)", slug)
        return None

    my_snap   = my_snaps[0]
    comp_snap = comp_snaps[0]

    logger.info("Running AI comparison for slug='%s'", slug)
    result = compare_pages(
        slug=slug,
        my_url=my_page["url"],
        my_text=my_snap["clean_text"] or "",
        my_headings=my_snap["headings"] or [],
        my_word_count=my_snap["word_count"],
        competitor_url=competitor_page["url"],
        competitor_text=comp_snap["clean_text"] or "",
        competitor_headings=comp_snap["headings"] or [],
        competitor_word_count=comp_snap["word_count"],
    )

    result["slug"]                  = slug
    result["my_url"]                = my_page["url"]
    result["competitor_url"]        = competitor_page["url"]
    result["my_word_count"]         = my_snap["word_count"]
    result["competitor_word_count"] = comp_snap["word_count"]
    result["my_scraped_at"]         = my_snap["scraped_at"]
    result["competitor_scraped_at"] = comp_snap["scraped_at"]

    return result


def run_all_comparisons(slugs: list[str]) -> list[dict]:
    """Run comparisons for all provided slugs. Returns list of result dicts."""
    results = []
    for slug in slugs:
        result = run_comparison_for_slug(slug)
        if result:
            results.append(result)
            logger.info(
                "Comparison done for '%s': my score=%d, competitor score=%d",
                slug,
                result.get("my_depth_score", 0),
                result.get("competitor_depth_score", 0),
            )
        else:
            logger.warning("Comparison skipped for slug '%s'", slug)
    return results
