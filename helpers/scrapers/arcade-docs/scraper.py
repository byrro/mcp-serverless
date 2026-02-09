# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests",
# ]
# ///
"""
Arcade documentation scraper.

Fetches the official Arcade documentation from https://docs.arcade.dev/
by appending ``.md`` to each page URL, which returns the page content as
Markdown directly (no HTML parsing needed).

Page URLs are discovered via the sitemap referenced in robots.txt.
On subsequent runs, only pages whose remote ``lastmod`` (from the sitemap)
is newer than the local ``scraped_at`` timestamp are re-downloaded.
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://docs.arcade.dev"
ROBOTS_URL = BASE_URL + "/robots.txt"
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "external" / "arcade-docs"
REQUEST_DELAY = 0.3  # seconds between requests

SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class ScraperError(Exception):
    """Raised when the scraper detects a per-page problem."""


def _fail(msg: str) -> None:
    """Print a detailed error and exit."""
    print(f"\n❌  SCRAPER BROKEN: {msg}", file=sys.stderr, flush=True)
    print(
        "\nThis likely means Arcade changed their documentation site structure.\n"
        "Please inspect the URLs manually and update the scraper.\n"
        f"  Base URL: {BASE_URL}",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Robots.txt → Sitemap discovery
# ---------------------------------------------------------------------------


def fetch_sitemap_url() -> str:
    """Read robots.txt and extract the Sitemap URL."""
    try:
        resp = requests.get(ROBOTS_URL, timeout=30)
    except requests.RequestException as exc:
        _fail(f"Could not reach robots.txt ({ROBOTS_URL}).\n  Network error: {exc}")

    if resp.status_code != 200:
        _fail(
            f"robots.txt returned HTTP {resp.status_code} (expected 200).\n"
            f"  URL: {ROBOTS_URL}"
        )

    for line in resp.text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            return url

    _fail(
        "robots.txt does not contain a Sitemap directive.\n"
        f"  URL: {ROBOTS_URL}\n"
        f"  Content:\n{resp.text[:500]}"
    )


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------


def fetch_sitemap(sitemap_url: str) -> str:
    """Fetch the sitemap XML."""
    try:
        resp = requests.get(sitemap_url, timeout=30)
    except requests.RequestException as exc:
        _fail(f"Could not reach sitemap ({sitemap_url}).\n  Network error: {exc}")

    if resp.status_code != 200:
        _fail(
            f"Sitemap returned HTTP {resp.status_code} (expected 200).\n"
            f"  URL: {sitemap_url}"
        )

    return resp.text


def parse_sitemap(xml_text: str) -> list[dict]:
    """Parse sitemap XML into a list of {url, lastmod} dicts.

    Returns a list sorted by URL for deterministic ordering.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        _fail(
            f"Sitemap XML is malformed.\n"
            f"  Parse error: {exc}\n"
            f"  First 300 chars: {xml_text[:300]}"
        )

    urls = root.findall(".//s:url", SITEMAP_NS)
    if not urls:
        # Maybe namespace changed — try without namespace
        urls = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url")

    if not urls:
        _fail(
            "Sitemap XML contains no <url> elements.\n"
            "  The sitemap schema may have changed."
        )

    pages = []
    for url_el in urls:
        loc_el = url_el.find("s:loc", SITEMAP_NS)
        if loc_el is None:
            loc_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if loc_el is None or not loc_el.text:
            continue

        lastmod_el = url_el.find("s:lastmod", SITEMAP_NS)
        if lastmod_el is None:
            lastmod_el = url_el.find("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")

        lastmod = None
        if lastmod_el is not None and lastmod_el.text:
            try:
                lastmod = datetime.fromisoformat(lastmod_el.text.replace("Z", "+00:00"))
            except ValueError:
                pass

        pages.append({"url": loc_el.text.strip(), "lastmod": lastmod})

    if not pages:
        _fail("Sitemap parsed but yielded 0 pages with valid <loc> elements.")

    return sorted(pages, key=lambda p: p["url"])


def validate_sitemap_pages(pages: list[dict]) -> None:
    """Spot-check that sitemap pages look like Arcade doc URLs."""
    first_url = pages[0]["url"]
    if "docs.arcade.dev" not in first_url:
        _fail(
            f"First sitemap URL doesn't look like an Arcade docs page.\n"
            f"  URL: {first_url}\n"
            f"  The sitemap may now point to different content."
        )


# ---------------------------------------------------------------------------
# URL → local file path
# ---------------------------------------------------------------------------


