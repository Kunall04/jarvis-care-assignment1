#!/usr/bin/env python3
"""
scraper.py — Boericke's Homoeopathic Materia Medica Scraper

Crawls http://homeoint.org/books/boericmm/ (26 letter index pages),
follows every remedy link, and writes boericke_remedies.json.

Usage:
    python scraper.py               # Full A–Z scrape
    python scraper.py --letter A    # Single-letter test
    python scraper.py --letter A --limit 5   # First 5 remedies of A
    python scraper.py --upload      # Scrape then push to MongoDB
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# ── Constants ──────────────────────────────────────────────────────────────────

BASE_URL = "http://homeoint.org/books/boericmm/"
OUTPUT_FILE = "boericke_remedies.json"
FAILED_FILE = "failed_urls.txt"
ALL_LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DELAY_RANGE = (0.5, 1.0)  # seconds between HTTP requests

# Known top-level section names; used to detect the end of a Relationship
# sub-entry zone (e.g. "Aconitine.--" inside Relationship should not become
# its own top-level section).
MAIN_SECTIONS: frozenset[str] = frozenset(
    {
        "Mind",
        "Head",
        "Eyes",
        "Ears",
        "Nose",
        "Face",
        "Mouth",
        "Throat",
        "Stomach",
        "Abdomen",
        "Rectum",
        "Stool",
        "Urine",
        "Urinary",
        "Urinary System",
        "Male",
        "Female",
        "Respiratory",
        "Heart",
        "Circulation",
        "Back",
        "Neck",
        "Extremities",
        "Limbs",
        "Sleep",
        "Skin",
        "Fever",
        "Temperature",
        "Modalities",
        "Dose",
        "Relationship",
        "Relationships",
        "Generalities",
        "Glands",
        "Nervous System",
        "Constitutional",
    }
)

# Regex: bold tag that is a section heading  → "Head.--", "Dose.--" etc.
_SECTION_B_RE = re.compile(r"^([A-Z][A-Za-z\s\/\-]+?)\.[-–—]+\s*$")

# Regex: relationship keywords followed by a colon inside section text
_REL_KW_RE = re.compile(
    r"\b(Complementary|Antidote[sd]?|Compare[sd]?|Inimical|Incompatible|Similar)\s*:",
    re.IGNORECASE,
)

# Site-header phrases that appear in <b> tags but are NOT remedy names
_BOILERPLATE: frozenset[str] = frozenset(
    {
        "MATERIA MEDICA",
        "HOMOEOPATHIC",
        "HOMOEOPATHIC MATERIA MEDICA",
        "BOERICKE",
        "MEDI-T",
        "MÉDI-T",
        "HOME",
        "MAIN",
        "COPYRIGHT",
    }
)

log = logging.getLogger("boericke")


# ── HTTP helpers ───────────────────────────────────────────────────────────────


def fetch_page(url: str, retries: int = 3) -> Optional[str]:
    """
    GET *url* and return the response body as a string.

    Retries up to *retries* times with exponential back-off on failure.
    Returns ``None`` if every attempt fails.

    Args:
        url:     The URL to fetch.
        retries: Number of attempts before giving up.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BoerickeScraperBot/1.0)"}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt < retries - 1:
                wait = 2**attempt
                log.warning(
                    "  [RETRY %d/%d] %s — waiting %ds…",
                    attempt + 1,
                    retries - 1,
                    url,
                    wait,
                )
                time.sleep(wait)
            else:
                log.error("FAIL  %s  →  %s", url, exc)
    return None


def fetch_letter_index(letter: str) -> Optional[str]:
    """
    Fetch the letter-index page for *letter* (e.g. ``a.htm`` for ``"A"``).

    Returns raw HTML or ``None`` on failure.

    Args:
        letter: Single uppercase letter ``"A"``–``"Z"``.
    """
    return fetch_page(f"{BASE_URL}{letter.lower()}.htm")


# ── Index parsing ──────────────────────────────────────────────────────────────


