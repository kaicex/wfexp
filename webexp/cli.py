"""Python Webflow Exporter CLI
This script allows you to scrape a Webflow site for assets and internal links,
download them, and process the HTML files to fix asset links. It also provides 
an option to remove the Webflow badge from the HTML files.
"""

from urllib.parse import urlparse, urljoin
import re
import json
import argparse
import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # Python 3.10+
    from importlib.metadata import PackageNotFoundError, version as pkg_version
except ImportError:  # pragma: no cover - fallback for older runtimes
    from importlib_metadata import PackageNotFoundError, version as pkg_version  # type: ignore

import requests
from bs4 import BeautifulSoup
from halo import Halo

try:  # Python 3.11+
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - fallback when tomllib missing
    tomllib = None  # type: ignore[assignment]


def _load_version_from_pyproject(pyproject_path: Path) -> Optional[str]:
    """Return the version from pyproject.toml if available."""

    if not pyproject_path.exists():
        return None

    try:
        content = pyproject_path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        return None

    data = None

    if tomllib is not None:
        try:
            data = tomllib.loads(content)
        except Exception:  # pragma: no cover - defensive
            data = None
    else:
        try:
            import tomli  # type: ignore
        except ModuleNotFoundError:
            tomli = None  # type: ignore
        if tomli is not None:
            try:
                data = tomli.loads(content)
            except Exception:  # pragma: no cover - defensive
                data = None

    if not data:
        return None

    project = data.get("project")
    if isinstance(project, dict):
        version_value = project.get("version")
        if isinstance(version_value, str) and version_value.strip():
            return version_value.strip()

    return None


def _determine_version() -> str:
    """Determine the current package version with graceful fallbacks."""

    package_name = "python-webflow-exporter"
    try:
        return pkg_version(package_name)
    except PackageNotFoundError:
        pyproject_version = _load_version_from_pyproject(
            Path(__file__).resolve().parent.parent / "pyproject.toml"
        )
        if pyproject_version:
            return pyproject_version

        env_version = os.environ.get("WEBEXP_VERSION")
        if env_version:
            return env_version

        return "0.0.0-dev"


VERSION_NUM = _determine_version()

WEBFLOW_ASSET_HOST_SUFFIXES = (
    "website-files.com",
    "webflow.io",
    "webflow.com",
)

WEBFLOW_ASSET_HOSTS = {
    "d3e54v103j8qbb.cloudfront.net",
}

logger = logging.getLogger(__name__)

stdout_log_formatter = logging.Formatter(
    '%(message)s'
)

stdout_log_handler = logging.StreamHandler(stream=sys.stdout)
stdout_log_handler.setLevel(logging.INFO)
stdout_log_handler.setFormatter(stdout_log_formatter)
logger.addHandler(stdout_log_handler)


def normalize_asset_url(url):
    """Convert protocol-relative URLs into absolute HTTPS URLs."""

    if not url:
        return url
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def is_webflow_asset_url(url):
    """Determine if the URL points to a Webflow-managed asset."""

    if not url:
        return False

    normalized = normalize_asset_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    if host in WEBFLOW_ASSET_HOSTS:
        return True

    return any(host.endswith(suffix) for suffix in WEBFLOW_ASSET_HOST_SUFFIXES)


def local_asset_path(asset_type, url):
    """Return the relative local path for a downloaded asset."""

    normalized = normalize_asset_url(url)
    parsed = urlparse(normalized)
    filename = os.path.basename(parsed.path)
    if not filename:
        return None
    return f"{asset_type}/{filename}"


def rewrite_srcset(value, asset_type):
    """Rewrite all URLs in a srcset-style attribute to local paths."""

    if not value:
        return value

    parts = []
    for item in value.split(','):
        piece = item.strip()
        if not piece:
            continue

        if ' ' in piece:
            url_part, descriptor = piece.split(' ', 1)
            descriptor = descriptor.strip()
        else:
            url_part, descriptor = piece, ""

        normalized = normalize_asset_url(url_part)
        if is_webflow_asset_url(normalized):
            local_path = local_asset_path(asset_type, normalized)
            if local_path:
                url_part = local_path

        if descriptor:
            parts.append(f"{url_part} {descriptor}")
        else:
            parts.append(url_part)

    return ", ".join(parts)

