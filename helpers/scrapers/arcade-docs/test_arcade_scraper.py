# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pytest",
#   "requests",
# ]
# ///
"""Unit tests for the Arcade documentation scraper."""

import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from scraper import (
    ScraperError,
    build_page,
    fetch_page_markdown,
    fetch_sitemap_url,
    format_duration,
    generate_index,
    get_local_scraped_at,
    needs_update,
    parse_sitemap,
    url_to_filepath,
    validate_sitemap_pages,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

VALID_ROBOTS_TXT = textwrap.dedent("""\
    # *
    User-agent: *
    Allow: /

    # Host
    Host: https://docs.arcade.dev

    # Sitemaps
    Sitemap: https://docs.arcade.dev/sitemap.xml
""")

VALID_SITEMAP_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
      <loc>https://docs.arcade.dev/en/get-started/about-arcade</loc>
      <lastmod>2026-02-07T01:46:28.229Z</lastmod>
    </url>
    <url>
      <loc>https://docs.arcade.dev/en/guides/security</loc>
      <lastmod>2026-02-07T01:46:28.233Z</lastmod>
    </url>
    <url>
      <loc>https://docs.arcade.dev/en/home</loc>
      <lastmod>2026-02-07T01:46:28.233Z</lastmod>
    </url>
    </urlset>
""")

VALID_MARKDOWN_BODY = textwrap.dedent("""\
    # About Arcade

    Arcade is a platform for building AI agents with tool-calling capabilities.

    ## Getting Started

    Install the SDK and get your API key.
""")


def _mock_response(
    status_code: int = 200,
    text: str = "",
    headers: dict | None = None,
    content_type: str = "text/html",
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"Content-Type": content_type}
    if headers:
        resp.headers.update(headers)
    return resp


# ===================================================================
# fetch_sitemap_url (robots.txt parsing)
# ===================================================================


class TestFetchSitemapUrl:

    @patch("scraper.requests.get")
    def test_valid_robots_txt(self, mock_get):
        mock_get.return_value = _mock_response(text=VALID_ROBOTS_TXT)
        url = fetch_sitemap_url()
        assert url == "https://docs.arcade.dev/sitemap.xml"

    @patch("scraper.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("DNS failure")
        with pytest.raises(SystemExit, match="2"):
            fetch_sitemap_url()

    @patch("scraper.requests.get")
    def test_non_200(self, mock_get):
        mock_get.return_value = _mock_response(status_code=403)
        with pytest.raises(SystemExit, match="2"):
            fetch_sitemap_url()

    @patch("scraper.requests.get")
    def test_no_sitemap_directive(self, mock_get):
        mock_get.return_value = _mock_response(text="User-agent: *\nDisallow: /private\n")
        with pytest.raises(SystemExit, match="2"):
            fetch_sitemap_url()

    @patch("scraper.requests.get")
    def test_case_insensitive_sitemap(self, mock_get):
        mock_get.return_value = _mock_response(text="sitemap: https://example.com/map.xml\n")
        url = fetch_sitemap_url()
        assert url == "https://example.com/map.xml"


# ===================================================================
# parse_sitemap
# ===================================================================


class TestParseSitemap:

    def test_valid_sitemap(self):
        pages = parse_sitemap(VALID_SITEMAP_XML)
        assert len(pages) == 3
        # Should be sorted by URL
        assert pages[0]["url"] == "https://docs.arcade.dev/en/get-started/about-arcade"
        assert pages[0]["lastmod"] is not None
        assert isinstance(pages[0]["lastmod"], datetime)

    def test_malformed_xml(self):
        with pytest.raises(SystemExit, match="2"):
            parse_sitemap("<not valid xml")

    def test_no_url_elements(self):
        xml = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        with pytest.raises(SystemExit, match="2"):
            parse_sitemap(xml)

    def test_missing_lastmod(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://docs.arcade.dev/en/home</loc></url>
            </urlset>
        """)
        pages = parse_sitemap(xml)
        assert len(pages) == 1
        assert pages[0]["lastmod"] is None

    def test_invalid_lastmod_ignored(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url>
              <loc>https://docs.arcade.dev/en/home</loc>
              <lastmod>not-a-date</lastmod>
            </url>
            </urlset>
        """)
        pages = parse_sitemap(xml)
        assert len(pages) == 1
        assert pages[0]["lastmod"] is None

    def test_entries_without_loc_are_skipped(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0"?>
            <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><lastmod>2026-01-01T00:00:00Z</lastmod></url>
            <url><loc>https://docs.arcade.dev/en/home</loc></url>
            </urlset>
        """)
        pages = parse_sitemap(xml)
        assert len(pages) == 1


