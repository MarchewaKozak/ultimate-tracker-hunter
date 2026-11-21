
import asyncio
import ipaddress
import json
import os
import re
import socket
import time

from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag, unquote

import aiohttp
from bs4 import BeautifulSoup

from scanner.tracker_extractor import extract_everything


# ==========================================
# CONFIGURATION
# ==========================================

OUTPUT = "trackers.txt"

MAX_PAGES = 5000
MAX_DEPTH = 5
CONCURRENCY = 5

REQUEST_TIMEOUT = 20
DOMAIN_DELAY = 2

PUBLISH_INTERVAL = 90
SESSION_DURATION = 340 * 60

MAX_CONTENT_SIZE = 2 * 1024 * 1024

HEADERS = {
    "User-Agent": "UltimateTrackerHunter/1.0"
}


# ==========================================
# DATABASE
# ==========================================

trackers = set()
seen_pages = set()
domain_times = {}

queue = deque()

last_publish = time.monotonic()
start_time = time.monotonic()


def load_database():

    if not os.path.exists(OUTPUT):
        return

    with open(OUTPUT, encoding="utf-8") as file:

        for line in file:

            value = line.strip()

            if valid_tracker(value):
                trackers.add(value)

    print(f"[DATABASE] Loaded {len(trackers)} trackers")


def save_database(force=False):

    global last_publish

    now = time.monotonic()

    if not force and now - last_publish < PUBLISH_INTERVAL:
        return

    existing = set()

    if os.path.exists(OUTPUT):

        with open(OUTPUT, encoding="utf-8") as file:

            existing = {
                line.strip()
                for line in file
                if line.strip()
            }

    combined = existing | trackers

    temporary = OUTPUT + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:

        for tracker in sorted(combined):
            file.write(tracker + "\n")

    os.replace(temporary, OUTPUT)

    trackers.clear()
    trackers.update(combined)

    last_publish = now

    print(
        f"[DATABASE] Published {len(combined)} unique trackers"
    )


# ==========================================
# TRACKER VALIDATION
# ==========================================

TRACKER_PATTERN = re.compile(
    r'(?i)(?:udp|https?|wss?|ws)://'
    r'(?:\[[0-9a-f:]+\]|[a-z0-9._-]+)'
    r'(?::\d{1,5})?'
    r'/announce(?:\?[^\s<>"\']*)?'
)


def valid_tracker(url):

    try:

        parsed = urlparse(url)

        if parsed.scheme.lower() not in (
            "http",
            "https",
            "udp",
            "ws",
            "wss"
        ):
            return False

        if not parsed.hostname:
            return False

        if not parsed.path.startswith("/announce"):
            return False

        if parsed.username or parsed.password:
            return False

        if parsed.port is not None:
            if not 1 <= parsed.port <= 65535:
                return False

        return True

    except ValueError:
        return False


def extract_trackers(content):

    if isinstance(content, bytes):

        content = content.decode(
            "utf-8",
            errors="ignore"
        )

    found = extract_everything(content)

    for match in TRACKER_PATTERN.findall(content):

        candidate = match.rstrip(".,;)]}")

        if valid_tracker(candidate):
            found.add(candidate)

    before = len(trackers)

    trackers.update(found)

    discovered = len(trackers) - before

    if discovered:
        print(f"[DISCOVERY] +{discovered} new trackers")


# ==========================================
# NETWORK SAFETY
# ==========================================

def public_host(host):

    if not host:
        return False

    try:

        results = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM
        )

        for result in results:

            address = ipaddress.ip_address(
                result[4][0]
            )

            if address.is_global:
                return True

        return False

    except (ValueError, OSError):
        return False


def valid_page(url):

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
        )

    except ValueError:
        return False


# ==========================================
# WEBSITE REQUESTS
# ==========================================

