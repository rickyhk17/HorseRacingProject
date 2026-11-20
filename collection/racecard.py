"""Opt-in HKJC PDF capture for manual review, not racing-data import.

``collect_racecard(url, *, terms_reviewed=False)`` returns the staging service's
``(observation, created)`` tuple, including on failure. Pass the literal True
only after reviewing source terms and authorizing collection.
Robots permission alone is not authorization. The collector makes at most two
requests per invocation, with process-local spacing; callers must not overlap
collector processes. The PDF extractor requires the separately installed pypdf.
"""
import importlib.util
import json
import re
import site
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlsplit

from django.utils import timezone

from .fetch import (
    MAX_PAGE, MAX_ROBOTS, FetchError, get_shared_fetcher, _robots_allowed, _robots_delay,
)
from .services import record_failure, save_observation


HOST = "racing.hkjc.com"
BASE_URL = "https://racing.hkjc.com/racing/content/PDF/RaceCard/"
KIND = "hkjc-racecard-pdf"
PDF_TIMEOUT = 15
MAX_PAGES = 40
MAX_TEXT_BYTES = 30000
_PATH = re.compile(r"/racing/content/PDF/RaceCard/(\d{8})_starter_all(?:_chi)?\.pdf\Z")
# Run the parser in a separate, resource-bounded interpreter. Only bounded JSON
# is emitted, so malformed PDFs cannot expand the parent's output indefinitely.
_EXTRACTOR = r"""
import io, json, resource, sys
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
if sys.argv[1]:
    sys.path.append(sys.argv[1])
from pypdf import PdfReader
try:
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=True)
    if reader.is_encrypted:
        raise ValueError("encrypted")
    total = len(reader.pages)
    pages = []
    remaining = 30000
    truncated = False
    for i in range(min(total, 40)):
        text = reader.pages[i].extract_text() or ""
        # Account for JSON escaping as well as UTF-8 bytes.
        size = lambda value: len(json.dumps(value, ensure_ascii=False).encode("utf-8")) - 2
        if size(text) > remaining:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if size(text[:middle]) <= remaining:
                    low = middle
                else:
                    high = middle - 1
            text = text[:low]
            truncated = True
        pages.append({"page": i + 1, "text": text})
        remaining -= size(text)
        if truncated or remaining == 0:
            truncated = truncated or i + 1 < total
            break
    print(json.dumps({"pages": pages, "total_pages": total,
                      "text_truncated": truncated}, ensure_ascii=True))
except Exception:
    sys.exit(1)
"""


def _validate_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise FetchError("Unsupported URL.")
    try:
        parts = urlsplit(url)
        if (url != "https://" + HOST + parts.path or parts.scheme != "https"
                or parts.netloc != HOST):
            raise FetchError("Unsupported URL.")
        match = _PATH.fullmatch(parts.path)
        if not match:
            raise FetchError("Unsupported URL.")
        datetime.strptime(match.group(1), "%Y%m%d")
    except ValueError:
        raise FetchError("Unsupported URL.") from None
    return parts.path


def _fetch_pdf(fetcher, path):
    content_type, robots = fetcher._request(HOST, "/robots.txt", MAX_ROBOTS)
    if content_type.split(";", 1)[0].strip() != "text/plain":
        raise FetchError("Robots response is not plain text.")
    try:
        robots = robots.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FetchError("Robots response is not UTF-8.") from None
    fetcher.interval = max(fetcher.interval, _robots_delay(robots))
    if not _robots_allowed(robots, path):
        raise FetchError("robots.txt disallows this URL.")
    content_type, data = fetcher._request(HOST, path, MAX_PAGE)
    if content_type.split(";", 1)[0].strip() != "application/pdf" or not data.startswith(b"%PDF-"):
        raise FetchError("Response is not a PDF.")
    return data


def _extract_pdf(data):
    if sys.platform != "linux" or importlib.util.find_spec("pypdf") is None:
        raise ValueError("PDF extractor unavailable.")
    try:
        process = subprocess.run(
            [sys.executable, "-I", "-c", _EXTRACTOR,
             site.getusersitepackages() if site.ENABLE_USER_SITE else ""], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PDF_TIMEOUT,
            check=True,
        )
        return json.loads(process.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("PDF extraction failed.") from None


def _error_code(error):
    message = str(error).lower()
    if any(word in message for word in ("robots", "proxy", "non-public")):
        return "access_denied"
    if "timed out" in message:
        return "timeout"
    if "size limit" in message:
        return "too_large"
    if "status" in message:
        return "http_error"
    if any(word in message for word in ("network", "tls", "dns")):
        return "network_error"
    return "invalid_response"


def collect_racecard(url, *, terms_reviewed=False):
    """Fetch one approved dated starter-all PDF and stage an unverified capture.

    URL format: https://racing.hkjc.com/racing/content/PDF/RaceCard/
    YYYYMMDD_starter_all_chi.pdf or YYYYMMDD_starter_all.pdf, with a real date
    and no query, port, or fragment.
    PDFs are limited to 1 MiB, 40 pages and 30,000 UTF-8 text bytes; extraction
    times out after 15 seconds. The text budget also counts JSON escaping.
    Text is not OCR, verified, or mapped to fields; success is text capture
    only, not a successfully parsed or complete racecard.
    """
    now = timezone.now()
    try:
        path = _validate_url(url)
    except FetchError:
        return record_failure(BASE_URL, KIND, "invalid_response", now)
    if terms_reviewed is not True:
        return record_failure(url, KIND, "not_authorized", now)
    if sys.platform != "linux" or importlib.util.find_spec("pypdf") is None:
        return record_failure(url, KIND, "parse_error", now)
    try:
        fetcher = get_shared_fetcher("hkjc-racecard")
        with fetcher.lock:
            pdf = _fetch_pdf(fetcher, path)
    except FetchError as exc:
        return record_failure(url, KIND, _error_code(exc), timezone.now())
    try:
        result = _extract_pdf(pdf)
        if not result["pages"] or not any(page["text"].strip() for page in result["pages"]):
            raise ValueError("No extractable text.")
    except (ValueError, KeyError, TypeError):
        return record_failure(url, KIND, "parse_error", timezone.now())
    payload = {
        **result,
        "review_required": True,
        "extraction_level": "raw_text_only",
        "limitations": [
            "Unverified extracted PDF text; reading order, spacing and character mapping may be incorrect.",
            "No OCR; images, scanned text, graphics and some fonts may be missing.",
            "No racing fields, missing entries, withdrawals or later updates are inferred or verified.",
            "At most 40 pages and 30000 JSON-encoded text bytes are extracted.",
            "The 1 MiB PDF limit may exclude whole-day racecards; the live size is unknown.",
        ],
        "missing_information": [
            "Structured meeting, race and runner fields have not been parsed.",
            "Completeness, accuracy, withdrawals and subsequent changes are unknown.",
        ],
        "empty_text_pages": [page["page"] for page in result["pages"] if not page["text"].strip()],
        "pages_truncated": len(result["pages"]) < result["total_pages"],
    }
    return save_observation(url, KIND, payload, timezone.now())
