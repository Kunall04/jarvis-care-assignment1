"""
test_scraper.py — Test suite for the Boericke scraper.

Run unit tests only (no network):
    pytest test_scraper.py -v -m "not integration"

Run everything including live-network tests:
    pytest test_scraper.py -v

Run just the integration tests:
    pytest test_scraper.py -v -m integration
"""

import json
import os
import textwrap

import pytest
from bs4 import BeautifulSoup, Tag

from scraper import (
    _collect_text_from_to,
    _extract_name,
    _find_section_tags,
    _split_out_relationships,
    clean_text,
    load_existing,
    parse_remedy_links,
    save_output,
    scrape_remedy_page,
)

# ── helpers ────────────────────────────────────────────────────────────────────


def _body(html: str) -> Tag:
    """Parse HTML and return the <body> tag (or soup root)."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    assert isinstance(body, Tag), "No <body> tag found"
    return body


# ══════════════════════════════════════════════════════════════════════════════
# clean_text
# ══════════════════════════════════════════════════════════════════════════════


class TestCleanText:
    def test_collapses_multiple_spaces(self):
        assert clean_text("hello   world") == "hello world"

    def test_collapses_newlines(self):
        assert clean_text("hello\n\nworld") == "hello world"

    def test_collapses_tabs(self):
        assert clean_text("hello\t\tworld") == "hello world"

    def test_strips_leading_and_trailing(self):
        assert clean_text("  hello  ") == "hello"

    def test_empty_string(self):
        assert clean_text("") == ""

    def test_only_whitespace(self):
        assert clean_text("   \n\t   ") == ""

    def test_single_word(self):
        assert clean_text("hello") == "hello"

    def test_preserves_internal_punctuation(self):
        assert clean_text("hello, world!") == "hello, world!"


# ══════════════════════════════════════════════════════════════════════════════
# parse_remedy_links
# ══════════════════════════════════════════════════════════════════════════════


class TestParseRemedyLinks:
    def test_extracts_two_links(self):
        html = """
        <html><body>
        <a href="a/abies-c.htm">ABIES-C</a>
        <a href="a/acon.htm">ACON</a>
        </body></html>
        """
        links = parse_remedy_links(html, "A")
        assert len(links) == 2

    def test_correct_url_construction(self):
        html = '<html><body><a href="a/abies-c.htm">ABIES-C</a></body></html>'
        links = parse_remedy_links(html, "A")
        assert links[0]["url"] == "http://homeoint.org/books/boericmm/a/abies-c.htm"

    def test_correct_abbreviation_uppercased(self):
        html = '<html><body><a href="a/acon.htm">acon</a></body></html>'
        links = parse_remedy_links(html, "A")
        assert links[0]["abbreviation"] == "ACON"

    def test_correct_letter_field(self):
        html = '<html><body><a href="b/bell.htm">BELL</a></body></html>'
        links = parse_remedy_links(html, "B")
        assert links[0]["letter"] == "B"

    def test_ignores_external_links(self):
        html = '<html><body><a href="http://other.com/a/x.htm">X</a></body></html>'
        assert parse_remedy_links(html, "A") == []

    def test_ignores_wrong_letter_subdirectory(self):
        html = '<html><body><a href="b/bell.htm">BELL</a></body></html>'
        assert parse_remedy_links(html, "A") == []

    def test_deduplicates_same_url(self):
        html = """
        <html><body>
        <a href="a/acon.htm">ACON</a>
        <a href="a/acon.htm">ACON</a>
        </body></html>
        """
        assert len(parse_remedy_links(html, "A")) == 1

    def test_skips_empty_anchor_text(self):
        html = '<html><body><a href="a/acon.htm">   </a></body></html>'
        assert parse_remedy_links(html, "A") == []

    def test_returns_empty_when_no_links(self):
        assert parse_remedy_links("<html><body></body></html>", "A") == []

    def test_order_preserved(self):
        html = """
        <html><body>
        <a href="a/abies-c.htm">ABIES-C</a>
        <a href="a/abies-n.htm">ABIES-N</a>
        <a href="a/acon.htm">ACON</a>
        </body></html>
        """
        links = parse_remedy_links(html, "A")
        assert [lnk["abbreviation"] for lnk in links] == ["ABIES-C", "ABIES-N", "ACON"]


# ══════════════════════════════════════════════════════════════════════════════
# _extract_name
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractName:
    def test_basic_name_and_common_name(self):
        body = _body("<body><b>ABIES CANADENSIS</b><br>Hemlock Spruce</body>")
        full, common, name_b = _extract_name(body)
        assert full == "ABIES CANADENSIS"
        assert common == "Hemlock Spruce"
        assert name_b is not None

    def test_common_name_in_italic_sibling(self):
        body = _body("<body><b>ACONITUM NAPELLUS</b><br><i>Monkshood</i></body>")
        full, common, _ = _extract_name(body)
        assert full == "ACONITUM NAPELLUS"
        assert common == "Monkshood"

    def test_no_common_name_when_next_is_section(self):
        body = _body("<body><b>ABSINTHIUM</b><b>Head.--</b>Some head text.</body>")
        full, common, _ = _extract_name(body)
        assert full == "ABSINTHIUM"
        # The next non-br sibling IS a section heading (all-caps style check
        # fails on "Head.--" differently, but guard still applies)
        # common_name may be None or the section title — verify it's not the
        # general text
        assert common != "Some head text."

    def test_skips_site_boilerplate(self):
        body = _body(
            "<body><b>HOMOEOPATHIC MATERIA MEDICA</b><b>ACONITUM NAPELLUS</b></body>"
        )
        full, _, _ = _extract_name(body)
        assert full == "ACONITUM NAPELLUS"

    def test_returns_empty_when_no_bold(self):
        body = _body("<body><p>No bold tags here.</p></body>")
        full, common, name_b = _extract_name(body)
        assert full == ""
        assert common is None
        assert name_b is None

    def test_hyphenated_remedy_name(self):
        body = _body("<body><b>ABIES CANADENSIS-PINUS CANADENSIS</b><br>Hemlock</body>")
        full, _, _ = _extract_name(body)
        assert full == "ABIES CANADENSIS-PINUS CANADENSIS"

    def test_common_name_strips_leading_dashes(self):
        body = _body("<body><b>SOME REMEDY</b><br>-- Actual Name</body>")
        _, common, _ = _extract_name(body)
        assert common == "Actual Name"


# ══════════════════════════════════════════════════════════════════════════════
# _find_section_tags
# ══════════════════════════════════════════════════════════════════════════════


class TestFindSectionTags:
    def test_finds_three_sections(self):
        body = _body(
            "<body>"
            "<b>Head.--</b>Head text."
            "<b>Stomach.--</b>Stomach text."
            "<b>Dose.--</b>First potency."
            "</body>"
        )
        tags = _find_section_tags(body)
        assert [t[0] for t in tags] == ["Head", "Stomach", "Dose"]

    def test_ignores_remedy_name_bold(self):
        body = _body("<body><b>REMEDY NAME</b><b>Head.--</b>Text.</body>")
        tags = _find_section_tags(body)
        assert len(tags) == 1
        assert tags[0][0] == "Head"

    def test_handles_en_dash(self):
        """Headings may use – (en dash) instead of --."""
        body = _body("<body><b>Stomach.–</b>En-dash heading.</body>")
        tags = _find_section_tags(body)
        assert len(tags) == 1
        assert tags[0][0] == "Stomach"

    def test_handles_em_dash(self):
        body = _body("<body><b>Fever.—</b>Em-dash heading.</body>")
        tags = _find_section_tags(body)
        assert len(tags) == 1
        assert tags[0][0] == "Fever"

    def test_empty_body_returns_empty_list(self):
        assert _find_section_tags(_body("<body></body>")) == []

    def test_relationship_section_detected(self):
        body = _body("<body><b>Relationship.--</b>Compare: Bell.</body>")
        tags = _find_section_tags(body)
        assert tags[0][0] == "Relationship"

    def test_multiword_section_name(self):
        body = _body("<body><b>Urinary System.--</b>Scanty urine.</body>")
        tags = _find_section_tags(body)
        assert tags[0][0] == "Urinary System"


# ══════════════════════════════════════════════════════════════════════════════
# _collect_text_from_to
# ══════════════════════════════════════════════════════════════════════════════


class TestCollectTextFromTo:
    def test_collects_text_between_two_nodes(self):
        soup = BeautifulSoup(
            "<body><b id='s'>START</b> middle text <b id='e'>END</b></body>",
            "html.parser",
        )
        body = soup.find("body")
        assert isinstance(body, Tag)
        start = soup.find("b", id="s")
        end = soup.find("b", id="e")
        result = _collect_text_from_to(body, start, end)
        assert "middle text" in result
        assert "START" not in result
        assert "END" not in result

    def test_skips_direct_children_of_start(self):
        soup = BeautifulSoup(
            "<body><b id='s'>SKIP ME</b> keep this</body>",
            "html.parser",
        )
        body = soup.find("body")
        assert isinstance(body, Tag)
        start = soup.find("b", id="s")
        result = _collect_text_from_to(body, start, None)
        assert "SKIP ME" not in result
        assert "keep this" in result

    def test_none_start_collects_from_beginning(self):
        body = _body("<body>first <b>second</b></body>")
        result = _collect_text_from_to(body, None, None)
        assert "first" in result
        assert "second" in result

    def test_none_end_collects_to_end(self):
        soup = BeautifulSoup(
            "<body><b id='s'>X</b>alpha beta gamma</body>",
            "html.parser",
        )
        body = soup.find("body")
        assert isinstance(body, Tag)
        start = soup.find("b", id="s")
        result = _collect_text_from_to(body, start, None)
        assert "alpha beta gamma" in result


# ══════════════════════════════════════════════════════════════════════════════
# _split_out_relationships
# ══════════════════════════════════════════════════════════════════════════════


class TestSplitOutRelationships:
    def test_splits_on_complementary(self):
        text = "Worse wet weather. Complementary: Rhus, Carbo."
        sec, rel = _split_out_relationships(text)
        assert "Worse wet weather" in sec
        assert rel is not None
        assert "Complementary: Rhus, Carbo" in rel

    def test_splits_on_compare(self):
        text = "Dry heat. Compare: Bell, Cham."
        sec, rel = _split_out_relationships(text)
        assert "Dry heat" in sec
        assert rel is not None
        assert "Compare:" in rel

    def test_splits_on_antidotes(self):
        text = "Section content. Antidotes: Camph, Ipec."
        sec, rel = _split_out_relationships(text)
        assert "Section content" in sec
        assert rel is not None
        assert "Antidotes:" in rel

    def test_no_keyword_returns_original(self):
        text = "Just plain section text with no relationship keywords."
        sec, rel = _split_out_relationships(text)
        assert sec == text
        assert rel is None

    def test_empty_string(self):
        sec, rel = _split_out_relationships("")
        assert sec == ""
        assert rel is None

    def test_keyword_at_start(self):
        text = "Complementary: Sulph, Calc."
        sec, rel = _split_out_relationships(text)
        assert sec == ""
        assert rel is not None
        assert "Complementary:" in rel

    def test_inimical_keyword(self):
        text = "Better warm. Inimical: Acon."
        _, rel = _split_out_relationships(text)
        assert rel is not None
        assert "Inimical:" in rel


# ══════════════════════════════════════════════════════════════════════════════
# save_output / load_existing
# ══════════════════════════════════════════════════════════════════════════════


class TestSaveLoadOutput:
    def test_round_trip(self, tmp_path):
        fp = str(tmp_path / "out.json")
        remedies = [{"abbreviation": "TST", "source_url": "http://x.com/t.htm"}]
        save_output(remedies, fp)
        loaded = load_existing(fp)
        assert "http://x.com/t.htm" in loaded
        assert loaded["http://x.com/t.htm"]["abbreviation"] == "TST"

    def test_load_missing_file_returns_empty(self, tmp_path):
        assert load_existing(str(tmp_path / "none.json")) == {}

    def test_load_invalid_json_returns_empty(self, tmp_path):
        fp = tmp_path / "bad.json"
        fp.write_text("{not valid json}")
        assert load_existing(str(fp)) == {}

    def test_save_creates_file(self, tmp_path):
        fp = str(tmp_path / "new.json")
        save_output([], fp)
        assert os.path.exists(fp)

    def test_saved_file_is_valid_json_array(self, tmp_path):
        fp = str(tmp_path / "out.json")
        remedies = [{"abbreviation": "TST", "source_url": "http://x.com/t.htm"}]
        save_output(remedies, fp)
        with open(fp) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert data[0]["abbreviation"] == "TST"

    def test_overwrites_existing_file(self, tmp_path):
        fp = str(tmp_path / "out.json")
        save_output([{"source_url": "http://a.com/1.htm"}], fp)
        save_output([{"source_url": "http://b.com/2.htm"}], fp)
        loaded = load_existing(fp)
        assert len(loaded) == 1
        assert "http://b.com/2.htm" in loaded

    def test_multiple_remedies_keyed_by_url(self, tmp_path):
        fp = str(tmp_path / "out.json")
        remedies = [
            {"source_url": "http://x.com/a.htm", "abbreviation": "A"},
            {"source_url": "http://x.com/b.htm", "abbreviation": "B"},
        ]
        save_output(remedies, fp)
        loaded = load_existing(fp)
        assert len(loaded) == 2
        assert loaded["http://x.com/a.htm"]["abbreviation"] == "A"


# ══════════════════════════════════════════════════════════════════════════════
# Full pipeline with mock HTML
# ══════════════════════════════════════════════════════════════════════════════

# Minimal mock that mimics the real page structure
_MOCK_ABIES_C = textwrap.dedent("""\
    <html><head><title>Abies Canadensis</title></head>
    <body>
    <a href="/">Home</a>
    <center>
      <b>HOMOEOPATHIC MATERIA MEDICA</b> by William BOERICKE, M.D.
      Presented by M\u00e9di-T
    </center>
    <p>
      <b>ABIES CANADENSIS-PINUS CANADENSIS</b><br>
      Hemlock Spruce
    </p>
    <p>
      Mucous membranes are affected by Abies can and gastric symptoms are most marked.
    </p>
    <p><b>Head.--</b>Feels light-headed, tipsy. Irritable.</p>
    <p><b>Stomach.--</b>Canine hunger with torpid liver.</p>
    <p><b>Female.--</b>Uterine displacements. Sore feeling at fundus.</p>
    <p><b>Fever.--</b>Cold shivering, as if blood were ice-water.</p>
    <p><b>Dose.--</b>First to third potency.</p>
    <p>** Copyright \u00a9 M\u00e9di-T 1999**</p>
    </body></html>
