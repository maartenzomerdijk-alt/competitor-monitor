"""
Notifications: Slack webhook alerts + local JSON report writer.
"""

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REPORTS_DIR = Path("reports")


# ── Slack ─────────────────────────────────────────────────────────────────────

def _slack_webhook_url() -> Optional[str]:
    return os.getenv("SLACK_WEBHOOK_URL")


def send_slack_alert(
    page_url: str,
    page_slug: str,
    site: str,
    change_pct: float,
    old_word_count: int,
    new_word_count: int,
    ai_summary: str,
) -> bool:
    """
    Send a Slack message when a significant change is detected.
    Returns True on success, False on failure.
    """
    webhook_url = _slack_webhook_url()
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return False

    emoji = "🔴" if site == "competitor" else "🟡"
    site_label = "Competitor" if site == "competitor" else "Our"
    word_delta = new_word_count - old_word_count
    delta_str  = f"+{word_delta}" if word_delta >= 0 else str(word_delta)

    message = {
        "text": f"{emoji} *Content Change Detected* — {site_label} Page",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Content Change: {page_slug.replace('-', ' ').title()}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Page:*\n<{page_url}|{page_slug}>"},
                    {"type": "mrkdwn", "text": f"*Site:*\n{site_label}"},
                    {"type": "mrkdwn", "text": f"*Change:*\n{change_pct:.1f}%"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Word count:*\n{old_word_count:,} → {new_word_count:,} ({delta_str})",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*AI Summary:*\n{ai_summary}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Detected at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | Competitor Monitor",
                    }
                ],
            },
        ],
    }

    try:
        resp = requests.post(
            webhook_url,
            json=message,
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Slack alert sent for %s (%.1f%% change)", page_slug, change_pct)
            return True
        else:
            logger.error("Slack webhook returned %d: %s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.error("Failed to send Slack alert: %s", exc)
        return False


def send_comparison_slack_summary(comparisons: list[dict]) -> bool:
    """Send a summary Slack message after all comparisons have run."""
    webhook_url = _slack_webhook_url()
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack comparison summary")
        return False

    if not comparisons:
        return False

    lines = ["*📊 Daily Competitor Comparison Summary*\n"]
    for c in comparisons:
        slug = c.get("slug", "?")
        my_score   = c.get("my_depth_score", 0)
        comp_score = c.get("competitor_depth_score", 0)
        my_wc      = c.get("my_word_count", 0)
        comp_wc    = c.get("competitor_word_count", 0)

        indicator = "✅" if my_score >= comp_score else "⚠️"
        lines.append(
            f"{indicator} *{slug.replace('-', ' ').title()}* — "
            f"My score: {my_score}/10 ({my_wc:,}w) | "
            f"Competitor: {comp_score}/10 ({comp_wc:,}w)"
        )

    message = {
        "text": "\n".join(lines),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        ],
    }

    try:
        resp = requests.post(webhook_url, json=message, timeout=10)
        return resp.status_code == 200
    except Exception as exc:
        logger.error("Failed to send comparison Slack summary: %s", exc)
        return False


# ── JSON report ───────────────────────────────────────────────────────────────

def write_json_report(
    diffs: list[dict],
    comparisons: list[dict],
    report_date: Optional[date] = None,
) -> Path:
    """
    Write a structured JSON report to reports/YYYY-MM-DD.json.
    Returns the path of the written file.
    """
    REPORTS_DIR.mkdir(exist_ok=True)
    report_date = report_date or date.today()
    filepath = REPORTS_DIR / f"{report_date.isoformat()}.json"

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date": report_date.isoformat(),
        "significant_changes": [
            {
                "page_url":      d.get("page_url", ""),
                "page_slug":     d.get("page_slug", ""),
                "site":          d.get("site", ""),
                "change_pct":    d.get("change_pct", 0),
                "old_word_count": d.get("old_word_count", 0),
                "new_word_count": d.get("new_word_count", 0),
                "added_sentences": (d.get("added_text") or "").splitlines()[:10],
                "removed_sentences": (d.get("removed_text") or "").splitlines()[:10],
                "ai_summary":    d.get("ai_summary", ""),
            }
            for d in diffs
        ],
        "comparisons": [
            {
                "slug":                     c.get("slug", ""),
                "my_url":                   c.get("my_url", ""),
                "competitor_url":           c.get("competitor_url", ""),
                "my_word_count":            c.get("my_word_count", 0),
                "competitor_word_count":    c.get("competitor_word_count", 0),
                "my_depth_score":           c.get("my_depth_score", 0),
                "competitor_depth_score":   c.get("competitor_depth_score", 0),
                "content_gaps":             c.get("content_gaps", ""),
                "keywords_they_cover":      c.get("keywords_they_cover", []),
                "recommendations":          c.get("recommendations", ""),
            }
            for c in comparisons
        ],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    logger.info("JSON report written to %s", filepath)
    return filepath
