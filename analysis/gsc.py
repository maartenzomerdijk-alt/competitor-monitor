"""
Google Search Console data fetcher.

Fetches top keywords, position trends, losing-rank alerts, and quick-win
opportunities for each monitored page. Writes results to docs/data/gsc/[slug].json.

Default lookback: 7 days. Trend is compared across the first vs second half of
the lookback window (e.g. days 4–7 vs days 1–3 for a 7-day window).

Authentication: Service Account JSON (set GSC_SERVICE_ACCOUNT_JSON env var).
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_service():
    """Build an authenticated Search Console API service from service account JSON."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Google API packages not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise EnvironmentError("GSC_SERVICE_ACCOUNT_JSON environment variable not set")

    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=_SCOPES
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _ds(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


# ── GSC API queries ───────────────────────────────────────────────────────────

def _page_filter(page_url: str) -> list:
    return [{"dimensionFilterGroups": [{"filters": [
        {"dimension": "page", "operator": "equals", "expression": page_url}
    ]}]}]


def _fetch_top_keywords(service, site_url: str, page_url: str,
                        start: str, end: str, limit: int) -> list:
    """Top keywords for a page, sorted by clicks descending."""
    try:
        resp = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start, "endDate": end,
                "dimensions": ["query"],
                "dimensionFilterGroups": [{"filters": [{
                    "dimension": "page", "operator": "equals",
                    "expression": page_url,
                }]}],
                "rowLimit": limit,
                "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
            },
        ).execute()
        return resp.get("rows", [])
    except Exception as exc:
        logger.error("GSC top-keyword query failed for %s: %s", page_url, exc)
        return []


def _fetch_by_date(service, site_url: str, page_url: str,
                   start: str, end: str) -> list:
    """Keyword × date rows for trend analysis (up to 500 rows)."""
    try:
        resp = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start, "endDate": end,
                "dimensions": ["query", "date"],
                "dimensionFilterGroups": [{"filters": [{
                    "dimension": "page", "operator": "equals",
                    "expression": page_url,
                }]}],
                "rowLimit": 500,
            },
        ).execute()
        return resp.get("rows", [])
    except Exception as exc:
        logger.error("GSC trend query failed for %s: %s", page_url, exc)
        return []


# ── Trend calculation ─────────────────────────────────────────────────────────

