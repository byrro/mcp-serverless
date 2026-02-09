# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pytest",
#   "requests",
#   "beautifulsoup4",
#   "markdownify",
# ]
# ///
"""Unit tests for the AWS Lambda documentation scraper."""

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from scraper import (
    ScraperError,
    clean_html,
    flatten_toc,
    format_duration,
    generate_index,
    get_local_scraped_at,
    needs_update,
    parse_page_html,
    scrape_page,
    validate_page_html,
    validate_toc,
)
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

VALID_TOC = {
    "contents": [
        {
            "title": "What is AWS Lambda?",
            "href": "welcome.html",
            "contents": [
                {
                    "title": "How it works",
                    "href": "concepts-basics.html",
                    "contents": [
                        {"title": "Programming model", "href": "foundation-progmodel.html"},
                    ],
                },
            ],
        },
        {"title": "Create your first function", "href": "getting-started.html"},
    ]
}


def _make_page_html(body_content: str, has_main_col: bool = True) -> str:
    """Build a minimal AWS-style doc page HTML."""
    if has_main_col:
        main = f'<div id="main-col-body">{body_content}</div>'
    else:
        main = f"<div>{body_content}</div>"
    return f"<html><body>{main}</body></html>"


VALID_PAGE_HTML = _make_page_html(
    '<h1 class="topictitle" id="welcome">What is AWS Lambda?</h1>'
    "<p>AWS Lambda is a compute service that lets you run code without "
    "provisioning or managing servers. Lambda runs your code on a "
    "high-availability compute infrastructure.</p>"
    "<h2>How Lambda works</h2>"
    "<p>Lambda is event-driven. You write functions that respond to events.</p>"
    '<awsdocs-thumb-feedback>Did this page help you?</awsdocs-thumb-feedback>'
    '<div><p>Did this page help you?</p></div>'
    '<div><p>Thanks for letting us know!</p></div>'
)


def _mock_response(
    status_code: int = 200,
    text: str = "",
    json_data: dict | None = None,
    headers: dict | None = None,
    content_type: str = "text/html",
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"Content-Type": content_type}
    if headers:
        resp.headers.update(headers)
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = json.JSONDecodeError("", "", 0)
    return resp


# ===================================================================
# validate_toc
# ===================================================================


class TestValidateToc:
    """Tests for TOC JSON structure validation."""

    def test_valid_toc(self):
        """Valid TOC should pass without error."""
        validate_toc(VALID_TOC)  # should not raise

    def test_toc_not_a_dict(self):
        """TOC that is a list instead of a dict should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc([{"title": "foo", "href": "bar.html"}])

    def test_toc_missing_contents_key(self):
        """TOC dict without 'contents' key should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"pages": []})

    def test_toc_contents_empty_list(self):
        """TOC with empty 'contents' list should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"contents": []})

    def test_toc_contents_not_a_list(self):
        """TOC with 'contents' as a string should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"contents": "not a list"})

    def test_toc_first_entry_missing_title(self):
        """First TOC entry without 'title' should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"contents": [{"href": "foo.html"}]})

    def test_toc_first_entry_missing_href(self):
        """First TOC entry without 'href' should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"contents": [{"title": "Foo"}]})

    def test_toc_first_entry_not_a_dict(self):
        """First TOC entry as a string should fail."""
        with pytest.raises(SystemExit, match="2"):
            validate_toc({"contents": ["not a dict"]})


# ===================================================================
# flatten_toc
# ===================================================================