""")

_MOCK_ACON = textwrap.dedent("""\
    <html><body>
    <b>ACONITUM NAPELLUS</b><br>Monkshood
    <p>A state of fear, anxiety. Physical and mental restlessness.</p>
    <p><b>Mind.--</b>Great fear, anxiety, and worry.</p>
    <p><b>Head.--</b>Fullness; heavy, pulsating.</p>
    <p><b>Dose.--</b>Sixth potency for sensory affections.</p>
    <p><b>Relationship.--</b>Acids, wine and coffee modify its action.
    Complementary: Coffea; Sulph. Compare: Bell, Cham.</p>
    </body></html>
""")

_MOCK_ARS = textwrap.dedent("""\
    <html><body>
    <b>ARSENICUM ALBUM</b><br>Arsenious Acid-Arsenic Trioxide
    <p>A profoundly acting remedy. Great exhaustion after slightest exertion.</p>
    <p><b>Mind.--</b>Great anguish and restlessness.</p>
    <p><b>Modalities.--</b>Worse wet weather, after midnight.
    Complementary: Rhus; Carbo; Phos. Antidotes: Opium; Carbo.
    Compare: Phos, China, Verat.</p>
    <p><b>Dose.--</b>Third to thirtieth potency.</p>
    </body></html>
""")


class TestFullPipelineMockHTML:
    """Tests that exercise scrape_remedy_page with mocked HTTP responses."""

    def _mock_scrape(self, html: str, url: str, letter: str, abbr: str) -> dict:
        """Patch fetch_page to return *html* and call scrape_remedy_page."""
        import unittest.mock as mock

        with mock.patch("scraper.fetch_page", return_value=html):
            result = scrape_remedy_page(url, letter, abbr)
        assert result is not None, f"scrape_remedy_page returned None for {url}"
        return result

    # ── ABIES-C mock ──────────────────────────────────────────────────────────

    def test_abies_c_abbreviation(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["abbreviation"] == "ABIES-C"

    def test_abies_c_full_name(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["full_name"] == "ABIES CANADENSIS-PINUS CANADENSIS"

    def test_abies_c_common_name(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["common_name"] == "Hemlock Spruce"

    def test_abies_c_letter(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["letter"] == "A"

    def test_abies_c_source_url(self):
        url = "http://x.com/a/abies-c.htm"
        r = self._mock_scrape(_MOCK_ABIES_C, url, "A", "ABIES-C")
        assert r["source_url"] == url

    def test_abies_c_general_not_empty(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["general"]
        assert "Mucous membranes" in r["general"]

    def test_abies_c_general_excludes_common_name(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert not r["general"].startswith("Hemlock Spruce")

    def test_abies_c_sections_is_dict(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert isinstance(r["sections"], dict)

    def test_abies_c_has_head_section(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert "Head" in r["sections"]
        assert "light-headed" in r["sections"]["Head"]

    def test_abies_c_has_stomach_section(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert "Stomach" in r["sections"]

    def test_abies_c_has_dose_section(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert "Dose" in r["sections"]
        assert "potency" in r["sections"]["Dose"]

    def test_abies_c_copyright_stripped_from_sections(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        for text in r["sections"].values():
            assert "Copyright" not in text

    def test_abies_c_relationships_null(self):
        r = self._mock_scrape(
            _MOCK_ABIES_C, "http://x.com/a/abies-c.htm", "A", "ABIES-C"
        )
        assert r["relationships"] is None

    # ── ACON mock (has explicit Relationship section) ─────────────────────────

    def test_acon_full_name(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert r["full_name"] == "ACONITUM NAPELLUS"

    def test_acon_common_name(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert r["common_name"] == "Monkshood"

    def test_acon_has_mind_section(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert "Mind" in r["sections"]

    def test_acon_relationships_not_null(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert r["relationships"] is not None

    def test_acon_relationships_contains_complementary(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert "Complementary" in r["relationships"]

    def test_acon_relationship_not_in_sections(self):
        r = self._mock_scrape(_MOCK_ACON, "http://x.com/a/acon.htm", "A", "ACON")
        assert "Relationship" not in r["sections"]
        assert "Relationships" not in r["sections"]

    # ── ARS mock (relationships embedded in Modalities, no explicit heading) ──

    def test_ars_full_name(self):
        r = self._mock_scrape(_MOCK_ARS, "http://x.com/a/ars.htm", "A", "ARS")
        assert r["full_name"] == "ARSENICUM ALBUM"

    def test_ars_common_name(self):
        r = self._mock_scrape(_MOCK_ARS, "http://x.com/a/ars.htm", "A", "ARS")
        assert r["common_name"] is not None
        assert "Arsenic" in r["common_name"] or "Arsenious" in r["common_name"]

    def test_ars_relationships_extracted_from_modalities(self):
        r = self._mock_scrape(_MOCK_ARS, "http://x.com/a/ars.htm", "A", "ARS")
        assert r["relationships"] is not None
        assert "Complementary" in r["relationships"]

    def test_ars_modalities_text_cleaned(self):
        r = self._mock_scrape(_MOCK_ARS, "http://x.com/a/ars.htm", "A", "ARS")
        # The modalities section text should NOT contain "Complementary:"
        if "Modalities" in r["sections"]:
            assert "Complementary:" not in r["sections"]["Modalities"]

    # ── fetch failure ─────────────────────────────────────────────────────────

    def test_returns_none_on_fetch_failure(self):
        import unittest.mock as mock

        with mock.patch("scraper.fetch_page", return_value=None):
            result = scrape_remedy_page("http://x.com/a/bad.htm", "A", "BAD")
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# Output schema validation
# ══════════════════════════════════════════════════════════════════════════════


class TestOutputSchema:
    """Verify the output dict always contains all required fields."""

    REQUIRED_FIELDS = {
        "abbreviation",
        "full_name",
        "common_name",
        "source_url",
        "letter",
        "general",
        "sections",
        "relationships",
    }

    def _mock_scrape(self, html: str) -> dict:
        import unittest.mock as mock

        with mock.patch("scraper.fetch_page", return_value=html):
            result = scrape_remedy_page("http://x.com/a/t.htm", "A", "TST")
        assert result is not None
        return result

    def test_all_required_fields_present(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert self.REQUIRED_FIELDS.issubset(r.keys())

    def test_abbreviation_is_string(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert isinstance(r["abbreviation"], str)

    def test_full_name_is_string(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert isinstance(r["full_name"], str)

    def test_common_name_is_string_or_none(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert r["common_name"] is None or isinstance(r["common_name"], str)

    def test_sections_is_dict(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert isinstance(r["sections"], dict)

    def test_sections_values_are_strings(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        for v in r["sections"].values():
            assert isinstance(v, str)

    def test_relationships_is_string_or_none(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert r["relationships"] is None or isinstance(r["relationships"], str)

    def test_letter_is_single_uppercase(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert len(r["letter"]) == 1
        assert r["letter"].isupper()

    def test_source_url_starts_with_http(self):
        r = self._mock_scrape(_MOCK_ABIES_C)
        assert r["source_url"].startswith("http")


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests  (require live network — run with -m integration)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Live-network tests against homeoint.org.  Skip in CI / offline mode."""

    def test_fetch_letter_a_index(self):
        from scraper import fetch_letter_index

        html = fetch_letter_index("A")
        assert html is not None
        assert "ABIES" in html

    def test_parse_letter_a_has_many_links(self):
        from scraper import fetch_letter_index

        html = fetch_letter_index("A")
        assert html is not None
        links = parse_remedy_links(html, "A")
        # Letter A has 100+ remedies
        assert len(links) > 50

    def test_scrape_abies_c_live(self):
        """Scrape the real ABIES-C page and validate key fields."""
        result = scrape_remedy_page(
            "http://homeoint.org/books/boericmm/a/abies-c.htm", "A", "ABIES-C"
        )
        assert result is not None
        assert "ABIES" in result["full_name"]
        assert result["common_name"] == "Hemlock Spruce"
        assert result["letter"] == "A"
        assert result["general"]
        assert "Head" in result["sections"]
        assert "Stomach" in result["sections"]
        assert "Dose" in result["sections"]
        assert result["relationships"] is None

    def test_scrape_acon_live(self):
        """Scrape ACONITUM NAPELLUS and validate it has many sections."""
        result = scrape_remedy_page(
            "http://homeoint.org/books/boericmm/a/acon.htm", "A", "ACON"
        )
        assert result is not None
        assert result["full_name"] == "ACONITUM NAPELLUS"
        assert result["common_name"] == "Monkshood"
        assert "Mind" in result["sections"]
        assert "Head" in result["sections"]
        assert "Dose" in result["sections"]
        # ACON has a Relationship section
        assert result["relationships"] is not None

    def test_scrape_ars_live(self):
        """Scrape ARSENICUM ALBUM and verify embedded relationships are found."""
        result = scrape_remedy_page(
            "http://homeoint.org/books/boericmm/a/ars.htm", "A", "ARS"
        )
        assert result is not None
        assert result["full_name"] == "ARSENICUM ALBUM"
        assert result["relationships"] is not None
        assert "Complementary" in result["relationships"]

    def test_scrape_sample_five_match_schema(self):
        """
        Scrape the same 5 remedies in sample_output.json and verify
        each result has all required schema fields and non-empty core text.
        """
        sample_urls = [
            ("http://homeoint.org/books/boericmm/a/abies-c.htm", "A", "ABIES-C"),
            ("http://homeoint.org/books/boericmm/a/acon.htm", "A", "ACON"),
            ("http://homeoint.org/books/boericmm/a/arn.htm", "A", "ARN"),
            ("http://homeoint.org/books/boericmm/a/ars.htm", "A", "ARS"),
            ("http://homeoint.org/books/boericmm/a/aur.htm", "A", "AUR"),
        ]
        required = {
            "abbreviation",
            "full_name",
            "common_name",
            "source_url",
            "letter",
            "general",
            "sections",
            "relationships",
        }
        for url, letter, abbr in sample_urls:
            r = scrape_remedy_page(url, letter, abbr)
            assert r is not None, f"Failed to scrape {url}"
            assert required.issubset(r.keys()), f"Missing fields in {abbr}"
            assert r["full_name"], f"Empty full_name for {abbr}"
            assert r["general"], f"Empty general for {abbr}"
            assert isinstance(r["sections"], dict), f"sections not dict for {abbr}"
            import time as _time

            _time.sleep(0.7)  # be polite