def _spinner_start(spinner, text):
    """Helper to start a spinner if one was provided."""

    if spinner:
        spinner.text = text
        spinner.start()


def _spinner_stop(spinner):
    """Helper to stop a spinner if one was provided."""

    if spinner:
        spinner.stop()


def run_export(
    url,
    output="out",
    *,
    remove_badge=False,
    create_sitemap=False,
    debug=False,
    silent=False,
    use_spinner=False,
    ensure_parent_dir=False,
    single_page=False,
):
    """Execute the Webflow export workflow and return a summary."""

    if debug and silent:
        raise ValueError("Invalid configuration: 'debug' and 'silent' options cannot be used together.")

    previous_level = logger.level
    if silent:
        logger.setLevel(logging.ERROR)
    elif debug:
        logger.info("Debug mode enabled.")
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    spinner = Halo(spinner='dots') if use_spinner else None

    try:
        check_url(url)

        output_path = os.path.abspath(output)
        if not check_output_path_exists(output_path, create=ensure_parent_dir):
            raise ValueError("Output path does not exist. Please provide a valid path.")

        clear_output_folder(output_path)

        _spinner_start(spinner, 'Scraping the web...')
        assets_manifest = scan_html(url, follow_internal_links=not single_page)
        _spinner_stop(spinner)

        logger.debug("Assets found: %s", json.dumps(assets_manifest, indent=2))

        _spinner_start(spinner, 'Downloading...')
        download_assets(assets_manifest, output_path)
        _spinner_stop(spinner)

        logger.info("Assets downloaded to %s", output_path)

        if remove_badge:
            _spinner_start(spinner, 'Removing webflow badge...')
            remove_badge_from_output(output_path)
            _spinner_stop(spinner)

        if create_sitemap:
            _spinner_start(spinner, 'Generating sitemap...')
            generate_sitemap(output_path, assets_manifest)
            _spinner_stop(spinner)

        _spinner_stop(spinner)
        return {
            "output_path": output_path,
            "assets": assets_manifest,
        }
    finally:
        _spinner_stop(spinner)
        logger.setLevel(previous_level)


def main():
    """Main function to handle command line arguments and initiate the scraping process."""

    parser = argparse.ArgumentParser(description="Python Webflow Exporter CLI")
    parser.add_argument("--url", required=True, help="the URL to fetch data from")
    parser.add_argument("--output", default="out", help="the folder to save the output to")
    parser.add_argument(
        "--remove-badge",
        action="store_true",
        help="remove Webflow badge"
    )
    parser.add_argument(
        "--generate-sitemap",
        action="store_true",
        help="generate a sitemap.xml file"
    )
    parser.add_argument(
        "--single-page",
        action="store_true",
        help="export only the provided URL without following internal links"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"python-webflow-exporter version: {VERSION_NUM}",
        help="show the version of the package"
    )
    parser.add_argument("--debug", action="store_true", help="enable debug mode")
    parser.add_argument("--silent", action="store_true", help="silent, no output")
    args = parser.parse_args()

    try:
        run_export(
            url=args.url,
            output=args.output,
            remove_badge=args.remove_badge,
            create_sitemap=args.generate_sitemap,
            debug=args.debug,
            silent=args.silent,
            use_spinner=True,
            ensure_parent_dir=False,
            single_page=args.single_page,
        )
    except ValueError as exc:
        logger.error(str(exc))

