"""
Claude-powered analysis:
  1. Summarise a detected diff (what changed, why it matters)
  2. Evidence-based side-by-side page comparison across 8 scored dimensions

Every score is either:
  - Measured directly in Python (D1, D4, D6, D7, D8) — no AI opinion
  - Backed by an exact quote from the page (D2, D3, D5) — AI must cite evidence
"""

import json
import logging
import os
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TEXT_CHARS = 12_000

# Dimension weights for the overall depth score
_WEIGHTS = {
    "question_coverage":     0.25,
    "faq_coverage":          0.20,
    "heading_structure":     0.15,
    "word_count":            0.15,
    "transactional_clarity": 0.10,
    "trust_signals":         0.05,
    "freshness":             0.05,
    "internal_linking":      0.05,
}

# Slugs that are competitions rather than team pages (affects D3 questions)
_COMPETITION_SLUGS = {"fa-cup", "world-cup", "champions-league", "europa-league", "euro"}

_TEAM_QUESTIONS = [
    "Where is the stadium and how do I get there?",
    "How much do tickets cost?",
    "How do I actually buy tickets?",
    "When are the next fixtures?",
    "Are there hospitality or premium options?",
    "What should I know as a visitor or away fan?",
    "Is this site trustworthy?",
]

_COMPETITION_QUESTIONS = [
    "What rounds or stages are available?",
    "Which teams are involved?",
    "When are the matches?",
    "Where are the venues?",
    "How do I buy?",
    "What is the price range?",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key)


def _truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[... truncated at {max_chars} chars ...]"


def _get_questions(slug: str) -> list:
    return _COMPETITION_QUESTIONS if slug in _COMPETITION_SLUGS else _TEAM_QUESTIONS


def _find_quote(text: str, keyword: str, context: int = 80) -> Optional[str]:
    """Return a short surrounding quote when keyword is found in text."""
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return None
    start = max(0, idx - 20)
    end = min(len(text), idx + context)
    return text[start:end].strip()


# ── 1. Diff summary (unchanged) ───────────────────────────────────────────────

def summarise_diff(
    page_url: str,
    page_slug: str,
    old_text: str,
    new_text: str,
    added_text: str,
    removed_text: str,
    change_pct: float,
) -> str:
    """Ask Claude to summarise what changed and assess the strategic intent."""
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
3. Your assessment of the strategic intent — are they targeting new keywords,
   improving trust signals, adding urgency, etc.?

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


# ── 2. Python-measured dimensions ─────────────────────────────────────────────

def _dim_word_count(wc: int) -> dict:
    """D1 — hard measurement, no AI opinion."""
    if wc < 300:    score = 2
    elif wc < 600:  score = 4
    elif wc < 900:  score = 6
    elif wc < 1200: score = 7
    elif wc < 1800: score = 8
    else:           score = 10
    return {"score": score, "evidence": f"{wc:,} words in extracted body text"}


def _dim_headings(headings: list) -> dict:
    """D2 — count measured in Python; Claude adds diversity verdict."""
    h2s = [h for h in headings if h.get("level") == "h2"]
    h3s = [h for h in headings if h.get("level") == "h3"]
    n2, n3 = len(h2s), len(h3s)

    if n2 == 0:    base = 1
    elif n2 <= 2:  base = 4
    elif n2 <= 4:  base = 6
    elif h3s:      base = 9
    else:          base = 7

    h2_texts = [h["text"] for h in h2s]
    evidence = f"{n2} H2s, {n3} H3s"
    if h2_texts:
        evidence += f". H2s: {', '.join(h2_texts[:5])}"
    return {"base_score": base, "h2_texts": h2_texts, "h3_count": n3, "evidence": evidence}


