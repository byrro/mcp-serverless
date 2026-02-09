# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
#   "beautifulsoup4",
#   "markdownify",
# ]
# ///
"""
AWS Lambda Developer Guide scraper.

Fetches the official AWS Lambda documentation from
https://docs.aws.amazon.com/lambda/latest/dg/ and converts each page
to Markdown, storing the result in ./external/aws-lambda/.

On subsequent runs, only pages whose remote Last-Modified date is newer
than the local scraped_at timestamp are re-downloaded.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://docs.aws.amazon.com/lambda/latest/dg/"
TOC_URL = BASE_URL + "toc-contents.json"
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "external" / "aws-lambda"
REQUEST_DELAY = 0.3  # seconds between requests


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class ScraperError(Exception):
    """Raised when the scraper detects an upstream format change."""


def _fail(msg: str) -> None:
    """Print a detailed error and exit."""
    print(f"\n❌  SCRAPER BROKEN: {msg}", file=sys.stderr, flush=True)
    print(
        "\nThis likely means AWS changed their documentation site structure.\n"
        "Please inspect the URLs/HTML manually and update the scraper.\n"
        f"  TOC URL:  {TOC_URL}\n"
        f"  Base URL: {BASE_URL}",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# TOC helpers
# ---------------------------------------------------------------------------


def fetch_toc() -> dict:
    """Fetch and parse the table of contents JSON."""
    try:
        resp = requests.get(TOC_URL, timeout=30)
    except requests.RequestException as exc:
        _fail(f"Could not reach TOC URL ({TOC_URL}).\n  Network error: {exc}")

    if resp.status_code != 200:
        _fail(
            f"TOC URL returned HTTP {resp.status_code} (expected 200).\n"
            f"  URL: {TOC_URL}\n"
            f"  The TOC endpoint may have been moved or removed."
        )

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        _fail(
            f"TOC URL did not return valid JSON.\n"
            f"  URL: {TOC_URL}\n"
            f"  Content-Type: {resp.headers.get('Content-Type', '(none)')}\n"
            f"  Parse error: {exc}\n"
            f"  AWS may have changed the TOC format or endpoint."
        )

    return data


def validate_toc(data: dict) -> None:
    """Verify the TOC JSON has the expected structure."""
    if not isinstance(data, dict):
        _fail(
            f"TOC JSON is a {type(data).__name__}, expected a dict.\n"
            f"  AWS may have changed the toc-contents.json schema."
        )

    if "contents" not in data:
        _fail(
            "TOC JSON missing top-level 'contents' key.\n"
            f"  Keys found: {list(data.keys())}\n"
            f"  AWS may have changed the toc-contents.json schema."
        )

    if not isinstance(data["contents"], list) or len(data["contents"]) == 0:
        _fail(
            "TOC JSON 'contents' is empty or not a list.\n"
            f"  Type: {type(data['contents']).__name__}, "
            f"Length: {len(data['contents']) if isinstance(data['contents'], list) else 'N/A'}\n"
            f"  AWS may have changed the toc-contents.json schema."
        )

    # Spot-check the first entry has the expected keys
    first = data["contents"][0]
    if not isinstance(first, dict) or "title" not in first or "href" not in first:
        _fail(
            "First TOC entry is missing expected 'title' and/or 'href' keys.\n"
            f"  Entry: {json.dumps(first, indent=2)[:300]}\n"
            f"  AWS may have changed the toc-contents.json schema."
        )


def flatten_toc(contents: list, pages: list | None = None) -> list[tuple[str, str]]:
    """Flatten nested TOC into a list of (title, href) tuples."""
    if pages is None:
        pages = []
    for item in contents:
        if "href" in item:
            pages.append((item["title"], item["href"]))
        if "contents" in item:
            flatten_toc(item["contents"], pages)
    return pages


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------


def get_local_scraped_at(filepath: Path) -> datetime | None:
    """Extract scraped_at from YAML frontmatter of a local .md file."""
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # Read only the frontmatter (first ~10 lines)
            for _ in range(15):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^scraped_at:\s*(.+)$", line)
                if m:
                    dt = datetime.strptime(m.group(1).strip(), "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def needs_update(filepath: Path, url: str) -> bool:
    """Check whether a page needs to be (re-)scraped.

    Uses a HEAD request to compare the remote Last-Modified date with the
    local scraped_at timestamp.  If Last-Modified is unavailable we
    conservatively re-scrape.
    """
    scraped_at = get_local_scraped_at(filepath)
    if scraped_at is None:
        return True

    try:
        resp = requests.head(url, timeout=10)
        last_modified_str = resp.headers.get("Last-Modified")
        if last_modified_str:
            last_modified = parsedate_to_datetime(last_modified_str)
            if last_modified <= scraped_at:
                return False
    except Exception:
        pass

    # Default: re-scrape when we cannot determine freshness
    return True


# ---------------------------------------------------------------------------
# HTML → Markdown conversion
# ---------------------------------------------------------------------------

# Elements to remove from the main content div before conversion
REMOVE_TAGS = [
    "awsdocs-language-banner",
    "awsdocs-page-header",
    "awsdocs-filter-selector",
    "awsdocs-copyright",
    "awsdocs-thumb-feedback",
    "awsdocs-doc-page-banner",
]


def clean_html(main: Tag) -> None:
    """Remove non-content chrome from the main content element (in-place)."""
    # The page title <h1> is inside main-col-body; we add our own from the
    # TOC metadata, so strip the original to avoid duplication.
    h1 = main.find("h1", class_="topictitle")
    if h1:
        h1.decompose()

    # Custom AWS web-components
    for tag_name in REMOVE_TAGS:
        for el in main.find_all(tag_name):
            el.decompose()

    # "Did this page help you?" feedback blocks
    for el in main.find_all(string=re.compile(r"Did this page help you", re.I)):
        parent = el.find_parent("div")
        if parent:
            parent.decompose()

    # "Thanks for letting us know" feedback blocks
    for el in main.find_all(string=re.compile(r"Thanks for letting us know", re.I)):
        parent = el.find_parent("div")
        if parent:
            parent.decompose()

    # awsdocs-note / awsdocs-tip wrappers: keep the inner content, drop the
    # wrapper noise.  (They render fine as-is via markdownify.)

    # Remove empty divs that just add whitespace
    for div in main.find_all("div"):
        if not div.get_text(strip=True) and not div.find_all(["img", "table", "pre", "code"]):
            div.decompose()


def validate_page_html(soup: BeautifulSoup, url: str) -> Tag:
    """Locate and return the main content div, or fail with a clear message."""
    main = soup.find("div", id="main-col-body")
    if main is None:
        # Check for common alternative containers to give a helpful hint
        alternatives = []
        for candidate_id in ["main-content", "content", "doc-content", "main"]:
            if soup.find("div", id=candidate_id) or soup.find(candidate_id):
                alternatives.append(candidate_id)

        hint = ""
        if alternatives:
            hint = f"\n  Possible alternative containers found: {alternatives}"

        _fail(
            f"Page HTML is missing <div id=\"main-col-body\">.\n"
            f"  URL: {url}{hint}\n"
            f"  AWS may have changed the documentation page HTML structure."
        )

    # Sanity check: the content div should have some actual text
    text = main.get_text(strip=True)
    if len(text) < 50:
        _fail(
            f"<div id=\"main-col-body\"> exists but contains almost no text ({len(text)} chars).\n"
            f"  URL: {url}\n"
            f"  The page content may now be loaded dynamically via JavaScript."
        )

    return main


def parse_page_html(html: str, url: str, title: str) -> str:
    """Parse an HTML doc page and return it as Markdown with frontmatter.

    This is a pure function (no network I/O) so it can be tested directly.
    """
    soup = BeautifulSoup(html, "html.parser")
    main = validate_page_html(soup, url)

    clean_html(main)

    md_body = markdownify(str(main), heading_style="ATX", strip=["img"])

    # Tidy up excessive blank lines
    md_body = re.sub(r"\n{3,}", "\n\n", md_body)
    md_body = md_body.strip()

    if not md_body:
        raise ScraperError(
            f"Markdown conversion produced empty output.\n"
            f"  URL: {url}\n"
            f"  The HTML-to-Markdown conversion may need updating."
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"---\n"
        f"title: {title}\n"
        f"source: {url}\n"
        f"scraped_at: {now}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{md_body}\n"
    )


def scrape_page(url: str, title: str) -> str:
    """Download a single doc page and return it as Markdown with frontmatter."""
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as exc:
        raise ScraperError(f"Network error fetching {url}: {exc}") from exc

    if resp.status_code == 404:
        raise ScraperError(
            f"Page returned 404 — it may have been removed or renamed.\n"
            f"  URL: {url}"
        )
    if resp.status_code != 200:
        raise ScraperError(f"Page returned HTTP {resp.status_code}.\n  URL: {url}")

    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type:
        raise ScraperError(
            f"Page returned unexpected Content-Type: '{content_type}' (expected HTML).\n"
            f"  URL: {url}"
        )

    return parse_page_html(resp.text, url, title)


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def generate_index(toc_data: dict) -> str:
    """Build a _index.md with the full TOC tree linking to local files."""
    lines = [
        "---",
        "title: AWS Lambda Developer Guide",
        "---",
        "",
        "# AWS Lambda Developer Guide",
        "",
    ]

    def _render(contents: list, depth: int = 0) -> None:
        for item in contents:
            indent = "  " * depth
            if "href" in item:
                md_name = item["href"].replace(".html", ".md")
                lines.append(f"{indent}- [{item['title']}]({md_name})")
            if "contents" in item:
                _render(item["contents"], depth + 1)

    _render(toc_data["contents"])

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m:02d}m{s:02d}s"


def print_progress(
    done: int,
    total: int,
    *,
    skipped: int = 0,
    updated: int = 0,
    failed: int = 0,
    elapsed: float = 0.0,
    bar_width: int = 30,
) -> None:
    """Overwrite the current terminal line with a progress bar + ETA."""
    pct = done / total if total else 1.0
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    # ETA based on average time per page processed so far
    if done > 0 and done < total:
        eta = (elapsed / done) * (total - done)
        eta_str = format_duration(eta)
    elif done >= total:
        eta_str = "done"
    else:
        eta_str = "--:--"

    status = (
        f"\r  {bar} {done}/{total} ({pct:5.1%}) "
        f"| ↻ {updated} ✓ {skipped} ✗ {failed} "
        f"| ETA {eta_str}  "
    )
    sys.stderr.write(status)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Fetching table of contents...", flush=True)
    toc = fetch_toc()
    validate_toc(toc)

    pages = flatten_toc(toc["contents"])
    total = len(pages)

    if total == 0:
        _fail(
            "TOC parsed successfully but yielded 0 pages.\n"
            "  The TOC structure may have changed — items may no longer use "
            "'href' keys."
        )

    print(f"Found {total} documentation pages.\n", flush=True)

    # Smoke-test the first page to catch HTML structure changes early,
    # before committing to the full scrape.
    first_title, first_href = pages[0]
    first_url = BASE_URL + first_href
    print(f"Smoke-testing first page: {first_url}", flush=True)
    try:
        resp = requests.get(first_url, timeout=30)
        if resp.status_code != 200:
            _fail(
                f"Smoke test: first page returned HTTP {resp.status_code}.\n"
                f"  URL: {first_url}"
            )
        soup = BeautifulSoup(resp.text, "html.parser")
        validate_page_html(soup, first_url)
        print("Smoke test passed.\n", flush=True)
    except ScraperError:
        raise
    except Exception as exc:
        _fail(f"Smoke test failed with unexpected error: {exc}\n  URL: {first_url}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    updated = 0
    skipped = 0
    failed = 0
    t_start = time.monotonic()

    print_progress(0, total, skipped=0, updated=0, failed=0, elapsed=0)

    for i, (title, href) in enumerate(pages, 1):
        url = BASE_URL + href
        filepath = OUTPUT_DIR / href.replace(".html", ".md")

        if not needs_update(filepath, url):
            skipped += 1
            print_progress(
                i, total, skipped=skipped, updated=updated, failed=failed,
                elapsed=time.monotonic() - t_start,
            )
            continue

        try:
            md = scrape_page(url, title)
            filepath.write_text(md, encoding="utf-8")
            updated += 1
        except ScraperError as exc:
            # Per-page errors are logged but don't abort the whole run.
            # The progress bar shows the failure count.
            failed += 1
        except Exception:
            failed += 1

        print_progress(
            i, total, skipped=skipped, updated=updated, failed=failed,
            elapsed=time.monotonic() - t_start,
        )
        time.sleep(REQUEST_DELAY)

    # Clear the progress line
    sys.stderr.write("\r" + " " * 80 + "\r")
    sys.stderr.flush()

    elapsed = time.monotonic() - t_start

    # Always regenerate the index (cheap operation)
    print("Generating _index.md...", flush=True)
    (OUTPUT_DIR / "_index.md").write_text(generate_index(toc), encoding="utf-8")

    print(
        f"\nDone in {format_duration(elapsed)}!  "
        f"updated={updated}  skipped={skipped}  failed={failed}",
        flush=True,
    )
    print(f"Output directory: {OUTPUT_DIR}", flush=True)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("\r" + " " * 80 + "\r")
        sys.stderr.flush()
        print("\nInterrupted. Progress saved — already scraped pages are on disk.", flush=True)
        sys.exit(130)