# ===================================================================
# validate_sitemap_pages
# ===================================================================


class TestValidateSitemapPages:

    def test_valid_pages(self):
        pages = [{"url": "https://docs.arcade.dev/en/home", "lastmod": None}]
        validate_sitemap_pages(pages)  # should not raise

    def test_wrong_domain(self):
        pages = [{"url": "https://example.com/page", "lastmod": None}]
        with pytest.raises(SystemExit, match="2"):
            validate_sitemap_pages(pages)


# ===================================================================
# url_to_filepath
# ===================================================================


class TestUrlToFilepath:

    def test_strips_en_prefix(self):
        fp = url_to_filepath("https://docs.arcade.dev/en/guides/security")
        assert fp.name == "guides--security.md"

    def test_nested_path(self):
        fp = url_to_filepath("https://docs.arcade.dev/en/get-started/agent-frameworks/crewai/use-arcade-tools")
        assert fp.name == "get-started--agent-frameworks--crewai--use-arcade-tools.md"

    def test_home_page(self):
        fp = url_to_filepath("https://docs.arcade.dev/en/home")
        assert fp.name == "home.md"

    def test_no_en_prefix(self):
        fp = url_to_filepath("https://docs.arcade.dev/some/other/path")
        assert fp.name == "some--other--path.md"


# ===================================================================
# get_local_scraped_at
# ===================================================================