def _dim_trust_signals(text: str) -> dict:
    """D4 — scan for trust signal categories; quote the found text."""
    text_lower = text.lower()
    categories = {
        "guarantee":  ["100% guarantee", "money back guarantee", "guaranteed", "100%"],
        "reviews":    ["trustpilot", "reviews", "rated", "stars", "rating"],
        "experience": ["years experience", "since 19", "since 20", "established in"],
        "security":   ["secure payment", "ssl", "secure checkout", "safe and secure", "encrypted"],
        "official":   ["official", "authorised", "authorized", "licensed seller", "official partner"],
    }
    found = {}
    for category, keywords in categories.items():
        for kw in keywords:
            quote = _find_quote(text, kw)
            if quote:
                found[category] = quote
                break
    score = min(10, len(found) * 2)
    return {"score": score, "found_categories": found}


def _dim_freshness(text: str) -> dict:
    """D6 — scan for freshness signals; quote what was found."""
    signals = []

    m = re.search(r"\b(202[5-9](?:/\d{2})?|2025-2[0-9])\b", text)
    if m:
        signals.append(("season", f"season/year reference: '{m.group()}'"))

    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+202[5-9]\b",
        text,
    )
    if m:
        signals.append(("fixtures", f"fixture date: '{m.group()[:60]}'"))
    elif re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", text):
        signals.append(("fixtures", "fixture dates present"))

    m = re.search(r"\b(current form|recent results?|latest results?|this season)\b", text, re.I)
    if m:
        signals.append(("form", f"form/results language: '{m.group()[:50]}'"))

    m = re.search(r"\b(upcoming|latest|new for|new season)\b", text, re.I)
    if m:
        signals.append(("language", f"freshness language: '{m.group()}'"))

    score = min(10, int(len(signals) * 2.5))
    return {"score": score, "signals": signals}


def _dim_faq(text: str, headings: list) -> dict:
    """D7 — detect FAQ sections and question-format headings."""
    text_lower = text.lower()
    has_explicit = any(
        kw in text_lower
        for kw in ["frequently asked", "faq", "common questions", "people also ask"]
    )

    question_starts = (
        "how ", "what ", "where ", "when ", "can ", "do ", "is ",
        "are ", "why ", "which ", "will ",
    )
    question_headings = [
        h["text"] for h in headings
        if any(h["text"].lower().startswith(q) for q in question_starts)
    ]
    count = len(question_headings)

    if has_explicit and count >= 5: score = 10
    elif has_explicit or count >= 3: score = 7
    elif count >= 1:                 score = 4
    else:                            score = 0

    if has_explicit and count:
        evidence = f"Explicit FAQ section with {count} question headings"
    elif has_explicit:
        evidence = "Explicit FAQ section found (no question-format headings detected)"
    elif count:
        evidence = f"{count} question-format heading(s) found"
    else:
        evidence = "No FAQ section or question-format headings found"

    return {"score": score, "has_explicit_faq": has_explicit,
            "question_headings": question_headings, "evidence": evidence}


def _dim_internal_links(links: list) -> dict:
    """D8 — count directly from stored internal_links list."""
    count = len(links)
    if count >= 10:  score = 10
    elif count >= 6: score = 7
    elif count >= 3: score = 5
    else:            score = 2
    return {"score": score, "count": count, "evidence": f"{count} internal links found"}


# ── 3. AI-judged dimensions (one API call) ────────────────────────────────────