def _compute_trends(top_rows: list, date_rows: list, today: datetime,
                    days_lookback: int = 7) -> list:
    """
    For each top keyword, compute:
      position_prev_half — avg position over the first half of the lookback window
      position_last_half — avg position over the second (most recent) half
      position_delta     — recent − older  (negative = improved, positive = dropped)
      trend              — "improving" | "dropping" | "stable"
      opportunity        — impressions ≥ 35 (7-day equiv), position 5–20, CTR < 3 %
    """
    # Group date rows by keyword
    by_kw: dict[str, list] = {}
    for row in date_rows:
        kw = row["keys"][0]
        by_kw.setdefault(kw, []).append(
            {"date": row["keys"][1], "position": row["position"]}
        )

    # Split the lookback window in half for trend comparison
    half          = max(days_lookback // 2, 1)
    cut_recent    = _ds(today - timedelta(days=half))           # recent  >= this date
    cut_old_end   = _ds(today - timedelta(days=half + 1))       # older   <= this date
    cut_old_start = _ds(today - timedelta(days=days_lookback))  # older   >= this date

    # Opportunity threshold scaled to the lookback window (~500 imp per 90 days → ~35 per 7 days)
    imp_threshold = max(int(500 * days_lookback / 90), 5)

    results = []
    for row in top_rows:
        kw      = row["keys"][0]
        dates   = by_kw.get(kw, [])
        old_pos = [d["position"] for d in dates if cut_old_start <= d["date"] <= cut_old_end]
        new_pos = [d["position"] for d in dates if d["date"] >= cut_recent]

        pos_prev = round(sum(old_pos) / len(old_pos), 1) if old_pos else None
        pos_curr = round(sum(new_pos) / len(new_pos), 1) if new_pos else None
        avg_pos  = round(row["position"], 1)
        ctr      = round(row["ctr"] * 100, 2)

        if pos_prev is not None and pos_curr is not None:
            delta = round(pos_curr - pos_prev, 1)
            trend = "improving" if delta <= -2 else "dropping" if delta >= 3 else "stable"
        else:
            delta, trend = None, "stable"

        results.append({
            "keyword":            kw,
            "clicks":             row["clicks"],
            "impressions":        row["impressions"],
            "ctr":                ctr,
            "avg_position":       avg_pos,
            "position_prev_half": pos_prev,
            "position_last_half": pos_curr,
            "position_delta":     delta,
            "trend":              trend,
            "opportunity":        (row["impressions"] >= imp_threshold and 5 <= avg_pos <= 20 and ctr < 3.0),
        })
    return results


def _losing_rank(keywords: list) -> list:
    return sorted(
        [kw for kw in keywords if kw.get("position_delta") is not None and kw["position_delta"] > 3],
        key=lambda x: x["position_delta"], reverse=True,
    )


def _opportunities(keywords: list) -> list:
    return [kw for kw in keywords if kw.get("opportunity")]


# ── Content gap correlation ───────────────────────────────────────────────────

def cross_reference_gaps(gsc_data: dict, keywords_they_cover: list) -> list:
    """
    For each keyword the competitor covers that we don't, check if GSC already
    shows impressions for that keyword. Returns enriched gap list.
    """
    if not gsc_data or not gsc_data.get("keywords"):
        return [{"topic": k} for k in keywords_they_cover]

    gsc_index = {kw["keyword"].lower(): kw for kw in gsc_data["keywords"]}
    results = []
    for gap_kw in keywords_they_cover:
        entry: dict = {"topic": gap_kw}
        gap_lower = gap_kw.lower()
        # Exact or partial match
        for gsc_kw, data in gsc_index.items():
            if gap_lower in gsc_kw or gsc_kw in gap_lower:
                pos = data["avg_position"]
                entry["gsc_signal"] = {
                    "keyword":        data["keyword"],
                    "impressions_7d": data["impressions"],
                    "clicks_7d":      data["clicks"],
                    "avg_position":    pos,
                    "ctr":             data["ctr"],
                    # HIGH = ranking below page 1 (pos > 10); MEDIUM = on page 1 but not top 5
                    "priority":        "HIGH" if pos > 10 else "MEDIUM",
                    "verdict": (
                        f"You're already showing up for this — but at position {pos:.0f}. "
                        "Adding dedicated content could push you to page 1."
                        if pos > 10 else
                        f"Ranking position {pos:.0f} — strengthen this content to climb into the top 5."
                    ),
                }
                break
        results.append(entry)
    return results


# ── Per-page fetch ────────────────────────────────────────────────────────────

def fetch_page_gsc_data(
    service,
    site_url: str,
    slug: str,
    page_url: str,
    days_lookback: int = 7,
    max_keywords: int = 20,
) -> dict:
    today      = datetime.now(timezone.utc)
    end_date   = _ds(today - timedelta(days=2))   # GSC has ~2-day lag
    start_date = _ds(today - timedelta(days=days_lookback))

    logger.info("Fetching GSC for %s (%s → %s)", slug, start_date, end_date)

    top_rows   = _fetch_top_keywords(service, site_url, page_url, start_date, end_date, max_keywords)
    date_rows  = _fetch_by_date(service, site_url, page_url, start_date, end_date)
    keywords   = _compute_trends(top_rows, date_rows, today, days_lookback)
    losing     = _losing_rank(keywords)
    opps       = _opportunities(keywords)

    total_clicks = sum(k["clicks"]      for k in keywords)
    total_impr   = sum(k["impressions"] for k in keywords)
    avg_pos      = round(
        sum(k["avg_position"] * k["impressions"] for k in keywords) / max(total_impr, 1), 1
    ) if keywords else 0.0
    top_kw = keywords[0] if keywords else None

    return {
        "slug":       slug,
        "page_url":   page_url,
        "fetched_at": today.isoformat(),
        "date_range": {"start": start_date, "end": end_date},
        "summary": {
            "total_clicks_7d":      total_clicks,
            "total_impressions_7d": total_impr,
            "avg_position":          avg_pos,
            "top_keyword":           top_kw["keyword"]      if top_kw else None,
            "top_keyword_position":  top_kw["avg_position"] if top_kw else None,
            "keywords_tracked":      len(keywords),
            "losing_rank_count":     len(losing),
            "opportunities_count":   len(opps),
        },
        "keywords":      keywords,
        "losing_rank":   losing,
        "opportunities": opps,
        "gap_correlations": [],   # filled in by run_gsc_pipeline
    }


# ── Pipeline entry point ──────────────────────────────────────────────────────

def run_gsc_pipeline(
    pages_config: list,
    gsc_config: dict,
    comparisons: list,
    output_dir: str = "docs/data/gsc",
) -> dict:
    """
    Fetch GSC data for all pages, cross-reference with AI content gaps,
    write docs/data/gsc/[slug].json. Returns {slug: gsc_data}.
    """
    site_url = gsc_config.get("site_url", "").rstrip("/")
    days     = int(gsc_config.get("days_lookback", 7))
    max_kw   = int(gsc_config.get("max_keywords_per_page", 20))

    if not site_url:
        logger.warning("gsc.site_url not set in config — skipping GSC fetch")
        return {}

    try:
        service = _get_service()
    except (EnvironmentError, ImportError) as exc:
        logger.warning("GSC unavailable: %s — skipping", exc)
        return {}

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    comp_by_slug = {c["slug"]: c for c in comparisons}
    results: dict = {}

    for entry in pages_config:
        slug     = entry["slug"]
        page_url = entry["my_url"]
        try:
            data = fetch_page_gsc_data(service, site_url, slug, page_url, days, max_kw)
            comp = comp_by_slug.get(slug, {})
            data["gap_correlations"] = cross_reference_gaps(
                data, comp.get("keywords_they_cover", [])
            )
            out_path = out_dir / f"{slug}.json"
            out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            logger.info("GSC → %s", out_path)
            results[slug] = data
        except Exception as exc:
            logger.error("GSC pipeline failed for %s: %s", slug, exc)

    return results