def check_url(url):
    """Check if the URL is a valid Webflow URL."""

    try:
        request = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        raise ValueError(f"Failed to reach the provided URL: {exc}") from exc

    if request.status_code != 200:
        raise ValueError("Invalid URL. Please provide a valid Webflow URL.")

    soup = BeautifulSoup(request.text, 'html.parser')

    webflow_indicators = []

    # Check 1: Links with "website-files.com" (existing check)
    links = soup.find_all('link', href=True)
    has_webflow_links = any("website-files.com" in link['href'] for link in links)
    if has_webflow_links:
        webflow_indicators.append("website-files.com links")

    # Check 2: Scripts with "website-files.com" (especially webflow.js)
    scripts = soup.find_all('script', src=True)
    has_webflow_scripts = any("website-files.com" in script['src'] for script in scripts)
    if has_webflow_scripts:
        webflow_indicators.append("website-files.com scripts")

    # Check 3: Meta generator tag with "Webflow"
    meta_generator = soup.find('meta', attrs={'name': 'generator', 'content': True})
    has_webflow_meta = (meta_generator and
                        'webflow' in meta_generator.get('content', '').lower())
    if has_webflow_meta:
        webflow_indicators.append("Webflow meta generator")

    # If any indicators are found, consider it a valid Webflow site
    if webflow_indicators:
        logger.debug("Webflow site detected with indicators: %s", ', '.join(webflow_indicators))
        return True

    raise ValueError(
        "The provided URL does not appear to be a Webflow site. "
        "No Webflow indicators found (website-files.com links/scripts or Webflow meta generator tag). "
        "Ensure the site is a valid Webflow site."
    )

def check_output_path_exists(path, create=False):
    """Check if the output parent folder exists, optionally creating it."""

    absolute_path = os.path.abspath(path)
    folder = os.path.dirname(absolute_path)
    if os.path.exists(folder):
        return True
    if create:
        os.makedirs(folder, exist_ok=True)
        return True
    return False