def _ai_dimensions(
    slug: str,
    my_url: str,
    my_text: str,
    my_h2s: list,
    competitor_url: str,
    competitor_text: str,
    comp_h2s: list,
) -> Optional[dict]:
    """
    Single Claude call covering D2 (heading diversity), D3 (question coverage
    with quotes), D5 (transactional clarity with quotes), plus content gaps,
    keywords, and recommendations.

    Returns parsed dict or None if Claude is unavailable.
    """
    questions = _get_questions(slug)

    prompt = f"""You are a content analyst for a football ticket marketplace.
Analyze two pages for the topic "{slug}".

=== MY PAGE ({my_url}) ===
H2 headings: {json.dumps(my_h2s)}
Content:
{_truncate(my_text, 5000)}

=== COMPETITOR PAGE ({competitor_url}) ===
H2 headings: {json.dumps(comp_h2s)}
Content:
{_truncate(competitor_text, 5000)}

Respond with ONLY valid JSON — no markdown fences, no explanation. Use this exact structure:

{{
  "heading_diversity": {{
    "mine": {{
      "score_adjustment": 0,
      "verdict": "One sentence: do the H2s cover meaningfully different subtopics, or are they repetitive?"
    }},
    "competitor": {{
      "score_adjustment": 0,
      "verdict": "One sentence verdict."
    }}
  }},
  "question_coverage": {{
    "mine": {{
      "answers": {{
        "QUESTION_TEXT": {{"answered": true, "quote": "exact short quote from MY PAGE or null"}}
      }},
      "score": 0
    }},
    "competitor": {{
      "answers": {{
        "QUESTION_TEXT": {{"answered": true, "quote": "exact short quote from COMPETITOR or null"}}
      }},
      "score": 0
    }}
  }},
  "transactional_clarity": {{
    "mine": {{
      "cta":             {{"found": false, "quote": null}},
      "price_range":     {{"found": false, "quote": null}},
      "delivery_method": {{"found": false, "quote": null}},
      "booking_process": {{"found": false, "quote": null}},
      "score": 0
    }},
    "competitor": {{
      "cta":             {{"found": false, "quote": null}},
      "price_range":     {{"found": false, "quote": null}},
      "delivery_method": {{"found": false, "quote": null}},
      "booking_process": {{"found": false, "quote": null}},
      "score": 0
    }}
  }},
  "content_gaps": "Specific topics/sections competitor covers that my page does not.",
  "keywords_they_cover": ["keyword1", "keyword2"],
  "recommendations": "3-5 concrete, actionable improvements for my page."
}}

Rules — read carefully:
- Replace every QUESTION_TEXT key with the actual question from this list: {json.dumps(questions)}
- heading_diversity.score_adjustment: +2 if H2s cover distinctly varied subtopics, 0 if adequate, -2 if repetitive
- question_coverage score = round((answered_count / {len(questions)}) * 10)
- transactional_clarity score = each of 4 elements found adds 2.5 points (max 10)
- For "quote": copy the EXACT text from the page (max 100 chars). Use null if not found.
- NEVER invent quotes. Only quote text that is literally present in the page content above."""

    raw = ""
    try:
        client = _client()
        message = client.messages.create(
            model=MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if Claude adds them despite instructions
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned invalid JSON for %s: %s", slug, exc)
        logger.debug("Raw Claude response: %.300s", raw)
        return None
    except Exception as exc:
        logger.error("Claude AI dimensions call failed for %s: %s", slug, exc)
        return None


# ── 4. Evidence formatting helpers ────────────────────────────────────────────

def _fmt_question_answers(answers: dict) -> str:
    if not answers:
        return "No question coverage data available."
    lines = []
    for q, a in answers.items():
        tick = "✓" if a.get("answered") else "✗"
        quote = f': "{a["quote"]}"' if a.get("quote") else ""
        lines.append(f"{tick} {q}{quote}")
    return "\n".join(lines)


def _fmt_trust(found: dict) -> str:
    if not found:
        return "No trust signals detected."
    return "; ".join(f'{cat}: "{q[:80]}"' for cat, q in found.items())


def _fmt_transactional(detail: dict) -> str:
    if not detail:
        return "No transactional data available."
    out = []
    for el in ("cta", "price_range", "delivery_method", "booking_process"):
        d = detail.get(el, {})
        if not isinstance(d, dict):
            continue
        label = el.replace("_", " ").title()
        if d.get("found"):
            out.append(f'✓ {label}: "{d.get("quote", "")}"')
        else:
            out.append(f"✗ {label}: not found")
    return "\n".join(out)


def _reco_word_count(my_wc: int, comp_wc: int) -> str:
    if my_wc >= comp_wc:
        return f"Word count ({my_wc:,}) is already competitive."
    gap = comp_wc - my_wc
    return (
        f"Add ~{gap:,} words to match competitor. "
        "Focus on informational sections: stadium guide, travel, FAQs, history."
    )


def _reco_questions(answers: dict) -> str:
    if not answers:
        return "Ensure your page answers all key buyer questions."
    missing = [q for q, a in answers.items() if not a.get("answered")]
    if not missing:
        return "All key buyer questions are answered."
    return "Add content answering: " + "; ".join(missing)


def _reco_trust(found: dict) -> str:
    all_cats = ["guarantee", "reviews", "experience", "security", "official"]
    missing = [c for c in all_cats if c not in found]
    if not missing:
        return "Trust signals are comprehensive."
    return "Add missing trust signals: " + ", ".join(missing)


def _reco_transactional(detail: dict) -> str:
    if not detail:
        return "Ensure page has clear CTA, price range, delivery info, and booking process."
    missing = []
    for el in ("cta", "price_range", "delivery_method", "booking_process"):
        d = detail.get(el, {})
        if isinstance(d, dict) and not d.get("found"):
            missing.append(el.replace("_", " "))
    if not missing:
        return "All transactional elements are present."
    return "Add missing transactional elements: " + ", ".join(missing)


def _reco_faq(slug: str) -> str:
    questions = _get_questions(slug)
    q_lines = "\n".join(f"  - {q}" for q in questions[:5])
    return f"Add a FAQ section with at least 5 questions, including:\n{q_lines}"


# ── 5. Weighted score ─────────────────────────────────────────────────────────

def _weighted_avg(scores: dict) -> float:
    return round(sum(_WEIGHTS[k] * scores[k] for k in _WEIGHTS if k in scores), 1)


# ── 6. Main compare_pages ─────────────────────────────────────────────────────

def compare_pages(
    slug: str,
    my_url: str,
    my_text: str,
    my_headings: list,
    my_word_count: int,
    my_internal_links: list,
    competitor_url: str,
    competitor_text: str,
    competitor_headings: list,
    competitor_word_count: int,
    competitor_internal_links: list,
) -> dict:
    """
    Evidence-based depth comparison across 8 dimensions.

    Returns dict with:
      dimensions              — list of per-dimension dicts with scores + evidence
      my_depth_score          — rounded weighted overall (int, for backward compat)
      competitor_depth_score  — rounded weighted overall (int, for backward compat)
      my_depth_score_weighted / competitor_depth_score_weighted — float
      my_dimension_scores / competitor_dimension_scores — raw dict
      content_gaps, keywords_they_cover, recommendations — from AI
    """

    # ── Python-measured dimensions ────────────────────────────────────────────
    d1_my   = _dim_word_count(my_word_count)
    d1_comp = _dim_word_count(competitor_word_count)

    d2_my_raw   = _dim_headings(my_headings)
    d2_comp_raw = _dim_headings(competitor_headings)

    d4_my   = _dim_trust_signals(my_text)
    d4_comp = _dim_trust_signals(competitor_text)

    d6_my   = _dim_freshness(my_text)
    d6_comp = _dim_freshness(competitor_text)

    d7_my   = _dim_faq(my_text, my_headings)
    d7_comp = _dim_faq(competitor_text, competitor_headings)

    d8_my   = _dim_internal_links(my_internal_links)
    d8_comp = _dim_internal_links(competitor_internal_links)

    # ── AI-judged dimensions ──────────────────────────────────────────────────
    ai = _ai_dimensions(
        slug=slug,
        my_url=my_url,
        my_text=my_text,
        my_h2s=d2_my_raw["h2_texts"],
        competitor_url=competitor_url,
        competitor_text=competitor_text,
        comp_h2s=d2_comp_raw["h2_texts"],
    )

    # ── D2: base score ± Claude diversity adjustment ──────────────────────────
    if ai:
        hd = ai.get("heading_diversity", {})
        my_d2_adj       = hd.get("mine", {}).get("score_adjustment", 0)
        comp_d2_adj     = hd.get("competitor", {}).get("score_adjustment", 0)
        my_d2_verdict   = hd.get("mine", {}).get("verdict", "")
        comp_d2_verdict = hd.get("competitor", {}).get("verdict", "")
    else:
        my_d2_adj = comp_d2_adj = 0
        my_d2_verdict = comp_d2_verdict = "AI analysis unavailable"

    d2_score_my   = max(1, min(10, d2_my_raw["base_score"]   + my_d2_adj))
    d2_score_comp = max(1, min(10, d2_comp_raw["base_score"] + comp_d2_adj))

    # ── D3, D5, content fields from AI ───────────────────────────────────────
    if ai:
        qc          = ai.get("question_coverage", {})
        d3_my       = qc.get("mine",        {})
        d3_comp     = qc.get("competitor",  {})
        d3_score_my   = d3_my.get("score",   0)
        d3_score_comp = d3_comp.get("score", 0)
        d3_my_ans     = d3_my.get("answers",   {})
        d3_comp_ans   = d3_comp.get("answers", {})

        tc            = ai.get("transactional_clarity", {})
        d5_my         = tc.get("mine",       {})
        d5_comp       = tc.get("competitor", {})
        d5_score_my   = d5_my.get("score",   0)
        d5_score_comp = d5_comp.get("score", 0)

        content_gaps        = ai.get("content_gaps", "")
        keywords_they_cover = ai.get("keywords_they_cover", [])
        recommendations     = ai.get("recommendations", "")
    else:
        d3_score_my = d3_score_comp = 0
        d3_my_ans = d3_comp_ans = {}
        d5_score_my = d5_score_comp = 0
        d5_my = d5_comp = {}
        content_gaps        = "[AI analysis unavailable — check ANTHROPIC_API_KEY and credits]"
        keywords_they_cover = []
        recommendations     = "[AI analysis unavailable]"

    # ── Build per-dimension output ────────────────────────────────────────────
    dimensions = [
        {
            "dimension":            "Word Count Adequacy",
            "score_mine":           d1_my["score"],
            "score_competitor":     d1_comp["score"],
            "gap":                  d1_comp["score"] - d1_my["score"],
            "my_evidence":          d1_my["evidence"],
            "competitor_evidence":  d1_comp["evidence"],
            "recommendation":       _reco_word_count(my_word_count, competitor_word_count),
        },
        {
            "dimension":            "Heading Structure",
            "score_mine":           d2_score_my,
            "score_competitor":     d2_score_comp,
            "gap":                  d2_score_comp - d2_score_my,
            "my_evidence":          d2_my_raw["evidence"] + (f" — {my_d2_verdict}" if my_d2_verdict else ""),
            "competitor_evidence":  d2_comp_raw["evidence"] + (f" — {comp_d2_verdict}" if comp_d2_verdict else ""),
            "recommendation": (
                "Add more H2s covering distinct subtopics; use H3s for sub-sections."
                if d2_score_my < d2_score_comp
                else "Heading structure is competitive."
            ),
        },
        {
            "dimension":            "Question Coverage",
            "score_mine":           d3_score_my,
            "score_competitor":     d3_score_comp,
            "gap":                  d3_score_comp - d3_score_my,
            "my_evidence":          _fmt_question_answers(d3_my_ans),
            "competitor_evidence":  _fmt_question_answers(d3_comp_ans),
            "recommendation":       _reco_questions(d3_my_ans),
        },
        {
            "dimension":            "Trust Signals",
            "score_mine":           d4_my["score"],
            "score_competitor":     d4_comp["score"],
            "gap":                  d4_comp["score"] - d4_my["score"],
            "my_evidence":          _fmt_trust(d4_my["found_categories"]),
            "competitor_evidence":  _fmt_trust(d4_comp["found_categories"]),
            "recommendation":       _reco_trust(d4_my["found_categories"]),
        },
        {
            "dimension":            "Transactional Clarity",
            "score_mine":           d5_score_my,
            "score_competitor":     d5_score_comp,
            "gap":                  d5_score_comp - d5_score_my,
            "my_evidence":          _fmt_transactional(d5_my),
            "competitor_evidence":  _fmt_transactional(d5_comp),
            "recommendation":       _reco_transactional(d5_my),
        },
        {
            "dimension":            "Freshness Signals",
            "score_mine":           d6_my["score"],
            "score_competitor":     d6_comp["score"],
            "gap":                  d6_comp["score"] - d6_my["score"],
            "my_evidence":          (
                "; ".join(s[1] for s in d6_my["signals"])
                if d6_my["signals"] else "No freshness signals detected."
            ),
            "competitor_evidence":  (
                "; ".join(s[1] for s in d6_comp["signals"])
                if d6_comp["signals"] else "No freshness signals detected."
            ),
            "recommendation": (
                "Add current season year, upcoming fixture dates, and 'latest/upcoming' language."
            ),
        },
        {
            "dimension":            "FAQ Coverage",
            "score_mine":           d7_my["score"],
            "score_competitor":     d7_comp["score"],
            "gap":                  d7_comp["score"] - d7_my["score"],
            "my_evidence":          d7_my["evidence"] + (
                f": {', '.join(d7_my['question_headings'][:3])}"
                if d7_my["question_headings"] else ""
            ),
            "competitor_evidence":  d7_comp["evidence"] + (
                f": {', '.join(d7_comp['question_headings'][:3])}"
                if d7_comp["question_headings"] else ""
            ),
            "recommendation":       _reco_faq(slug),
        },
        {
            "dimension":            "Internal Linking",
            "score_mine":           d8_my["score"],
            "score_competitor":     d8_comp["score"],
            "gap":                  d8_comp["score"] - d8_my["score"],
            "my_evidence":          d8_my["evidence"],
            "competitor_evidence":  d8_comp["evidence"],
            "recommendation": (
                f"Add more internal links to related pages. "
                f"Currently {d8_my['count']} — target 10+ with descriptive anchor text."
            ),
        },
    ]

    # ── Weighted overall scores ───────────────────────────────────────────────
    my_scores = {
        "word_count":            d1_my["score"],
        "heading_structure":     d2_score_my,
        "question_coverage":     d3_score_my,
        "trust_signals":         d4_my["score"],
        "transactional_clarity": d5_score_my,
        "freshness":             d6_my["score"],
        "faq_coverage":          d7_my["score"],
        "internal_linking":      d8_my["score"],
    }
    comp_scores = {
        "word_count":            d1_comp["score"],
        "heading_structure":     d2_score_comp,
        "question_coverage":     d3_score_comp,
        "trust_signals":         d4_comp["score"],
        "transactional_clarity": d5_score_comp,
        "freshness":             d6_comp["score"],
        "faq_coverage":          d7_comp["score"],
        "internal_linking":      d8_comp["score"],
    }

    my_weighted   = _weighted_avg(my_scores)
    comp_weighted = _weighted_avg(comp_scores)

    return {
        # Backward-compatible int scores
        "my_depth_score":                    round(my_weighted),
        "competitor_depth_score":            round(comp_weighted),
        # Full detail
        "my_depth_score_weighted":           my_weighted,
        "competitor_depth_score_weighted":   comp_weighted,
        "my_dimension_scores":               my_scores,
        "competitor_dimension_scores":       comp_scores,
        "dimensions":                        dimensions,
        # Legacy fields (still used by dashboard + reports)
        "content_gaps":                      content_gaps,
        "keywords_they_cover":              keywords_they_cover,
        "recommendations":                   recommendations,
    }
