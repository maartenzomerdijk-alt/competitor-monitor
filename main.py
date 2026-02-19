"""
main.py — CLI entry point for the competitor content monitor.

Usage:
  python main.py --run-now      Run the full pipeline immediately
  python main.py --compare      Run AI comparisons only (requires existing snapshots)
  python main.py --schedule     Start the daily scheduler (blocks)
  python main.py --init-db      Initialise the database only
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Core pipeline ─────────────────────────────────────────────────────────────

async def _scrape_and_store(pages_config: list, settings: dict) -> list[dict]:
    """
    Scrape every URL (mine + competitor), extract content, store snapshot.
    Returns list of page info dicts with latest snapshot IDs attached.
    """
    from scraper.crawler import fetch_page
    from scraper.extractor import extract
    from storage.snapshots import get_page_by_url, save_snapshot

    delay_min   = settings.get("scrape_delay_min", 2)
    delay_max   = settings.get("scrape_delay_max", 5)
    max_retries = settings.get("max_retries", 3)
    retry_wait  = settings.get("retry_wait_seconds", 60)

    results = []
    for entry in pages_config:
        for site_key, url_key in [("mine", "my_url"), ("competitor", "competitor_url")]:
            url = entry[url_key]
            slug = entry["slug"]

            logger.info("Scraping [%s] %s (%s)", site_key, slug, url)
            html = await fetch_page(
                url,
                delay_min=delay_min,
                delay_max=delay_max,
                max_retries=max_retries,
                retry_wait=retry_wait,
            )

            if html is None:
                logger.error("Failed to fetch %s — skipping", url)
                continue

            extracted = extract(html, url)
            page = get_page_by_url(url)
            if not page:
                logger.error("Page not found in DB for URL %s", url)
                continue

            snap_id = save_snapshot(
                page_id=page["id"],
                raw_html=html,
                clean_text=extracted["clean_text"],
                word_count=extracted["word_count"],
                title=extracted["title"],
                h1=extracted["h1"],
                meta_description=extracted["meta_description"],
                headings=extracted["headings"],
                internal_links=extracted["internal_links"],
            )

            results.append({
                "page_id":    page["id"],
                "snap_id":    snap_id,
                "url":        url,
                "slug":       slug,
                "site":       site_key,
                "word_count": extracted["word_count"],
            })

    return results


def _run_diffs_and_notify(pages_config: list, settings: dict) -> list[dict]:
    """
    For each page, compute diff between the two latest snapshots.
    Fire Slack alerts for significant changes.
    Returns list of significant diff dicts.
    """
    from analysis.diff import run_diff_for_page
    from analysis.ai_summary import summarise_diff
    from notifications.alerts import send_slack_alert
    from storage.snapshots import get_page_by_url, get_latest_snapshots, save_diff

    threshold = settings.get("change_threshold_pct", 5.0)
    significant = []

    for entry in pages_config:
        for site_key, url_key in [("mine", "my_url"), ("competitor", "competitor_url")]:
            url  = entry[url_key]
            slug = entry["slug"]

            page = get_page_by_url(url)
            if not page:
                continue

            diff_result = run_diff_for_page(page["id"], threshold_pct=threshold)
            if diff_result is None:
                continue

            change_pct = diff_result["change_pct"]
            is_sig     = diff_result["is_significant"]

            logger.info(
                "[%s] %s — %.1f%% change (%s)",
                site_key, slug, change_pct,
                "SIGNIFICANT" if is_sig else "minor",
            )

            if is_sig:
                snaps = get_latest_snapshots(page["id"], n=2)
                new_snap, old_snap = snaps[0], snaps[1]

                ai_summary = summarise_diff(
                    page_url=url,
                    page_slug=slug,
                    old_text=old_snap["clean_text"] or "",
                    new_text=new_snap["clean_text"] or "",
                    added_text=diff_result["added_text"],
                    removed_text=diff_result["removed_text"],
                    change_pct=change_pct,
                )

                save_diff(
                    page_id=page["id"],
                    snapshot_old_id=diff_result["snapshot_old_id"],
                    snapshot_new_id=diff_result["snapshot_new_id"],
                    change_pct=change_pct,
                    added_text=diff_result["added_text"],
                    removed_text=diff_result["removed_text"],
                    ai_summary=ai_summary,
                )

                send_slack_alert(
                    page_url=url,
                    page_slug=slug,
                    site=site_key,
                    change_pct=change_pct,
                    old_word_count=diff_result["old_word_count"],
                    new_word_count=diff_result["new_word_count"],
                    ai_summary=ai_summary,
                )

                significant.append({
                    **diff_result,
                    "page_url":  url,
                    "page_slug": slug,
                    "site":      site_key,
                    "ai_summary": ai_summary,
                })

    return significant


def _run_comparisons(pages_config: list) -> list[dict]:
    """Run side-by-side AI comparisons for all slugs."""
    from analysis.compare import run_all_comparisons
    from notifications.alerts import send_comparison_slack_summary

    slugs = [entry["slug"] for entry in pages_config]
    comparisons = run_all_comparisons(slugs)
    send_comparison_slack_summary(comparisons)
    return comparisons


def write_dashboard_data(pages_config: list, comparisons: list, significant_diffs: list, gsc_data: dict = None) -> None:
    """
    Write docs/data/latest.json (always overwrite) and append to
    docs/data/history.json (keeps last 90 days of daily entries).
    """
    import json
    from datetime import datetime, timezone, timedelta
    from storage.snapshots import get_page_by_url, get_latest_snapshots, get_latest_diff

    docs_data = Path("docs/data")
    docs_data.mkdir(parents=True, exist_ok=True)

    # Index comparisons by slug for quick lookup
    comp_by_slug = {c["slug"]: c for c in comparisons}

    # Index significant diffs by url
    sig_by_url = {}
    for d in significant_diffs:
        sig_by_url[d["page_url"]] = d

    now_iso = datetime.now(timezone.utc).isoformat()
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    page_entries = []
    for entry in pages_config:
        slug         = entry["slug"]
        my_url       = entry["my_url"]
        comp_url     = entry["competitor_url"]
        comparison   = comp_by_slug.get(slug, {})

        # Fetch latest snapshots for both sides
        my_page   = get_page_by_url(my_url)
        comp_page = get_page_by_url(comp_url)

        my_snap   = (get_latest_snapshots(my_page["id"],   n=1) or [None])[0] if my_page   else None
        comp_snap = (get_latest_snapshots(comp_page["id"], n=1) or [None])[0] if comp_page else None

        my_wc   = my_snap["word_count"]   if my_snap   else 0
        comp_wc = comp_snap["word_count"] if comp_snap else 0

        # Latest diff for MY page (we track our changes)
        my_diff = None
        if my_page:
            my_diff = get_latest_diff(my_page["id"])

        change_pct     = my_diff["change_pct"]    if my_diff else 0.0
        change_summary = my_diff["ai_summary"]    if my_diff else ""
        last_changed   = my_diff["detected_at"]   if my_diff else None

        # Determine status
        if my_snap is None and comp_snap is None:
            status = "error"
        elif slug in sig_by_url or (my_diff and my_diff["change_pct"] >= 5.0):
            status = "changed"
        else:
            status = "unchanged"

        # Content gaps as a list (split on newline / semicolon if string)
        raw_gaps = comparison.get("content_gaps", "")
        if isinstance(raw_gaps, list):
            gaps_list = raw_gaps
        elif isinstance(raw_gaps, str) and raw_gaps.startswith("[Unavailable"):
            gaps_list = []
        else:
            gaps_list = [g.strip() for g in raw_gaps.replace(";", "\n").splitlines() if g.strip()]

        page_entries.append({
            "slug":                        slug,
            "my_url":                      my_url,
            "competitor_url":              comp_url,
            "my_word_count":               my_wc,
            "competitor_word_count":       comp_wc,
            "content_depth_score_mine":        comparison.get("my_depth_score", 0),
            "content_depth_score_competitor":  comparison.get("competitor_depth_score", 0),
            "my_depth_score_weighted":         comparison.get("my_depth_score_weighted", 0),
            "competitor_depth_score_weighted": comparison.get("competitor_depth_score_weighted", 0),
            "dimensions":                  comparison.get("dimensions", []),
            "my_dimension_scores":         comparison.get("my_dimension_scores", {}),
            "competitor_dimension_scores": comparison.get("competitor_dimension_scores", {}),
            "last_change_detected":        last_changed,
            "change_pct":                  round(change_pct, 2),
            "change_summary":              change_summary,
            "content_gaps":                gaps_list,
            "keywords_they_cover":         comparison.get("keywords_they_cover", []),
            "recommendations":             comparison.get("recommendations", ""),
            "status":                      status,
            # Extra fields for the comparison modal
            "my_title":        my_snap.get("title", "")        if my_snap   else "",
            "my_h1":           my_snap.get("h1", "")           if my_snap   else "",
            "my_headings":     my_snap.get("headings", [])     if my_snap   else [],
            "comp_title":      comp_snap.get("title", "")      if comp_snap else "",
            "comp_h1":         comp_snap.get("h1", "")         if comp_snap else "",
            "comp_headings":   comp_snap.get("headings", [])   if comp_snap else [],
        })

    latest = {
        "generated_at": now_iso,
        "date":         today,
        "pages":        page_entries,
        "summary": {
            "total_pages":       len(page_entries),
            "changed":           sum(1 for p in page_entries if p["status"] == "changed"),
            "unchanged":         sum(1 for p in page_entries if p["status"] == "unchanged"),
            "errors":            sum(1 for p in page_entries if p["status"] == "error"),
            "avg_my_words":      int(sum(p["my_word_count"] for p in page_entries) / max(len(page_entries), 1)),
            "avg_comp_words":    int(sum(p["competitor_word_count"] for p in page_entries) / max(len(page_entries), 1)),
        },
    }

    latest_path = docs_data / "latest.json"
    latest_path.write_text(json.dumps(latest, indent=2, default=str), encoding="utf-8")
    logger.info("Dashboard latest.json written to %s", latest_path)

    # ── history.json — append today's summary row, keep 90 days ──────────────
    history_path = docs_data / "history.json"
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            history = []
    else:
        history = []

    # Remove any existing entry for today (idempotent re-runs)
    history = [h for h in history if h.get("date") != today]

    history.append({
        "date":       today,
        "changed":    latest["summary"]["changed"],
        "unchanged":  latest["summary"]["unchanged"],
        "errors":     latest["summary"]["errors"],
        "page_stats": [
            {
                "slug":               p["slug"],
                "my_word_count":      p["my_word_count"],
                "comp_word_count":    p["competitor_word_count"],
                "change_pct":         p["change_pct"],
                "status":             p["status"],
            }
            for p in page_entries
        ],
    })

    # Keep last 90 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date", "") >= cutoff]
    history.sort(key=lambda h: h["date"])

    history_path.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")
    logger.info("Dashboard history.json updated at %s", history_path)

    # ── gsc_summary.json — aggregate across all pages ─────────────────────────
    if gsc_data:
        total_clicks  = sum(v["summary"]["total_clicks_90d"]      for v in gsc_data.values())
        total_impr    = sum(v["summary"]["total_impressions_90d"]  for v in gsc_data.values())
        losing_count  = sum(v["summary"]["losing_rank_count"]      for v in gsc_data.values())
        opp_count     = sum(v["summary"]["opportunities_count"]    for v in gsc_data.values())
        gsc_summary = {
            "generated_at":            now_iso,
            "total_clicks_90d":        total_clicks,
            "total_impressions_90d":   total_impr,
            "keywords_losing_rank":    losing_count,
            "quick_win_opportunities": opp_count,
        }
        gsc_sum_path = docs_data / "gsc_summary.json"
        gsc_sum_path.write_text(json.dumps(gsc_summary, indent=2), encoding="utf-8")
        logger.info("GSC summary written to %s", gsc_sum_path)


def _run_gsc(pages_config: list, config: dict, comparisons: list) -> dict:
    """Fetch GSC data for all pages. Skips gracefully if not configured."""
    gsc_config = config.get("gsc", {})
    if not gsc_config.get("site_url"):
        logger.info("GSC not configured in config.yaml — skipping")
        return {}
    try:
        from analysis.gsc import run_gsc_pipeline
        return run_gsc_pipeline(pages_config, gsc_config, comparisons)
    except Exception as exc:
        logger.warning("GSC pipeline failed: %s — skipping", exc)
        return {}


def run_full_pipeline():
    """Entry point called by the scheduler and --run-now."""
    config   = load_config()
    pages    = config["pages"]
    settings = config.get("settings", {})

    from storage.db import init_db, seed_pages
    init_db()
    seed_pages(pages)

    logger.info("=== Starting scrape phase ===")
    asyncio.run(_scrape_and_store(pages, settings))

    logger.info("=== Starting diff phase ===")
    significant_diffs = _run_diffs_and_notify(pages, settings)

    logger.info("=== Starting comparison phase ===")
    comparisons = _run_comparisons(pages)

    logger.info("=== Starting GSC data fetch ===")
    gsc_data = _run_gsc(pages, config, comparisons)

    logger.info("=== Writing report ===")
    from notifications.alerts import write_json_report
    report_path = write_json_report(significant_diffs, comparisons)
    logger.info("Report written to %s", report_path)

    logger.info("=== Writing dashboard data ===")
    write_dashboard_data(pages, comparisons, significant_diffs, gsc_data=gsc_data)

    logger.info(
        "Pipeline complete — %d significant changes, %d comparisons, %d pages with GSC data",
        len(significant_diffs),
        len(comparisons),
        len(gsc_data),
    )


def run_compare_only():
    """Run comparisons only (requires existing snapshots)."""
    config = load_config()
    pages  = config["pages"]

    from storage.db import init_db, seed_pages
    init_db()
    seed_pages(pages)

    logger.info("=== Running comparison phase only ===")
    comparisons = _run_comparisons(pages)

    from notifications.alerts import write_json_report
    report_path = write_json_report([], comparisons)
    logger.info("Comparison report written to %s", report_path)

    # Pretty-print to console
    for c in comparisons:
        print(f"\n{'='*60}")
        print(f"  Slug:           {c['slug']}")
        print(f"  My score:       {c.get('my_depth_score',0)}/10  ({c.get('my_word_count',0):,} words)")
        print(f"  Competitor:     {c.get('competitor_depth_score',0)}/10  ({c.get('competitor_word_count',0):,} words)")
        print(f"\n  Content Gaps:\n{c.get('content_gaps','')}")
        print(f"\n  Keywords they cover:\n  {', '.join(c.get('keywords_they_cover',[]))}")
        print(f"\n  Recommendations:\n{c.get('recommendations','')}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Competitor Content Monitor — livefootballtickets.com vs seatpick.com"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the full pipeline immediately (scrape → diff → compare → report)",
    )
    group.add_argument(
        "--compare",
        action="store_true",
        help="Run AI side-by-side comparisons only (requires existing snapshots)",
    )
    group.add_argument(
        "--schedule",
        action="store_true",
        help="Start the daily scheduler (blocks until interrupted)",
    )
    group.add_argument(
        "--init-db",
        action="store_true",
        help="Initialise the database and seed page records only",
    )

    args = parser.parse_args()

    if args.run_now:
        run_full_pipeline()

    elif args.compare:
        run_compare_only()

    elif args.schedule:
        config   = load_config()
        settings = config.get("settings", {})
        hour     = settings.get("schedule_hour", 8)
        from scheduler import start_scheduler
        start_scheduler(run_full_pipeline, schedule_hour=hour)

    elif args.init_db:
        config = load_config()
        from storage.db import init_db, seed_pages
        init_db()
        seed_pages(config["pages"])
        logger.info("Database initialised and pages seeded.")


if __name__ == "__main__":
    main()
