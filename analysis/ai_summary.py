"""
Claude-powered analysis:
  1. Summarise a detected diff (what changed, why it matters)
  2. Side-by-side page comparison (content gaps, keyword coverage, depth score)
"""

import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TEXT_CHARS = 12_000   # truncate long texts before sending to keep tokens reasonable


def _client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key)


def _truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"


# ── 1. Diff summary ───────────────────────────────────────────────────────────

def summarise_diff(
    page_url: str,
    page_slug: str,
    old_text: str,
    new_text: str,
    added_text: str,
    removed_text: str,
    change_pct: float,
) -> str:
    """
    Ask Claude to summarise what changed on a competitor page and assess
    the strategic intent behind the change.

    Returns a plain-text summary (2-4 sentences).
    """
    prompt = f"""You are a competitive intelligence analyst for a football ticket marketplace.

A competitor page has changed significantly.

Page: {page_url} (slug: {page_slug})
Change level: {change_pct:.1f}% of content changed

--- CONTENT ADDED ---
{_truncate(added_text, 4000)}

--- CONTENT REMOVED ---
{_truncate(removed_text, 4000)}

--- OLD FULL TEXT (truncated) ---
{_truncate(old_text, 3000)}

--- NEW FULL TEXT (truncated) ---
{_truncate(new_text, 3000)}

Please provide a concise analysis (2-4 sentences) covering:
1. What specifically changed (new sections, removed info, updated facts)
2. Any new topics, keywords, or angles the competitor has added
3. Your assessment of the strategic intent — are they targeting new keywords, improving trust signals, adding urgency, etc.?

Be direct and specific. Focus on what matters for a competing ticket marketplace."""

    try:
        client = _client()
        message = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        logger.error("Claude diff summary failed: %s", exc)
        return f"[AI summary unavailable: {exc}]"


# ── 2. Side-by-side page comparison ──────────────────────────────────────────

def compare_pages(
    slug: str,
    my_url: str,
    my_text: str,
    my_headings: list,
    my_word_count: int,
    competitor_url: str,
    competitor_text: str,
    competitor_headings: list,
    competitor_word_count: int,
) -> dict:
    """
    Ask Claude to do a side-by-side content comparison between my page and
    the competitor's equivalent page.

    Returns a dict with keys:
        content_gaps        (str)  — topics/keywords competitor covers that I don't
        keywords_they_cover (list) — specific keywords/phrases identified
        my_depth_score      (int)  — 1-10
        competitor_depth_score (int) — 1-10
        recommendations     (str)  — what to add/improve on my page
        raw_response        (str)  — full Claude response
    """
    my_headings_text    = "\n".join(f"  [{h['level'].upper()}] {h['text']}" for h in my_headings)
    comp_headings_text  = "\n".join(f"  [{h['level'].upper()}] {h['text']}" for h in competitor_headings)

    prompt = f"""You are a senior SEO and content strategist for a football ticket marketplace.

Compare these two pages selling tickets for the same topic: "{slug}"

=== MY PAGE ({my_url}) ===
Word count: {my_word_count}
Headings:
{my_headings_text or '  (none detected)'}

Content (truncated):
{_truncate(my_text, 4000)}

=== COMPETITOR PAGE ({competitor_url}) ===
Word count: {competitor_word_count}
Headings:
{comp_headings_text or '  (none detected)'}

Content (truncated):
{_truncate(competitor_text, 4000)}

Please respond in the following EXACT format (use the labels as shown):

CONTENT_GAPS:
[List the specific topics, sections, or information the competitor covers that my page does NOT. Be specific — e.g. "Competitor has a 'How to get to the stadium' section", "Competitor covers hospitality packages", etc.]

KEYWORDS_THEY_COVER:
[Comma-separated list of keywords/phrases present in the competitor's content but absent or underrepresented in mine]

MY_DEPTH_SCORE: [single integer 1-10]
COMPETITOR_DEPTH_SCORE: [single integer 1-10]

RECOMMENDATIONS:
[3-5 specific, actionable things I should add or improve on my page to outperform this competitor. Be concrete — e.g. "Add an FAQ section covering X", "Include a price comparison table", etc.]"""

    try:
        client = _client()
        message = client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        return _parse_comparison_response(raw)
    except Exception as exc:
        logger.error("Claude comparison failed for %s: %s", slug, exc)
        return {
            "content_gaps":             f"[Unavailable: {exc}]",
            "keywords_they_cover":      [],
            "my_depth_score":           0,
            "competitor_depth_score":   0,
            "recommendations":          f"[Unavailable: {exc}]",
            "raw_response":             str(exc),
        }


def _parse_comparison_response(raw: str) -> dict:
    """Extract structured fields from Claude's formatted response."""
    import re

    def extract_block(label: str) -> str:
        pattern = rf"{label}:\s*([\s\S]*?)(?=\n[A-Z_]+:|$)"
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else ""

    content_gaps   = extract_block("CONTENT_GAPS")
    keywords_raw   = extract_block("KEYWORDS_THEY_COVER")
    recs           = extract_block("RECOMMENDATIONS")

    # Parse scores
    def extract_score(label: str) -> int:
        m = re.search(rf"{label}:\s*(\d+)", raw)
        return int(m.group(1)) if m else 0

    my_score   = extract_score("MY_DEPTH_SCORE")
    comp_score = extract_score("COMPETITOR_DEPTH_SCORE")

    # Parse keywords list
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else []

    return {
        "content_gaps":           content_gaps,
        "keywords_they_cover":    keywords,
        "my_depth_score":         my_score,
        "competitor_depth_score": comp_score,
        "recommendations":        recs,
        "raw_response":           raw,
    }