class TestGetLocalScrapedAt:

    def test_valid_frontmatter(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\ntitle: Test\nsource: http://x\nscraped_at: 2026-02-01 12:00:00\n---\n")
        result = get_local_scraped_at(f)
        assert result == datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_missing_file(self, tmp_path):
        assert get_local_scraped_at(tmp_path / "nope.md") is None

    def test_no_scraped_at_field(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\ntitle: Test\n---\n")
        assert get_local_scraped_at(f) is None


# ===================================================================
# needs_update
# ===================================================================


class TestNeedsUpdate:

    def test_no_local_file(self, tmp_path):
        assert needs_update(tmp_path / "missing.md", None) is True

    def test_fresh_file(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-09 10:00:00\n---\n")
        lastmod = datetime(2026, 2, 8, 10, 0, 0, tzinfo=timezone.utc)
        assert needs_update(f, lastmod) is False

    def test_stale_file(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-01 10:00:00\n---\n")
        lastmod = datetime(2026, 2, 9, 10, 0, 0, tzinfo=timezone.utc)
        assert needs_update(f, lastmod) is True

    def test_no_lastmod_conservatively_updates(self, tmp_path):
        f = tmp_path / "page.md"
        f.write_text("---\nscraped_at: 2026-02-09 10:00:00\n---\n")
        assert needs_update(f, None) is True


# ===================================================================
# fetch_page_markdown
# ===================================================================


class TestFetchPageMarkdown:

    @patch("scraper.requests.get")
    def test_valid_markdown_response(self, mock_get):
        mock_get.return_value = _mock_response(
            text=VALID_MARKDOWN_BODY, content_type="text/markdown; charset=utf-8"
        )
        result = fetch_page_markdown("https://docs.arcade.dev/en/home")
        assert "Arcade" in result

    @patch("scraper.requests.get")
    def test_text_plain_accepted(self, mock_get):
        mock_get.return_value = _mock_response(
            text=VALID_MARKDOWN_BODY, content_type="text/plain"
        )
        result = fetch_page_markdown("https://docs.arcade.dev/en/home")
        assert "Arcade" in result

    @patch("scraper.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(ScraperError, match="Network error"):
            fetch_page_markdown("https://docs.arcade.dev/en/home")

    @patch("scraper.requests.get")
    def test_404(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404)
        with pytest.raises(ScraperError, match="404"):
            fetch_page_markdown("https://docs.arcade.dev/en/home")

    @patch("scraper.requests.get")
    def test_500(self, mock_get):
        mock_get.return_value = _mock_response(status_code=500)
        with pytest.raises(ScraperError, match="500"):
            fetch_page_markdown("https://docs.arcade.dev/en/home")

    @patch("scraper.requests.get")
    def test_html_response_means_endpoint_not_deployed(self, mock_get):
        """If server returns HTML, the markdown endpoint isn't live yet."""
        mock_get.return_value = _mock_response(
            text="<html><body>page</body></html>",
            content_type="text/html; charset=utf-8",
        )
        with pytest.raises(SystemExit, match="2"):
            fetch_page_markdown("https://docs.arcade.dev/en/home")

    @patch("scraper.requests.get")
    def test_unexpected_content_type_but_valid_body(self, mock_get):
        """Non-markdown Content-Type is accepted as long as body isn't HTML."""
        mock_get.return_value = _mock_response(
            text="# Some markdown", content_type="application/octet-stream"
        )
        result = fetch_page_markdown("https://docs.arcade.dev/en/home")
        assert result == "# Some markdown"

    @patch("scraper.requests.get")
    def test_html_body_rejected(self, mock_get):
        """HTML body is rejected even if Content-Type is text/plain."""
        mock_get.return_value = _mock_response(
            text="<!DOCTYPE html><html><body>not markdown</body></html>",
            content_type="text/plain",
        )
        with pytest.raises(ScraperError, match="HTML body"):
            fetch_page_markdown("https://docs.arcade.dev/en/home")


# ===================================================================
# build_page
# ===================================================================


class TestBuildPage:

    def test_adds_frontmatter(self):
        result = build_page(VALID_MARKDOWN_BODY, "https://docs.arcade.dev/en/home")
        assert result.startswith("---\n")
        assert "title: About Arcade" in result
        assert "source: https://docs.arcade.dev/en/home" in result
        assert "scraped_at:" in result

    def test_extracts_title_from_heading(self):
        result = build_page("# My Title\n\nContent here.", "https://example.com/page")
        assert "title: My Title" in result

    def test_fallback_title_from_url(self):
        result = build_page("No heading, just content text here.", "https://example.com/my-page")
        assert "title: my-page" in result

    def test_empty_body_raises(self):
        with pytest.raises(ScraperError, match="empty"):
            build_page("", "https://example.com/page")

    def test_whitespace_only_body_raises(self):
        with pytest.raises(ScraperError, match="empty"):
            build_page("   \n\n  ", "https://example.com/page")

    def test_explicit_title(self):
        result = build_page("Some content here.", "https://example.com/page", title="Custom Title")
        assert "title: Custom Title" in result

    def test_preserves_markdown_body(self):
        result = build_page(VALID_MARKDOWN_BODY, "https://example.com/page")
        assert "## Getting Started" in result
        assert "Install the SDK" in result


# ===================================================================
# generate_index
# ===================================================================


class TestGenerateIndex:

    def test_generates_valid_index(self):
        pages = parse_sitemap(VALID_SITEMAP_XML)
        result = generate_index(pages)
        assert "# Arcade Documentation" in result
        assert ".md)" in result
        assert result.startswith("---\n")

    def test_lists_all_pages(self):
        pages = parse_sitemap(VALID_SITEMAP_XML)
        result = generate_index(pages)
        assert "get-started--about-arcade" in result
        assert "guides--security" in result
        assert "home" in result


# ===================================================================
# format_duration
# ===================================================================


class TestFormatDuration:

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

    @patch("scraper.fetch_sitemap")
    @patch("scraper.fetch_sitemap_url")
    def test_empty_sitemap(self, mock_url, mock_sitemap):
        mock_url.return_value = "https://docs.arcade.dev/sitemap.xml"
        mock_sitemap.return_value = (
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        )
        from scraper import main
        with pytest.raises(SystemExit, match="2"):
            main()

    @patch("scraper.fetch_page_markdown")
    @patch("scraper.fetch_sitemap")
    @patch("scraper.fetch_sitemap_url")
    def test_smoke_test_html_instead_of_markdown(self, mock_url, mock_sitemap, mock_fetch_md):
        mock_url.return_value = "https://docs.arcade.dev/sitemap.xml"
        mock_sitemap.return_value = VALID_SITEMAP_XML
        mock_fetch_md.side_effect = SystemExit(2)  # _fail exits
        from scraper import main
        with pytest.raises(SystemExit):
            main()