def parse_remedy_links(html: str, letter: str) -> list[dict]:
    """
    Extract remedy abbreviations and page URLs from a letter index page.

    Only ``<a href>`` tags whose ``href`` matches the pattern
    ``<letter>/<slug>.htm`` (e.g. ``a/abies-c.htm``) are kept.
    Duplicates are silently dropped.

    Args:
        html:   Raw HTML of the letter index page.
        letter: Current letter (``"A"``–``"Z"``).

    Returns:
        List of ``{"abbreviation", "url", "letter"}`` dicts in document order.
    """
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(rf"^{re.escape(letter.lower())}/[\w-]+\.htm$", re.IGNORECASE)
    seen: set[str] = set()
    out: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if pat.match(href):
            abbr = a.get_text(strip=True).upper()
            full_url = BASE_URL + href
            if full_url not in seen and abbr:
                seen.add(full_url)
                out.append(
                    {
                        "abbreviation": abbr,
                        "url": full_url,
                        "letter": letter.upper(),
                    }
                )
    return out


# ── Text utilities ─────────────────────────────────────────────────────────────


def clean_text(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip edges."""
    return re.sub(r"\s+", " ", text).strip()


def _split_out_relationships(text: str) -> tuple[str, Optional[str]]:
    """
    If *text* contains embedded relationship keywords (``Complementary:``,
    ``Compare:``, ``Antidotes:`` …), split and return
    ``(section_text, relationship_text)``.

    Some pages (e.g. Arsenicum) have no explicit ``Relationship.--`` heading
    but embed ``Complementary:`` / ``Compare:`` paragraphs inside the last
    clinical section.  This helper surfaces that text.

    Returns:
        ``(original_text, None)`` when no keywords are found.
    """
    m = _REL_KW_RE.search(text)
    if m:
        return clean_text(text[: m.start()]), clean_text(text[m.start() :])
    return text, None


# ── Remedy-page parsing ────────────────────────────────────────────────────────


def _is_boilerplate(text: str) -> bool:
    """Return ``True`` if *text* is a known site-header phrase."""
    return any(ph in text for ph in _BOILERPLATE)


def _extract_name(body: Tag) -> tuple[str, Optional[str], Optional[Tag]]:
    """
    Locate the remedy-name ``<b>`` tag and extract the optional common name.

    The remedy name is the first all-uppercase ``<b>`` that is neither a
    section heading (contains ``.--``) nor site boilerplate.
    The common name is the first non-empty sibling text / tag following the
    name ``<b>`` that is not itself all-uppercase.

    Args:
        body: The ``<body>`` Tag (or soup root) to search.

    Returns:
        ``(full_name, common_name_or_None, name_b_tag_or_None)``
    """
    full_name: str = ""
    common_name: Optional[str] = None
    name_b: Optional[Tag] = None

    for b in body.find_all("b"):
        raw = b.get_text()
        # Real pages pack name + common name in the same <b> tag separated
        # by newlines: "REMEDY NAME\nCommon Name"
        lines = [ln.strip() for ln in re.split(r"[\n\r]+", raw) if ln.strip()]
        if not lines:
            continue

        candidate = lines[0]
        if (
            len(candidate) > 4
            and re.match(r"^[A-Z][A-Z0-9\s\-\/\.\(\)]+$", candidate)
            and ".--" not in candidate
            and "-----" not in candidate
            and not _is_boilerplate(candidate)
        ):
            full_name = clean_text(candidate)
            name_b = b
            # Common name may be on the next line of the same <b> tag
            if len(lines) > 1:
                second = lines[1]
                if second and not re.match(r"^[A-Z][A-Z\s\-]+$", second):
                    common_name = clean_text(second)
            break

    if name_b is None:
        return "", None, None

    # If common name was not found inside the <b> tag, walk forward siblings
    if common_name is None:
        for sib in name_b.next_siblings:
            if isinstance(sib, NavigableString):
                t = sib.strip()
                if t and len(t) > 1:
                    common_name = clean_text(t)
                    break
            elif isinstance(sib, Tag):
                if sib.name in ("br", "hr"):
                    continue  # skip bare line-breaks, keep looking
                t = sib.get_text().strip()
                if t and len(t) > 1 and not re.match(r"^[A-Z][A-Z\s\-]+$", t):
                    common_name = clean_text(t)
                break  # stop after first non-break tag regardless

    # Strip stray leading punctuation / whitespace
    if common_name:
        common_name = re.sub(r"^[\s\-\*\|]+", "", common_name).strip()
        if len(common_name) < 2:
            common_name = None

    return full_name, common_name, name_b


def _find_section_tags(
    body: Tag,
) -> list[tuple[str, NavigableString]]:
    """
    Return all section-heading text nodes in document order.

    A heading matches ``SectionName.--`` (mixed-case name, literal period,
    one or more hyphens/dashes).  The search targets individual
    ``NavigableString`` nodes rather than their container tags, which
    handles three real-world page patterns:

    * ``<b>Head.--</b>text``           — heading inside ``<b>``
    * ``<b><p>Head.--</b>text``        — heading in ``<p>`` nested inside ``<b>``
    * ``<b></b><p>Mind.--text</p>``    — empty ``<b>``, heading bare in ``<p>``

    Args:
        body: The ``<body>`` Tag to search.

    Returns:
        Ordered list of ``(section_name, NavigableString)`` tuples.
    """
    result: list[tuple[str, NavigableString]] = []
    seen: set[int] = set()

    for node in body.descendants:
        if not isinstance(node, NavigableString):
            continue
        text = str(node).strip()
        m = _SECTION_B_RE.match(text)
        if m and id(node) not in seen:
            seen.add(id(node))
            result.append((m.group(1).strip(), node))

    return result


# A section boundary can be a Tag (remedy-name <b>) or a NavigableString
# (section-heading text node).  Union covers both call sites.
_NodeOrNone = Optional["Tag | NavigableString"]


def _collect_text_from_to(
    body: Tag,
    start_node: _NodeOrNone,
    end_node: _NodeOrNone,
) -> str:
    """
    Gather ``NavigableString`` text strictly between *start_node* and
    *end_node* in depth-first document order.

    Direct-child text of *start_node* is skipped (it is the tag's own
    label, e.g. ``"Head.--"`` inside the section heading ``<b>``).

    Args:
        body:       Root element to traverse.
        start_node: Begin collecting *after* this node; ``None`` = from start.
        end_node:   Stop *before* this node; ``None`` = until end of *body*.

    Returns:
        Clean, single-spaced string of collected text.
    """
    collecting = start_node is None
    parts: list[str] = []

    for node in body.descendants:
        if node is start_node:
            collecting = True
            continue
        if end_node is not None and node is end_node:
            break
        if not collecting:
            continue
        if isinstance(node, NavigableString):
            # Skip ALL text that lives inside start_node (at any depth).
            # Some pages wrap the heading text in a <p> inside the <b>, so
            # checking only node.parent is insufficient.
            if start_node is not None:
                p = node.parent
                inside = False
                while p is not None:
                    if p is start_node:
                        inside = True
                        break
                    p = p.parent
                if inside:
                    continue
            t = str(node).strip()
            if t:
                parts.append(t)

    return clean_text(" ".join(parts))


def _strip_footer(text: str) -> str:
    """Remove copyright / navigation boilerplate that appears at page bottom."""
    text = re.sub(r"\*?\*?\s*Copyright\s*[©Â©]?.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bH\.I\.\s*$", "", text)
    return clean_text(text)


def scrape_remedy_page(
    url: str,
    letter: str,
    abbreviation: str,
) -> Optional[dict]:
    """
    Fetch and parse one remedy page, returning a structured dict.

    Extracts full name, common name, general description, per-organ
    sections, and cross-reference relationships.

    Args:
        url:          Full URL of the remedy page.
        letter:       Uppercase letter for this remedy (``"A"``–``"Z"``).
        abbreviation: Abbreviation key (e.g. ``"ABIES-C"``).

    Returns:
        Dict matching the output schema, or ``None`` if the page could
        not be fetched.
    """
    html = fetch_page(url)
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    body: Tag = soup.find("body") or soup  # type: ignore[assignment]

    # ── 1. Remedy name & common name ─────────────────────────────────────────
    full_name, common_name, name_b = _extract_name(body)

    # ── 2. Section heading tags ───────────────────────────────────────────────
    section_tags = _find_section_tags(body)
    first_b = section_tags[0][1] if section_tags else None

    # ── 3. General description (between name tag and first section) ───────────
    raw_general = _collect_text_from_to(body, name_b, first_b)

    # Strip the common name if it lands at the very start of general text.
    # Use a while-loop in case the name appears multiple times at the start
    # (e.g. PHOS where "Phosphorus" echoes twice in the collected text).
    general = raw_general
    if common_name:
        pattern = re.compile(r"^" + re.escape(common_name) + r"\s*")
        while pattern.match(general):
            general = pattern.sub("", general).strip()

    # Strip site-header boilerplate that may bleed into the general text
    general = re.sub(
        r"(Home\s+)?HOM[ÉE]OPATHIC\s+MATERIA\s+MEDICA.*?(?:Medi-T|Médi-T)\s*",
        "",
        general,
        flags=re.IGNORECASE | re.DOTALL,
    )
    general = clean_text(general)

    # ── 4. Sections & relationships ───────────────────────────────────────────
    sections: dict[str, str] = {}
    relationship_parts: list[str] = []
    in_rel_zone = False

    for idx, (sec_name, sec_b) in enumerate(section_tags):
        next_b = section_tags[idx + 1][1] if idx + 1 < len(section_tags) else None
        raw_text = _collect_text_from_to(body, sec_b, next_b)
        sec_text = _strip_footer(raw_text)

        low = sec_name.lower()

        # — Explicit Relationship / Relationships section ——————————————————————
        if "relationship" in low:
            in_rel_zone = True
            relationship_parts.append(sec_text)
            continue

        # — Sub-entry within a Relationship zone (e.g. "Aconitine.--") ————————
        if in_rel_zone and sec_name not in MAIN_SECTIONS:
            if sec_text:
                relationship_parts.append(f"{sec_name}.-- {sec_text}")
            continue

        # — Back to regular sections ───────────────────────────────────────────
        in_rel_zone = False

        # Mine embedded relationship keywords (Complementary:, Compare: …)
        sec_text, rel_extra = _split_out_relationships(sec_text)
        if rel_extra:
            relationship_parts.append(rel_extra)

        if sec_text:
            sections[sec_name] = sec_text

    relationships = clean_text(" ".join(relationship_parts)) or None

    return {
        "abbreviation": abbreviation,
        "full_name": full_name,
        "common_name": common_name,
        "source_url": url,
        "letter": letter.upper(),
        "general": general,
        "sections": sections,
        "relationships": relationships,
    }


# ── Output helpers ─────────────────────────────────────────────────────────────


def load_existing(filepath: str) -> dict[str, dict]:
    """
    Load a previously saved JSON output file.

    Returns a ``{source_url: remedy_dict}`` index for O(1) deduplication.
    Returns ``{}`` if the file is missing or corrupt.

    Args:
        filepath: Path to the JSON output file.
    """
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, encoding="utf-8") as fh:
            data: list[dict] = json.load(fh)
        return {r["source_url"]: r for r in data}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def save_output(remedies: list[dict], filepath: str) -> None:
    """
    Serialise *remedies* as a pretty-printed JSON array to *filepath*.

    Args:
        remedies: List of remedy dicts to write.
        filepath: Destination path (created or overwritten).
    """
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(remedies, fh, indent=2, ensure_ascii=False)


def _log_failed(url: str) -> None:
    """Append *url* to ``failed_urls.txt``."""
    with open(FAILED_FILE, "a", encoding="utf-8") as fh:
        fh.write(url + "\n")


# ── Optional: MongoDB upload ───────────────────────────────────────────────────


def upload_to_mongodb(
    remedies: list[dict],
    uri: str = "mongodb://localhost:27017",
) -> None:
    """
    Push *remedies* into the ``jarvis.remedies`` MongoDB collection.

    Uses ``source_url`` as the upsert key so re-running never duplicates
    records.  Requires ``pymongo`` (``pip install pymongo``).

    Args:
        remedies: Remedy list to upload.
        uri:      MongoDB connection URI (defaults to localhost).
    """
    try:
        from pymongo import MongoClient, UpdateOne  # type: ignore
    except ImportError:
        log.error("pymongo not installed — run: pip install pymongo")
        return

    client = MongoClient(uri)
    col = client["jarvis"]["remedies"]
    ops = [
        UpdateOne({"source_url": r["source_url"]}, {"$set": r}, upsert=True)
        for r in remedies
    ]
    if ops:
        res = col.bulk_write(ops)
        log.info(
            "MongoDB upload: %d upserted, %d modified.",
            res.upserted_count,
            res.modified_count,
        )
    client.close()


# ── Main orchestrator ──────────────────────────────────────────────────────────


def main() -> None:
    """
    Orchestrate a full (or partial) A–Z scrape of Boericke's Materia Medica.

    Behaviour:
    * Skips URLs already present in ``boericke_remedies.json`` (resumable).
    * Sleeps 0.5–1 s between requests to respect the server.
    * Saves progress every 10 newly scraped remedies and after each letter.
    * Appends failed URLs to ``failed_urls.txt`` and continues without
      crashing.
    """
    parser = argparse.ArgumentParser(
        description="Scrape Boericke's Homoeopathic Materia Medica (homeoint.org)"
    )
    parser.add_argument(
        "--letter",
        "-l",
        metavar="X",
        help="Scrape only this letter (e.g. A).  Useful for testing.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        metavar="N",
        help="Max remedies to scrape per letter (useful for quick tests).",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="After scraping, upload to a local MongoDB jarvis.remedies collection.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    letters = [args.letter.upper()] if args.letter else ALL_LETTERS
    existing = load_existing(OUTPUT_FILE)
    all_remedies: list[dict] = list(existing.values())
    total_new = 0

    log.info("Boericke Scraper — %d remedies already cached.\n", len(all_remedies))

    for letter in letters:
        log.info(
            "── Letter %s ────────────────────────────────────────────────", letter
        )

        html = fetch_letter_index(letter)
        if html is None:
            log.error("  Could not fetch index for letter %s — skipping.", letter)
            continue

        links = parse_remedy_links(html, letter)
        if not links:
            log.warning("  No remedy links found for letter %s.", letter)
            continue

        total = len(links)
        if args.limit:
            links = links[: args.limit]

        letter_new = 0
        for i, link in enumerate(links, start=1):
            url = link["url"]

            if url in existing:
                log.info(
                    "  [%s] %d/%d  SKIP (cached)  %s",
                    letter,
                    i,
                    total,
                    link["abbreviation"],
                )
                continue

            remedy = scrape_remedy_page(url, letter, link["abbreviation"])
            time.sleep(random.uniform(*DELAY_RANGE))

            if remedy:
                all_remedies.append(remedy)
                existing[url] = remedy
                total_new += 1
                letter_new += 1
                log.info(
                    "  [%s] Scraped %d/%d — %s",
                    letter,
                    i,
                    total,
                    remedy["full_name"],
                )
            else:
                log.error("  [%s] FAILED  %d/%d — %s", letter, i, total, url)
                _log_failed(url)

            # Incremental save every 10 new remedies
            if total_new and total_new % 10 == 0:
                save_output(all_remedies, OUTPUT_FILE)

        save_output(all_remedies, OUTPUT_FILE)
        log.info("  Letter %s done — %d new remedies scraped.\n", letter, letter_new)

    save_output(all_remedies, OUTPUT_FILE)
    log.info(
        "✓ Complete — %d total remedies, %d newly scraped → %s",
        len(all_remedies),
        total_new,
        OUTPUT_FILE,
    )

    if args.upload:
        upload_to_mongodb(all_remedies)


if __name__ == "__main__":
    main()
