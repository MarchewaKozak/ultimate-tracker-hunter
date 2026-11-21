
import re
from urllib.parse import urlparse, unquote

TRACKER_REGEX = re.compile(
    r'(?i)\b(?:udp|https?|wss?|ws)://'
    r'(?:\[[0-9a-f:]+\]|[a-z0-9._-]+)'
    r'(?::\d{1,5})?'
    r'/announce(?:\?[^\s<>"\'\]\[{}]*)?'
)

MAGNET_REGEX = re.compile(
    r'(?i)magnet:\?[^\s<>"\']+'
)

SUPPORTED_SCHEMES = {
    "http", "https", "udp", "ws", "wss"
}


def normalize_tracker(url):
    url = unquote(url.strip())
    url = url.rstrip(".,;)]}")

    try:
        parsed = urlparse(url)

        if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
            return None

        if not parsed.hostname or not parsed.path.startswith("/announce"):
            return None

        if parsed.username or parsed.password:
            return None

        if parsed.port is not None:
            if not 1 <= parsed.port <= 65535:
                return None

        return url

    except (ValueError, UnicodeError):
        return None


def extract_from_text(content):
    found = set()

    for match in TRACKER_REGEX.findall(content):
        tracker = normalize_tracker(match)

        if tracker:
            found.add(tracker)

    return found


def extract_from_magnets(content):
    found = set()

    for magnet in MAGNET_REGEX.findall(content):
        decoded = unquote(magnet)

        for match in TRACKER_REGEX.findall(decoded):
            tracker = normalize_tracker(match)

            if tracker:
                found.add(tracker)

    return found


def extract_everything(content):
    if isinstance(content, bytes):
        content = content.decode(
            "utf-8",
            errors="ignore"
        )

    trackers = set()

    trackers.update(extract_from_text(content))
    trackers.update(extract_from_magnets(content))

    return trackers

