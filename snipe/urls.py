"""URL canonicalization, shared by the scanner and the database's dedup migration."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_src$|fbclid$|gclid$|trk$|trackingId$)")
# in.linkedin.com, uk.linkedin.com, ... serve the same page as www.linkedin.com
LINKEDIN_REGIONAL_HOST = re.compile(r"([a-z]{2,3}\.)?linkedin\.com")


def normalize_url(url: str) -> str:
    """Canonical form of a URL for deduplication (no tracking params, fragment, or trailing slash)."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    host = parts.netloc.lower()
    if LINKEDIN_REGIONAL_HOST.fullmatch(host):
        host = "www.linkedin.com"
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/", query, ""))