def url_to_filepath(url: str) -> Path:
    """Convert a page URL to a local .md file path.

    https://docs.arcade.dev/en/guides/security → guides/security.md
    Strips the /en/ prefix to keep paths short.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")

    # Strip leading "en/" locale prefix
    if path.startswith("en/"):
        path = path[3:]

    # Replace path separators with -- for a flat directory structure
    filename = path.replace("/", "--") + ".md"
    return OUTPUT_DIR / filename


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------


def get_local_scraped_at(filepath: Path) -> datetime | None:
    """Extract scraped_at from YAML frontmatter of a local .md file."""
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
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


def needs_update(filepath: Path, lastmod: datetime | None) -> bool:
    """Check whether a page needs to be (re-)scraped.

    Uses the sitemap ``lastmod`` timestamp to compare against the local
    ``scraped_at`` — no extra HEAD request needed (unlike the AWS scraper).
    """
    scraped_at = get_local_scraped_at(filepath)
    if scraped_at is None:
        return True
    if lastmod is None:
        # No lastmod in sitemap → conservatively re-scrape
        return True
    return lastmod > scraped_at


# ---------------------------------------------------------------------------
# Markdown fetching
# ---------------------------------------------------------------------------


def fetch_page_markdown(url: str) -> str:
    """Fetch a page as Markdown by appending ``.md`` to the URL path."""
    md_url = url.rstrip("/") + ".md"
    try:
        resp = requests.get(md_url, timeout=30)
    except requests.RequestException as exc:
        raise ScraperError(f"Network error fetching {md_url}: {exc}") from exc

    if resp.status_code == 404:
        raise ScraperError(
            f"Page returned 404 — it may have been removed or the .md endpoint "
            f"may not be available for this page.\n  URL: {md_url}"
        )
    if resp.status_code != 200:
        raise ScraperError(f"Page returned HTTP {resp.status_code}.\n  URL: {md_url}")

    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type:
        _fail(
            f"Page returned HTML instead of Markdown.\n"
            f"  URL: {md_url}\n"
            f"  Content-Type: {content_type}\n"
            f"  The .md endpoint may have been removed."
        )

    # Some pages may not support the .md endpoint and return full HTML pages
    # despite a text/plain Content-Type.  Detect and skip them.
    body = resp.text.strip()
    if body.startswith("<!DOCTYPE") or body.startswith("<html"):
        raise ScraperError(
            f"Page returned HTML body despite .md URL.\n"
            f"  URL: {md_url}\n"
            f"  The .md endpoint may not be available for this page."
        )

    return resp.text


def strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Strip YAML frontmatter from markdown text, returning (metadata, body).

    The Arcade .md endpoint returns content with its own frontmatter (title,
    description).  We strip it and merge into our own frontmatter.
    """
    text = text.strip()
    if not text.startswith("---"):
        return {}, text

    end = text.find("---", 3)
    if end == -1:
        return {}, text

    fm_block = text[3:end].strip()
    body = text[end + 3:].strip()

    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip().strip('"').strip("'")

    return meta, body


def build_page(md_body: str, url: str, title: str | None = None) -> str:
    """Wrap raw markdown content with YAML frontmatter.

    If the markdown already contains frontmatter (as the Arcade .md endpoint
    returns), it is stripped and its ``title`` is reused.
    """
    upstream_meta, body = strip_frontmatter(md_body)

    if not body:
        raise ScraperError(
            f"Page returned empty markdown body.\n  URL: {url}"
        )

    if title is None:
        title = upstream_meta.get("title")
    if title is None:
        m = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
        title = m.group(1).strip() if m else url.rsplit("/", 1)[-1]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return (
        f"---\n"
        f"title: {title}\n"
        f"source: {url}\n"
        f"scraped_at: {now}\n"
        f"---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def get_local_title(filepath: Path) -> str | None:
    """Extract title from YAML frontmatter of a local .md file."""
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for _ in range(15):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^title:\s*(.+)$", line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


def generate_index(pages: list[dict]) -> str:
    """Build a _index.md listing all scraped pages, using page titles."""
    lines = [
        "---",
        "title: Arcade Documentation",
        "---",
        "",
        "# Arcade Documentation",
        "",
    ]

    for page in pages:
        filepath = url_to_filepath(page["url"])
        title = get_local_title(filepath) or filepath.stem
        lines.append(f"- [{title}]({filepath.name})")

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
    # 1. Discover sitemap from robots.txt
    print("Reading robots.txt...", flush=True)
    sitemap_url = fetch_sitemap_url()
    print(f"Sitemap URL: {sitemap_url}", flush=True)

    # 2. Fetch and parse sitemap
    print("Fetching sitemap...", flush=True)
    sitemap_xml = fetch_sitemap(sitemap_url)
    pages = parse_sitemap(sitemap_xml)
    validate_sitemap_pages(pages)
    total = len(pages)
    print(f"Found {total} documentation pages.\n", flush=True)

    # 3. Smoke-test the first page (verify .md endpoint works)
    first_url = pages[0]["url"]
    print(f"Smoke-testing first page: {first_url}.md", flush=True)
    try:
        md = fetch_page_markdown(first_url)
        if not md.strip():
            _fail(f"Smoke test: first page returned empty markdown.\n  URL: {first_url}")
        if md.strip().startswith("<!DOCTYPE") or md.strip().startswith("<html"):
            _fail(
                f"Smoke test: .md endpoint returned HTML instead of Markdown.\n"
                f"  URL: {first_url}.md\n"
                f"  The .md endpoint may have been removed."
            )
        print("Smoke test passed.\n", flush=True)
    except ScraperError as exc:
        _fail(f"Smoke test failed: {exc}")

    # 4. Scrape all pages
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    updated = 0
    skipped = 0
    failed = 0
    t_start = time.monotonic()

    print_progress(0, total, skipped=0, updated=0, failed=0, elapsed=0)

    for i, page in enumerate(pages, 1):
        url = page["url"]
        filepath = url_to_filepath(url)

        if not needs_update(filepath, page["lastmod"]):
            skipped += 1
            print_progress(
                i, total, skipped=skipped, updated=updated, failed=failed,
                elapsed=time.monotonic() - t_start,
            )
            continue

        try:
            md_body = fetch_page_markdown(url)
            result = build_page(md_body, url)
            filepath.write_text(result, encoding="utf-8")
            updated += 1
        except ScraperError:
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

    # 5. Generate index
    print("Generating _index.md...", flush=True)
    (OUTPUT_DIR / "_index.md").write_text(generate_index(pages), encoding="utf-8")

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