class TestFlattenToc:
    """Tests for TOC flattening."""

    def test_flat_structure(self):
        contents = [
            {"title": "A", "href": "a.html"},
            {"title": "B", "href": "b.html"},
        ]
        assert flatten_toc(contents) == [("A", "a.html"), ("B", "b.html")]

    def test_nested_structure(self):
        pages = flatten_toc(VALID_TOC["contents"])
        assert len(pages) == 4
        assert pages[0] == ("What is AWS Lambda?", "welcome.html")
        assert pages[1] == ("How it works", "concepts-basics.html")
        assert pages[2] == ("Programming model", "foundation-progmodel.html")
        assert pages[3] == ("Create your first function", "getting-started.html")

    def test_empty_contents(self):
        assert flatten_toc([]) == []

    def test_items_without_href_are_skipped(self):
        """Items that only have 'contents' (section headers) are skipped."""
        contents = [
            {
                "title": "Section",
                "contents": [{"title": "Page", "href": "page.html"}],
            }
        ]
        assert flatten_toc(contents) == [("Page", "page.html")]


# ===================================================================
# fetch_toc
# ===================================================================


class TestFetchToc:
    """Tests for TOC fetching (HTTP layer)."""

    @patch("scraper.requests.get")
    def test_network_error(self, mock_get):
        """Network error should fail with exit code 2."""
        mock_get.side_effect = requests.ConnectionError("DNS failure")
        with pytest.raises(SystemExit, match="2"):
            from scraper import fetch_toc
            fetch_toc()

    @patch("scraper.requests.get")
    def test_non_200_status(self, mock_get):
        """Non-200 response should fail with exit code 2."""
        mock_get.return_value = _mock_response(status_code=403)
        with pytest.raises(SystemExit, match="2"):
            from scraper import fetch_toc
            fetch_toc()

    @patch("scraper.requests.get")
    def test_invalid_json(self, mock_get):
        """Response with non-JSON body should fail with exit code 2."""
        resp = _mock_response(status_code=200, text="<html>not json</html>")
        resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
        mock_get.return_value = resp
        with pytest.raises(SystemExit, match="2"):
            from scraper import fetch_toc
            fetch_toc()

    @patch("scraper.requests.get")
    def test_valid_response(self, mock_get):
        """Valid JSON response should return parsed data."""
        mock_get.return_value = _mock_response(
            status_code=200, json_data=VALID_TOC
        )
        from scraper import fetch_toc
        result = fetch_toc()
        assert result == VALID_TOC


# ===================================================================
# get_local_scraped_at
# ===================================================================