def clear_output_folder(path):
    """Clear the output folder if it exists, or create it if it doesn't."""

    if os.path.exists(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
    else:
        os.makedirs(path)

def scan_html(url):
    """Scan the website for assets and internal links and return a dictionary."""

    visited = set()
    html = []
    assets = {"css": set(), "js": set(), "images": set(), "media": set()}

    base_domain = urlparse(url).netloc

    def recursive_scan(current_url):
        current_url = current_url.rstrip("/")
        if current_url in visited:
            return
        visited.add(current_url)

        try:
            response = requests.get(current_url, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            return

        # Only scan HTML pages
        if "text/html" not in response.headers.get("Content-Type", ""):
            return

        logger.debug("Scanning %s", current_url)
        logger.debug("Found HTML page: %s", current_url)

        html.append(current_url)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find internal links
        for link in soup.find_all('a', href=True):
            href = link['href']
            joined_url = urljoin(current_url + "/", href)
            parsed_url = urlparse(joined_url)

            # Only follow internal links
            if parsed_url.netloc == base_domain:
                normalized_url = parsed_url.scheme + "://" + parsed_url.netloc + parsed_url.path
                recursive_scan(normalized_url)

        # Collect assets
        for css in soup.find_all('link', rel="stylesheet"):
            href = css.get('href')
            if href:
                css_url = normalize_asset_url(urljoin(current_url + "/", href))
                if is_webflow_asset_url(css_url):
                    assets["css"].add(css_url)
                    logger.debug("Found CSS: %s", css_url)

        for link in soup.find_all('link', rel=["apple-touch-icon", "shortcut icon"]):
            href = link.get('href')
            if href:
                image_url = normalize_asset_url(urljoin(current_url + "/", href))
                if is_webflow_asset_url(image_url):
                    assets["images"].add(image_url)
                    logger.debug("Found image file: %s", image_url)

        for preload in soup.find_all('link', href=True):
            rel_values = [rel.lower() for rel in preload.get('rel', [])]
            if 'preload' not in rel_values:
                continue
            href = preload.get('href')
            as_attr = preload.get('as', '').lower()
            bucket_map = {
                'style': 'css',
                'script': 'js',
                'font': 'images',
                'image': 'images',
            }
            asset_bucket = bucket_map.get(as_attr, 'images')
            preload_url = normalize_asset_url(urljoin(current_url + "/", href))
            if is_webflow_asset_url(preload_url):
                assets[asset_bucket].add(preload_url)
                logger.debug("Found preload %s asset: %s", asset_bucket, preload_url)

        for script in soup.find_all('script', src=True):
            src = script['src']
            if src:
                js_url = normalize_asset_url(urljoin(current_url + "/", src))
                if is_webflow_asset_url(js_url):
                    assets["js"].add(js_url)
                    logger.debug("Found Javascript file: %s", js_url)

        for img in soup.find_all('img', src=True):
            src = img['src']
            if src:
                img_url = normalize_asset_url(urljoin(current_url + "/", src))
                if is_webflow_asset_url(img_url):
                    assets["images"].add(img_url)
                    logger.debug("Found image file: %s", img_url)

            srcset = img.get('srcset')
            if srcset:
                for candidate in srcset.split(','):
                    url_part = candidate.strip().split(' ')[0]
                    candidate_url = normalize_asset_url(urljoin(current_url + "/", url_part))
                    if is_webflow_asset_url(candidate_url):
                        assets["images"].add(candidate_url)
                        logger.debug("Found image file in srcset: %s", candidate_url)

            data_src = img.get('data-src')
            if data_src:
                data_url = normalize_asset_url(urljoin(current_url + "/", data_src))
                if is_webflow_asset_url(data_url):
                    assets["images"].add(data_url)
                    logger.debug("Found data-src image: %s", data_url)

            data_srcset = img.get('data-srcset')
            if data_srcset:
                for candidate in data_srcset.split(','):
                    url_part = candidate.strip().split(' ')[0]
                    candidate_url = normalize_asset_url(urljoin(current_url + "/", url_part))
                    if is_webflow_asset_url(candidate_url):
                        assets["images"].add(candidate_url)
                        logger.debug("Found data-srcset image: %s", candidate_url)

        for source in soup.find_all('source'):
            parent = source.parent.name if source.parent else ""
            asset_bucket = "media" if parent in {"video", "audio"} else "images"
            for attribute in ("src", "srcset", "data-src", "data-srcset"):
                value = source.get(attribute)
                if not value:
                    continue
                items = value.split(',') if attribute.endswith('set') else [value]
                for item in items:
                    url_part = item.strip().split(' ')[0]
                    candidate_url = normalize_asset_url(urljoin(current_url + "/", url_part))
                    if is_webflow_asset_url(candidate_url):
                        assets[asset_bucket].add(candidate_url)
                        logger.debug("Found %s asset: %s", asset_bucket, candidate_url)

        for media in soup.find_all(['video', 'audio'], src=True):
            src = media['src']
            if src:
                media_url = normalize_asset_url(urljoin(current_url + "/", src))
                if is_webflow_asset_url(media_url):
                    assets["media"].add(media_url)
                    logger.debug("Found media file: %s", media_url)

        for meta in soup.find_all('meta', content=True):
            content_value = meta['content'].strip()
            if not content_value:
                continue

            parsed_meta = urlparse(normalize_asset_url(content_value))
            if parsed_meta.scheme in {"http", "https"} or content_value.startswith("//"):
                meta_url = normalize_asset_url(content_value)
            elif content_value.startswith("/"):
                meta_url = normalize_asset_url(urljoin(current_url + "/", content_value))
            else:
                continue

            if is_webflow_asset_url(meta_url):
                assets["images"].add(meta_url)
                logger.debug("Found meta asset: %s", meta_url)

    recursive_scan(url)

    return {
        "html": sorted(html),
        "css": sorted(assets["css"]),
        "js": sorted(assets["js"]),
        "images": sorted(assets["images"]),
        "media": sorted(assets["media"])
    }

def download_assets(assets, output_folder):
    """Download assets from the CDN and save them to the output folder."""
    def download_file(url, output_path, asset_type):
        try:
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
            if asset_type == 'html':
                process_html(output_path)
            elif asset_type == 'css':
                process_css(output_path, output_folder)
        except requests.RequestException as e:
            logger.error("Failed to download asset %s: %s", url, e)

    for asset_type, urls in assets.items():
        logger.debug("Downloading %s assets...", asset_type)
        for url in urls:
            parsed_uri = urlparse(url)

            if asset_type == 'html':
                page_path = parsed_uri.path.strip('/')
                if page_path:
                    relative_path = f"{page_path}.html"
                else:
                    relative_path = "index.html"
            else:
                filename = os.path.basename(parsed_uri.path)
                if not filename:
                    logger.debug("Skipping %s asset with empty filename: %s", asset_type, url)
                    continue
                relative_path = os.path.join(asset_type, filename)

            output_path = os.path.join(output_folder, relative_path)

            logger.info("Downloading %s to %s", url, output_path)
            download_file(url, output_path, asset_type)

def process_html(file):
    """Process the HTML file to fix asset links and format the HTML."""

    with open(file, 'r', encoding='utf-8') as f:
        soup = BeautifulSoup(f, 'html.parser')

    def rewrite_attribute(tag, attribute, asset_type):
        value = tag.get(attribute)
        if not value:
            return
        normalized = normalize_asset_url(value)
        if is_webflow_asset_url(normalized):
            local_path = local_asset_path(asset_type, normalized)
            if local_path:
                tag[attribute] = local_path

    def rewrite_srcset_attribute(tag, attribute, asset_type):
        value = tag.get(attribute)
        if not value:
            return
        rewritten = rewrite_srcset(value, asset_type)
        if rewritten:
            tag[attribute] = rewritten

    # Process JS
    for tag in soup.find_all('script'):
        rewrite_attribute(tag, 'src', 'js')

    # Process CSS
    for tag in soup.find_all('link', rel="stylesheet"):
        rewrite_attribute(tag, 'href', 'css')

    # Process links like favicons
    for tag in soup.find_all('link', rel=["apple-touch-icon", "shortcut icon"]):
        rewrite_attribute(tag, 'href', 'images')

    # Process preload links
    for tag in soup.find_all('link', href=True):
        rel_values = [rel.lower() for rel in tag.get('rel', [])]
        if 'preload' not in rel_values:
            continue
        bucket_map = {
            'style': 'css',
            'script': 'js',
            'font': 'images',
            'image': 'images',
        }
        asset_type = bucket_map.get(tag.get('as', '').lower(), 'images')
        rewrite_attribute(tag, 'href', asset_type)

    # Process IMG
    for tag in soup.find_all('img'):
        rewrite_attribute(tag, 'src', 'images')
        rewrite_srcset_attribute(tag, 'srcset', 'images')
        rewrite_attribute(tag, 'data-src', 'images')
        rewrite_srcset_attribute(tag, 'data-srcset', 'images')

    # Process Media
    for tag in soup.find_all(['video', 'audio']):
        rewrite_attribute(tag, 'src', 'media')
        rewrite_srcset_attribute(tag, 'srcset', 'media')
        rewrite_attribute(tag, 'data-src', 'media')
        rewrite_srcset_attribute(tag, 'data-srcset', 'media')

    # Process SOURCE tags in media/picture elements
    for tag in soup.find_all('source'):
        parent = tag.parent.name if tag.parent else ""
        asset_type = 'media' if parent in {"video", "audio"} else 'images'
        rewrite_attribute(tag, 'src', asset_type)
        rewrite_srcset_attribute(tag, 'srcset', asset_type)
        rewrite_attribute(tag, 'data-src', asset_type)
        rewrite_srcset_attribute(tag, 'data-srcset', asset_type)

    # Process meta tags with asset URLs
    for tag in soup.find_all('meta', content=True):
        content_value = tag['content']
        normalized = normalize_asset_url(content_value)
        if is_webflow_asset_url(normalized):
            local_path = local_asset_path('images', normalized)
            if local_path:
                tag['content'] = local_path

    # Format and unminify the HTML
    formatted_html = soup.prettify()

    output_file = os.path.join(os.path.dirname(file), os.path.basename(file))
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(str(formatted_html))

    logger.debug("Processed %s", file)

def process_css(file_path, output_folder):
    """Process the CSS file to fix asset links."""

    if not os.path.exists(file_path):
        logger.error("CSS folder does not exist: %s", file_path)
        return

    with open(file_path, 'r+', encoding='utf-8') as f:
        content = f.read()
        logger.info("Processing CSS file: %s", file_path)

        # Find all image URLs in the CSS content
        raw_urls = set(re.findall(r'https?:\/\/[^\s"\')]+|\/\/[^\s"\')]+', content))
        assets_map = {}
        downloaded = set()
        css_dir = os.path.dirname(file_path)

        if raw_urls:
            logger.info("Found %d asset URLs in CSS file %s", len(raw_urls), file_path)

        for raw_url in raw_urls:
            normalized_url = normalize_asset_url(raw_url)
            if not is_webflow_asset_url(normalized_url):
                continue

            parsed = urlparse(normalized_url)
            asset_name = os.path.basename(parsed.path)
            if not asset_name:
                continue

            assets_map[raw_url] = asset_name
            image_output_path = os.path.join(output_folder, "images", asset_name)
            relative_path = os.path.relpath(image_output_path, css_dir).replace(os.sep, "/")

            if normalized_url not in downloaded and not os.path.exists(image_output_path):
                try:
                    response = requests.get(normalized_url, stream=True, timeout=10)
                    response.raise_for_status()
                    os.makedirs(os.path.dirname(image_output_path), exist_ok=True)
                    with open(image_output_path, 'wb') as img_file:
                        for chunk in response.iter_content(chunk_size=8192):
                            img_file.write(chunk)
                    logger.info("Downloaded image: %s", normalized_url)
                except requests.RequestException as e:
                    logger.error("Failed to download image %s: %s", normalized_url, e)
                downloaded.add(normalized_url)

            assets_map[raw_url] = relative_path

        # Replace CDN URLs with local paths for images
        updated_content = content
        for original_url, relative_path in assets_map.items():
            updated_content = updated_content.replace(original_url, relative_path)
        f.seek(0)
        f.write(updated_content)
        f.truncate()

def remove_badge_from_output(output_path):
    """Remove Webflow badge from the HTML files by modifying the JS files."""
    js_folder = os.path.join(os.getcwd(), output_path, "js")
    if not os.path.exists(js_folder):
        return

    for root, _, files in os.walk(js_folder):
        for file in files:
            if file.endswith(".js"):
                file_path = os.path.join(root, file)
                with open(file_path, 'r+', encoding='utf-8') as f:
                    content = f.read()
                    if content.find('class="w-webflow-badge"') != -1:
                        logger.info("\nRemoving Webflow badge from %s", file_path)
                        content = content.replace(r'/\.webflow\.io$/i.test(h)', 'false')
                        content = content.replace('if(a){i&&e.remove();', 'if(true){i&&e.remove();')
                        f.seek(0)
                        f.write(content)
                        f.truncate()

def generate_sitemap(output_path, html_sites):
    """Generate a sitemap.xml file from the HTML files."""
    sitemap_path = os.path.join(output_path, "sitemap.xml")
    with open(sitemap_path, 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap-image/1.1">\n')
        for url in html_sites["html"]:
            f.write('  <url>\n')
            f.write(f'    <loc>{url}</loc>\n')
            current_date = datetime.now().strftime("%Y-%m-%d")
            f.write(f'    <lastmod>{current_date}</lastmod>\n')
            f.write('  </url>\n')
        f.write('</urlset>\n')

    logger.info("Sitemap generated at %s", sitemap_path)

if __name__ == "__main__":
    main()