async def fetch(session, url):

    if not valid_page(url):
        return None

    host = urlparse(url).hostname

    if not public_host(host):
        return None

    now = time.monotonic()

    previous = domain_times.get(host, 0)

    wait = DOMAIN_DELAY - (now - previous)

    if wait > 0:
        await asyncio.sleep(wait)

    domain_times[host] = time.monotonic()

    try:

        async with session.get(
            url,
            allow_redirects=True,
            max_redirects=5
        ) as response:

            if response.status != 200:
                return None

            if response.content_length and (
                response.content_length > MAX_CONTENT_SIZE
            ):
                return None

            body = await response.content.read(
                MAX_CONTENT_SIZE
            )

            return body.decode(
                "utf-8",
                errors="ignore"
            )

    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        OSError
    ):

        return None


# ==========================================
# LINK DISCOVERY
# ==========================================

def extract_links(content, base_url):

    soup = BeautifulSoup(
        content,
        "html.parser"
    )

    links = set()

    for element in soup.find_all(
        ["a", "link", "iframe"]
    ):

        href = element.get("href") or element.get("src")

        if not href:
            continue

        absolute = urljoin(base_url, href)

        absolute = urldefrag(absolute)[0]

        if valid_page(absolute):
            links.add(absolute)

    return links


# ==========================================
# CRAWLER
# ==========================================

async def crawl(session, url, depth):

    if url in seen_pages:
        return

    if len(seen_pages) >= MAX_PAGES:
        return

    if time.monotonic() - start_time >= SESSION_DURATION:
        return

    seen_pages.add(url)

    content = await fetch(session, url)

    if content is None:
        return

    extract_trackers(content)

    if depth < MAX_DEPTH:

        links = extract_links(
            content,
            url
        )

        for link in links:

            if link not in seen_pages:
                queue.append((link, depth + 1))

    if len(seen_pages) % 25 == 0:

        print(
            f"[CRAWLER] Pages: {len(seen_pages)} "
            f"Trackers: {len(trackers)} "
            f"Queue: {len(queue)}"
        )

    save_database()


# ==========================================
# GITHUB DISCOVERY
# ==========================================

async def github_discovery(session):

    queries = [
        "bittorrent tracker",
        "public torrent trackers",
        "tracker announce URLs",
        "bittorrent announce",
        "tracker list",
        "torrent tracker database"
    ]

    token = os.environ.get("GITHUB_TOKEN")

    headers = {}

    if token:

        headers["Authorization"] = (
            f"Bearer {token}"
        )

    for query in queries:

        if time.monotonic() - start_time >= SESSION_DURATION:
            break

        url = (
            "https://api.github.com/search/repositories"
            f"?q={query.replace(' ', '+')}"
            "&sort=updated&per_page=30"
        )

        try:

            async with session.get(
                url,
                headers=headers
            ) as response:

                if response.status != 200:
                    continue

                data = await response.json()

                for repo in data.get("items", []):

                    repository_url = repo.get("html_url")

                    if repository_url:
                        queue.append(
                            (repository_url, 0)
                        )

                    extract_trackers(
                        repo.get("description") or ""
                    )

                save_database()

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError
        ):

            continue


# ==========================================
# WORKERS
# ==========================================

async def worker(session):

    while queue:

        if time.monotonic() - start_time >= SESSION_DURATION:
            return

        url, depth = queue.popleft()

        await crawl(
            session,
            url,
            depth
        )


# ==========================================
# MAIN
# ==========================================

async def main():

    load_database()

    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT
    )

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY,
        ttl_dns_cache=300
    )

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout,
        connector=connector
    ) as session:

        print("=" * 50)
        print("ULTIMATE TRACKER HUNTER")
        print("DISCOVERY ENGINE STARTED")
        print("=" * 50)

        await github_discovery(session)

        while queue:

            if time.monotonic() - start_time >= SESSION_DURATION:
                break

            tasks = []

            for _ in range(
                min(CONCURRENCY, len(queue))
            ):

                tasks.append(
                    worker(session)
                )

            if not tasks:
                break

            await asyncio.gather(*tasks)

            save_database()

        save_database(force=True)

        print("=" * 50)
        print("SESSION FINISHED")
        print(f"Pages visited: {len(seen_pages)}")
        print(f"Trackers discovered: {len(trackers)}")
        print("=" * 50)


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        save_database(force=True)

        print("Scanner interrupted safely.")
