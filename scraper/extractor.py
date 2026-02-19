"""
HTML extraction: title, meta, headings, body text, internal links.
"""

import re
import logging
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Tags whose entire subtree we discard before reading body text
_STRIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "iframe", "svg", "button", "input", "select",
    "textarea", "figure", "figcaption",
}


def extract(html: str, page_url: str) -> dict:
    """
    Parse raw HTML and return a structured dict of content signals.

    Returns:
        title            (str)
        meta_description (str)
        h1               (str)   — first H1 found
        headings         (list)  — [{"level": "h2", "text": "..."}, ...]
        clean_text       (str)   — stripped body text
        word_count       (int)
        internal_links   (list)  — absolute same-domain href strings
    """
    soup = BeautifulSoup(html, "lxml")
    base_domain = urlparse(page_url).netloc

    # ── Title ─────────────────────────────────────────────────────────────────
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # ── Meta description ──────────────────────────────────────────────────────
    meta_desc = ""
    for tag in soup.find_all("meta"):
        if tag.get("name", "").lower() == "description":
            meta_desc = tag.get("content", "").strip()
            break

    # ── Headings ──────────────────────────────────────────────────────────────
    headings = []
    h1 = ""
    for level in ("h1", "h2", "h3", "h4"):
        for tag in soup.find_all(level):
            text = tag.get_text(separator=" ", strip=True)
            if not text:
                continue
            if level == "h1" and not h1:
                h1 = text
            headings.append({"level": level, "text": text})

    # ── Strip noisy elements before extracting body text ─────────────────────
    for tag_name in _STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body_tag = soup.find("body") or soup
    raw_text = body_tag.get_text(separator="\n", strip=True)

    # Collapse excessive whitespace / blank lines
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    clean_text = "\n".join(lines)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

    word_count = len(clean_text.split())

    # ── Internal links ────────────────────────────────────────────────────────
    internal_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc == base_domain:
            normalized = parsed._replace(fragment="").geturl()
            if normalized not in internal_links:
                internal_links.append(normalized)

    logger.debug(
        "Extracted from %s — %d words, %d headings, %d internal links",
        page_url, word_count, len(headings), len(internal_links),
    )

    return {
        "title":            title,
        "meta_description": meta_desc,
        "h1":               h1,
        "headings":         headings,
        "clean_text":       clean_text,
        "word_count":       word_count,
        "internal_links":   internal_links,
    }
