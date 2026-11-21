
import asyncio
import ipaddress
import os
import re
import socket
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

import aiohttp
from bs4 import BeautifulSoup

OUTPUT = "trackers.txt"

MAX_PAGES = 2000
MAX_DEPTH = 3
CONCURRENCY = 5
REQUEST_TIMEOUT = 15
DOMAIN_DELAY = 2

TRACKER_PATTERN = re.compile(
    r'(?:(?:https?|udp|wss?)://)'
    r'(?:\[[0-9a-fA-F:]+\]|'
    r'[a-zA-Z0-9._-]+)'
    r'(?::\d{1,5})?'
    r'/announce(?:\?[^\s<>"\']*)?',
    re.IGNORECASE
)

HEADERS = {
    "User-Agent": "UltimateTrackerHunter/1.0 (public research crawler)"
}

seen_pages = set()
trackers = set()
domain_times = {}
queue = deque()


def valid_public_host(host):
    if not host:
        return False

    try:
        addresses = socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )

        return any(
            ipaddress.ip_address(item[4][0]).is_global
            for item in addresses
        )

    except (ValueError, OSError):
        return False


def valid_page_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        if not parsed.hostname:
            return False

        if parsed.username or parsed.password:
            return False

        if parsed.port and not 1 <= parsed.port <= 65535:
            return False

        return True

    except ValueError:
        return False


def extract_trackers(content):
    for match in TRACKER_PATTERN.findall(content):
        tracker = match.rstrip(".,;)]}")

        if valid_tracker(tracker):
            trackers.add(tracker)


def valid_tracker(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme.lower() in
            ("http", "https", "udp", "ws", "wss")
            and bool(parsed.hostname)
            and parsed.path.startswith("/announce")
            and not parsed.username
            and not parsed.password
            and (
                parsed.port is None
                or 1 <= parsed.port <= 65535
            )
        )

    except ValueError:
        return False


def save_trackers():
    existing = set()

    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as file:
            existing = {
                line.strip()
                for line in file
                if line.strip()
            }

    combined = existing | trackers

    with open(OUTPUT, "w", encoding="utf-8") as file:
        for tracker in sorted(combined):
            file.write(tracker + "\n")

    print(f"[DATABASE] {len(combined)} unique trackers")


async def fetch(session, url):
    host = urlparse(url).hostname

    if not valid_page_url(url):
        return None

    if not valid_public_host(host):
        return None

    now = time.monotonic()
    previous = domain_times.get(host, 0)

    delay = DOMAIN_DELAY - (now - previous)

    if delay > 0:
        await asyncio.sleep(delay)

    domain_times[host] = time.monotonic()

    try:
        async with session.get(
            url,
            allow_redirects=True,
            max_redirects=5
        ) as response:

            if response.status != 200:
                return None

            if response.content_type not in (
                "text/html",
                "text/plain",
                "application/json",
                "application/xml",
                "text/xml"
            ):
                return None

            body = await response.content.read(2 * 1024 * 1024)

            return body.decode("utf-8", errors="ignore")

    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


def extract_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    links = set()

    for element in soup.find_all(["a", "link"]):
        href = element.get("href")

        if not href:
            continue

        absolute = urljoin(base_url, href)
        absolute = urldefrag(absolute)[0]

        if valid_page_url(absolute):
            links.add(absolute)

    return links


async def crawl(session, url, depth):
    if url in seen_pages:
        return

    if len(seen_pages) >= MAX_PAGES:
        return

    seen_pages.add(url)

    html = await fetch(session, url)

    if not html:
        return

    extract_trackers(html)

    if depth < MAX_DEPTH:
        for link in extract_links(html, url):
            if link not in seen_pages:
                queue.append((link, depth + 1))

    if len(seen_pages) % 25 == 0:
        print(
            f"[CRAWLER] Pages: {len(seen_pages)} "
            f"Trackers: {len(trackers)}"
        )

    save_trackers()


async def worker(session):
    while queue:
        url, depth = queue.popleft()

        await crawl(session, url, depth)


async def discover_github_repositories(session):
    queries = [
        "bittorrent tracker",
        "public tracker list",
        "torrent trackers",
        "tracker announce urls",
        "bittorrent announce"
    ]

    for query in queries:
        api = (
            "https://api.github.com/search/repositories"
            f"?q={query.replace(' ', '+')}"
            "&sort=updated&per_page=30"
        )

        try:
            async with session.get(api) as response:
                if response.status != 200:
                    continue

                data = await response.json()

                for repo in data.get("items", []):
                    url = repo.get("html_url")

                    if url:
                        queue.append((url, 0))

                    description = repo.get("description") or ""
                    extract_trackers(description)

                save_trackers()

        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue


async def main():
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

        print("ULTIMATE TRACKER HUNTER STARTED")

        await discover_github_repositories(session)

        while queue and len(seen_pages) < MAX_PAGES:
            batch = []

            for _ in range(min(CONCURRENCY, len(queue))):
                batch.append(worker(session))

            await asyncio.gather(*batch)

            save_trackers()

        print("DISCOVERY SESSION COMPLETE")
        print(f"Pages visited: {len(seen_pages)}")
        print(f"Trackers discovered: {len(trackers)}")

        save_trackers()


if __name__ == "__main__":
    asyncio.run(main())