class TestGetLocalScrapedAt:
    """Tests for reading scraped_at from local markdown files."""

    def test_valid_frontmatter(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text(
            "---\ntitle: Test\nsource: http://x\nscraped_at: 2026-02-01 12:00:00\n---\n\n# Test\n"
        )
        result = get_local_scraped_at(f)
        assert result == datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_missing_file(self, tmp_path):
        assert get_local_scraped_at(tmp_path / "nope.md") is None

    def test_no_scraped_at_field(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\ntitle: Test\n---\n\n# Test\n")
        assert get_local_scraped_at(f) is None

    def test_corrupt_file(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("this is not markdown frontmatter at all\n" * 20)
        assert get_local_scraped_at(f) is None

    def test_empty_file(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("")
        assert get_local_scraped_at(f) is None


# ===================================================================
# needs_update
# ===================================================================


class TestNeedsUpdate:
    """Tests for freshness checking."""

    def test_no_local_file(self, tmp_path):
        """Missing local file always needs update."""
        assert needs_update(tmp_path / "missing.md", "http://example.com") is True

    @patch("scraper.requests.head")
    def test_fresh_file(self, mock_head, tmp_path):
        """File scraped after Last-Modified should be skipped."""
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-09 10:00:00\n---\n")
        mock_head.return_value = _mock_response(
            headers={"Last-Modified": "Sun, 08 Feb 2026 10:00:00 GMT"}
        )
        assert needs_update(f, "http://example.com") is False

    @patch("scraper.requests.head")
    def test_stale_file(self, mock_head, tmp_path):
        """File scraped before Last-Modified should need update."""
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-01 10:00:00\n---\n")
        mock_head.return_value = _mock_response(
            headers={"Last-Modified": "Mon, 09 Feb 2026 10:00:00 GMT"}
        )
        assert needs_update(f, "http://example.com") is True

    @patch("scraper.requests.head")
    def test_no_last_modified_header(self, mock_head, tmp_path):
        """No Last-Modified header → conservatively re-scrape."""
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-09 10:00:00\n---\n")
        mock_head.return_value = _mock_response(headers={})
        # headers dict won't have Last-Modified
        mock_head.return_value.headers = {}
        assert needs_update(f, "http://example.com") is True

    @patch("scraper.requests.head")
    def test_head_request_network_error(self, mock_head, tmp_path):
        """Network error on HEAD → conservatively re-scrape."""
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-09 10:00:00\n---\n")
        mock_head.side_effect = requests.ConnectionError("timeout")
        assert needs_update(f, "http://example.com") is True


# ===================================================================
# validate_page_html
# ===================================================================


class TestValidatePageHtml:
    """Tests for HTML structure validation."""

    def test_valid_page(self):
        soup = BeautifulSoup(VALID_PAGE_HTML, "html.parser")
        main = validate_page_html(soup, "http://example.com/page.html")
        assert main is not None
        assert main.get("id") == "main-col-body"

    def test_missing_main_col_body(self):
        """Page without main-col-body should exit with code 2."""
        html = "<html><body><div id='other'>content</div></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        with pytest.raises(SystemExit, match="2"):
            validate_page_html(soup, "http://example.com/page.html")

    def test_missing_main_col_body_with_alternative_hint(self):
        """Should mention alternative containers if found."""
        html = '<html><body><div id="main-content">stuff</div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        with pytest.raises(SystemExit, match="2") as exc_info:
            validate_page_html(soup, "http://example.com/page.html")

    def test_main_col_body_nearly_empty(self):
        """Content div with barely any text should exit with code 2."""
        html = '<html><body><div id="main-col-body">Hi</div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        with pytest.raises(SystemExit, match="2"):
            validate_page_html(soup, "http://example.com/page.html")

    def test_main_col_body_with_enough_text(self):
        """Content div with sufficient text should pass."""
        long_text = "AWS Lambda documentation content. " * 10
        html = f'<html><body><div id="main-col-body"><p>{long_text}</p></div></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        main = validate_page_html(soup, "http://example.com/page.html")
        assert main is not None


# ===================================================================
# clean_html
# ===================================================================


class TestCleanHtml:
    """Tests for HTML cleaning before markdown conversion."""

    def _soup_main(self, inner_html: str) -> BeautifulSoup:
        html = f'<div id="main-col-body">{inner_html}</div>'
        soup = BeautifulSoup(html, "html.parser")
        return soup.find("div", id="main-col-body")

    def test_removes_topictitle_h1(self):
        main = self._soup_main('<h1 class="topictitle">Title</h1><p>Body</p>')
        clean_html(main)
        assert main.find("h1") is None
        assert "Body" in main.get_text()

    def test_removes_aws_web_components(self):
        main = self._soup_main(
            "<awsdocs-language-banner>x</awsdocs-language-banner>"
            "<awsdocs-thumb-feedback>y</awsdocs-thumb-feedback>"
            "<p>Real content here</p>"
        )
        clean_html(main)
        assert main.find("awsdocs-language-banner") is None
        assert main.find("awsdocs-thumb-feedback") is None
        assert "Real content" in main.get_text()

    def test_removes_feedback_blocks(self):
        main = self._soup_main(
            "<p>Good stuff</p>"
            "<div><p>Did this page help you?</p></div>"
            "<div><p>Thanks for letting us know we're doing a good job!</p></div>"
        )
        clean_html(main)
        text = main.get_text()
        assert "Did this page help" not in text
        assert "Thanks for letting us know" not in text
        assert "Good stuff" in text

    def test_removes_empty_divs(self):
        main = self._soup_main(
            "<div>   </div><div></div><p>Content</p>"
        )
        clean_html(main)
        # Only the main div and <p> should remain
        divs = main.find_all("div")
        for div in divs:
            if div.get("id") != "main-col-body":
                assert div.get_text(strip=True) != ""

    def test_preserves_divs_with_code(self):
        main = self._soup_main(
            "<div><pre><code>print('hello')</code></pre></div>"
        )
        clean_html(main)
        assert main.find("pre") is not None
        assert "print('hello')" in main.get_text()


# ===================================================================
# parse_page_html
# ===================================================================


class TestParsePageHtml:
    """Tests for full HTML → Markdown conversion."""

    def test_valid_page_produces_frontmatter_and_content(self):
        result = parse_page_html(
            VALID_PAGE_HTML,
            "https://docs.aws.amazon.com/lambda/latest/dg/welcome.html",
            "What is AWS Lambda?",
        )
        assert result.startswith("---\n")
        assert "title: What is AWS Lambda?" in result
        assert "source: https://docs.aws.amazon.com/lambda/latest/dg/welcome.html" in result
        assert "scraped_at:" in result
        assert "# What is AWS Lambda?" in result
        # Content should be present
        assert "compute service" in result
        # Chrome should be removed
        assert "Did this page help" not in result
        assert "Thanks for letting us know" not in result

    def test_no_duplicate_title(self):
        """The h1.topictitle from HTML should be removed (we add our own)."""
        result = parse_page_html(
            VALID_PAGE_HTML,
            "http://example.com/page.html",
            "What is AWS Lambda?",
        )
        # Should appear exactly once (our frontmatter title), not twice
        assert result.count("# What is AWS Lambda?") == 1

    def test_missing_main_col_body_exits(self):
        """Page without main-col-body should trigger _fail → SystemExit(2)."""
        html = "<html><body><p>No main-col-body here</p></body></html>"
        with pytest.raises(SystemExit, match="2"):
            parse_page_html(html, "http://example.com/page.html", "Test")

    def test_empty_content_after_cleaning_raises(self):
        """If cleaning leaves nothing, should raise ScraperError."""
        html = _make_page_html(
            '<h1 class="topictitle">Title</h1>'
            "<awsdocs-thumb-feedback>feedback</awsdocs-thumb-feedback>"
            # Only chrome, no real content — but we need to pass validate_page_html
            # first, which requires 50+ chars. So embed invisible text.
        )
        # This will fail at validate_page_html (too little text), which is correct.
        # Let's make a page that passes validation but produces empty markdown:
        html = _make_page_html(
            '<h1 class="topictitle">Title</h1>'
            "<div>                                                              </div>"
            "<p>" + " " * 100 + "</p>"
        )
        # validate_page_html checks get_text(strip=True), spaces will be stripped.
        # So this hits the "nearly empty" check. Let's use a smarter approach:
        # non-breaking spaces in text that get stripped by markdownify
        html = _make_page_html(
            '<h1 class="topictitle">Title</h1>'
            "<p>This has enough text to pass validation but will be interesting.</p>"
        )
        # This actually won't be empty. Let's test the path differently by
        # mocking markdownify to return empty:
        with patch("scraper.markdownify", return_value="   \n\n  "):
            with pytest.raises(ScraperError, match="empty output"):
                parse_page_html(html, "http://example.com/page.html", "Test")

    def test_dynamic_content_page_exits(self):
        """Page with main-col-body but only JS-loaded placeholder should fail."""
        html = _make_page_html("<span>x</span>")  # < 50 chars of text
        with pytest.raises(SystemExit, match="2"):
            parse_page_html(html, "http://example.com/page.html", "Test")


# ===================================================================
# scrape_page (HTTP layer)
# ===================================================================


class TestScrapePage:
    """Tests for the HTTP wrapper around parse_page_html."""

    @patch("scraper.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(ScraperError, match="Network error"):
            scrape_page("http://example.com/page.html", "Test")

    @patch("scraper.requests.get")
    def test_404(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404)
        with pytest.raises(ScraperError, match="404"):
            scrape_page("http://example.com/page.html", "Test")

    @patch("scraper.requests.get")
    def test_500(self, mock_get):
        mock_get.return_value = _mock_response(status_code=500)
        with pytest.raises(ScraperError, match="500"):
            scrape_page("http://example.com/page.html", "Test")

    @patch("scraper.requests.get")
    def test_non_html_content_type(self, mock_get):
        mock_get.return_value = _mock_response(
            status_code=200,
            content_type="application/json",
        )
        with pytest.raises(ScraperError, match="Content-Type"):
            scrape_page("http://example.com/page.html", "Test")

    @patch("scraper.requests.get")
    def test_valid_page(self, mock_get):
        mock_get.return_value = _mock_response(
            status_code=200,
            text=VALID_PAGE_HTML,
            content_type="text/html",
        )
        result = scrape_page("http://example.com/page.html", "What is AWS Lambda?")
        assert "# What is AWS Lambda?" in result
        assert "compute service" in result


# ===================================================================
# generate_index
# ===================================================================


class TestGenerateIndex:
    """Tests for TOC index generation."""

    def test_generates_valid_index(self):
        result = generate_index(VALID_TOC)
        assert "# AWS Lambda Developer Guide" in result
        assert "- [What is AWS Lambda?](welcome.md)" in result
        assert "  - [How it works](concepts-basics.md)" in result
        assert "    - [Programming model](foundation-progmodel.md)" in result
        assert "- [Create your first function](getting-started.md)" in result

    def test_replaces_html_with_md_extension(self):
        result = generate_index(VALID_TOC)
        assert ".html" not in result
        assert ".md)" in result

    def test_has_frontmatter(self):
        result = generate_index(VALID_TOC)
        assert result.startswith("---\n")
        assert "title: AWS Lambda Developer Guide" in result


# ===================================================================
# format_duration
# ===================================================================


class TestFormatDuration:
    """Tests for duration formatting."""

    def test_seconds(self):
        assert format_duration(45) == "00m45s"

    def test_minutes(self):
        assert format_duration(125) == "02m05s"

    def test_hours(self):
        assert format_duration(3661) == "1h01m01s"

    def test_zero(self):
        assert format_duration(0) == "00m00s"

    def test_negative(self):
        assert format_duration(-1) == "--:--"


# ===================================================================
# Integration-style: main() failure paths
# ===================================================================


class TestMainFailurePaths:
    """Tests for main() orchestration failure modes."""

    @patch("scraper.fetch_toc")
    def test_zero_pages_from_toc(self, mock_fetch):
        """TOC with items that have no href should fail."""
        mock_fetch.return_value = {
            "contents": [{"title": "Section Only", "contents": []}]
        }
        from scraper import main
        with pytest.raises(SystemExit, match="2"):
            main()

    @patch("scraper.requests.get")
    @patch("scraper.fetch_toc")
    def test_smoke_test_failure_non_200(self, mock_fetch, mock_get):
        """If the first page returns non-200 during smoke test, should fail."""
        mock_fetch.return_value = VALID_TOC
        mock_get.return_value = _mock_response(status_code=503)
        from scraper import main
        with pytest.raises(SystemExit, match="2"):
            main()

    @patch("scraper.requests.get")
    @patch("scraper.fetch_toc")
    def test_smoke_test_failure_no_main_col_body(self, mock_fetch, mock_get):
        """If the first page has no main-col-body during smoke test, should fail."""
        mock_fetch.return_value = VALID_TOC
        mock_get.return_value = _mock_response(
            status_code=200,
            text="<html><body><p>Restructured page</p></body></html>",
            content_type="text/html",
        )
        from scraper import main
        with pytest.raises(SystemExit, match="2"):
            main()
