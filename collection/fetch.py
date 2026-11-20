"""Restricted, opt-in public page retrieval. Robots permission is not a licence."""
import http.client
import ipaddress
import math
import os
import queue
import re
import socket
import ssl
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit


ORIGINS = {
    "hkjc-racecard": "racing.hkjc.com",
    "hkjc-results": "racing.hkjc.com",
    "hkjc-resultsall": "racing.hkjc.com",
    "oncc": "racing.on.cc",
    "horseracinghk": "horseracinghk.com",
}
USER_AGENT = "HorseRacingProjectCollector"
TIMEOUT = 10
MAX_PAGE = 1024 * 1024
MAX_ROBOTS = 256 * 1024


class FetchError(Exception):
    """A safe, non-sensitive failure message suitable for an observation."""


def _normalize_robot_octets(value):
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    result = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            encoded = value[index + 1:index + 3]
            if len(encoded) != 2 or not re.fullmatch(r"[0-9A-Fa-f]{2}", encoded):
                raise FetchError("Malformed percent encoding.")
            decoded = chr(int(encoded, 16))
            result.append(decoded if decoded in unreserved else "%" + encoded.upper())
            index += 3
        else:
            if ord(char) > 127:
                result.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
            else:
                result.append(char)
            index += 1
    return "".join(result)


def validate_url(url, source):
    host = ORIGINS.get(source)
    if not host or not isinstance(url, str) or len(url) > 2048:
        raise FetchError("Unsupported source or URL.")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != host or parts.port is not None
                or parts.username is not None or parts.password is not None or parts.fragment
                or any(ord(char) < 33 or ord(char) > 126 for char in url)):
            raise FetchError("URL must use the exact approved HTTPS origin without credentials or fragment.")
    except ValueError as exc:
        raise FetchError("Invalid source URL.") from exc
    _normalize_robot_octets(parts.path + ("?" + parts.query if parts.query else ""))
    if source == "hkjc-resultsall":
        if parts.path not in {
            "/zh-hk/local/information/resultsall",
            "/en-us/local/information/resultsall",
            "/racing/information/Chinese/Racing/ResultsAll.aspx",
        }:
            raise FetchError("Unsupported HKJC page path.")
        if parts.query:
            try:
                pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                params = {key.lower(): value for key, value in pairs}
                if (len(parts.query) > 100 or len(pairs) != len(params)
                        or set(params) != {"racedate", "racecourse"}
                        or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", params["racedate"])
                        or params["racecourse"] not in {"ST", "HV"}):
                    raise ValueError
                datetime.strptime(params["racedate"], "%Y/%m/%d")
            except ValueError:
                raise FetchError("Invalid HKJC meeting parameters.") from None
        return parts
    expected = {
        "hkjc-racecard": "/racing/information/Chinese/racing/RaceCard.aspx",
        "hkjc-results": "/racing/information/Chinese/Racing/LocalResults.aspx",
    }.get(source)
    if expected and parts.path != expected:
        raise FetchError("Unsupported HKJC page path.")
    if source in {"oncc", "horseracinghk"} and (parts.path not in ("", "/") or parts.query):
        raise FetchError("Only the public source homepage is supported.")
    if expected and parts.query:
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
            params = dict(pairs)
            if (len(parts.query) > 100 or len(pairs) != len(params)
                    or set(params) != {"RaceDate", "Racecourse", "RaceNo"}
                    or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", params["RaceDate"])
                    or params["Racecourse"] not in {"ST", "HV"}
                    or not re.fullmatch(r"(?:[1-9]|1[0-4])", params["RaceNo"])):
                raise ValueError
            datetime.strptime(params["RaceDate"], "%Y/%m/%d")
        except ValueError:
            raise FetchError("Invalid HKJC race parameters.") from None
    return parts


def _robots_allowed(text, path):
    # Treat any matching disallow as authoritative, even if an allow also matches.
    # This is deliberately stricter than robots precedence and cannot grant extra access.
    path = _normalize_robot_octets(path)
    groups = []
    agents, rules = [], []
    saw_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise FetchError("Malformed robots.txt.")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if saw_rule:
                groups.append((agents, rules))
                agents, rules, saw_rule = [], [], False
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            if not agents:
                raise FetchError("Malformed robots.txt.")
            saw_rule = True
            if key == "disallow" and value:
                rules.append(_normalize_robot_octets(value))
    if agents:
        groups.append((agents, rules))
    if not groups:
        raise FetchError("No robots.txt user-agent rules.")
    agent = USER_AGENT.lower()
    applicable = [rules for agents, rules in groups
                  if any(a == "*" or (a and a in agent) for a in agents)]
    for rules in applicable:
        for rule in rules:
            anchored = rule.endswith("$")
            pattern = rule[:-1] if anchored else rule
            chunks = pattern.split("*")
            if not path.startswith(chunks[0]):
                continue
            position = len(chunks[0])
            matched = True
            for index, chunk in enumerate(chunks[1:], 1):
                if anchored and index == len(chunks) - 1:
                    found = len(path) - len(chunk) if path.endswith(chunk) else -1
                else:
                    found = path.find(chunk, position)
                if found < position:
                    matched = False
                    break
                position = found + len(chunk)
            if matched and (not anchored or position == len(path)):
                return False
    return True


def _robots_delay(text):
    delay = 2.0
    for raw in text.splitlines():
        key, sep, value = raw.split("#", 1)[0].partition(":")
        if not sep:
            continue
        if key.strip().lower() == "request-rate":
            raise FetchError("robots.txt request-rate requires manual review.")
        if key.strip().lower() == "crawl-delay":
            try:
                seconds = float(value.strip())
                if not math.isfinite(seconds) or not 0 <= seconds <= 60:
                    raise ValueError
                delay = max(delay, seconds)
            except ValueError:
                raise FetchError("robots.txt crawl-delay requires manual review.") from None
    return delay


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, 443, timeout=TIMEOUT, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _resolve(host):
    results = queue.Queue(maxsize=1)

    def lookup():
        try:
            results.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            results.put(None)

    threading.Thread(target=lookup, daemon=True).start()
    try:
        entries = results.get(timeout=TIMEOUT)
    except queue.Empty:
        raise FetchError("DNS timed out.") from None
    if entries is None:
        raise FetchError("DNS lookup failed.")
    return {entry[4][0] for entry in entries}


class PublicFetcher:
    def __init__(self, interval=2):
        self.interval = max(2, interval)
        self.last_request = None

    def _request(self, host, path, limit):
        try:
            # Never silently ignore an environment-configured proxy or network policy.
            if any(os.environ.get(key) for key in ("http_proxy", "https_proxy", "all_proxy",
                                                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")):
                raise FetchError("Configured proxy requires manual network-policy review.")
            addresses = _resolve(host)
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise FetchError("DNS returned a non-public or empty address.")
            conn = _PinnedHTTPSConnection(host, sorted(addresses)[0])
            issued = False
            deadline = None
            try:
                conn.connect()
                response_socket = conn.sock
                timed_out = threading.Event()
                def stop_response():
                    timed_out.set()
                    if response_socket:
                        try:
                            response_socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        conn.close()
                if self.last_request is not None:
                    time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
                deadline = threading.Timer(TIMEOUT, stop_response)
                deadline.daemon = True
                deadline.start()
                self.last_request = time.monotonic()
                issued = True
                conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
                response = conn.getresponse()
                if response.status != 200:
                    raise FetchError(f"HTTP status {response.status}; redirects are not followed.")
                content_type = response.getheader("Content-Type", "").lower()
                body = response.read(limit + 1)
                if timed_out.is_set():
                    raise FetchError("Response timed out.")
                if len(body) > limit:
                    raise FetchError("Response exceeds size limit.")
                if getattr(response, "length", 0):
                    raise FetchError("Response body was incomplete.")
                return content_type, body
            finally:
                if issued:
                    self.last_request = time.monotonic()
                if deadline is not None:
                    deadline.cancel()
                conn.close()
        except FetchError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
            raise FetchError("Network, TLS, or DNS error.") from None

    def fetch(self, source, url):
        parts = validate_url(url, source)
        host = ORIGINS[source]
        content_type, robots = self._request(host, "/robots.txt", MAX_ROBOTS)
        if not content_type.startswith("text/plain"):
            raise FetchError("Robots response is not plain text.")
        try:
            robots = robots.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FetchError("Robots response is not UTF-8.") from None
        self.interval = max(self.interval, _robots_delay(robots))
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if not _robots_allowed(robots, path):
            raise FetchError("robots.txt disallows this URL.")
        content_type, body = self._request(host, path, MAX_PAGE)
        if not content_type.startswith(("text/html", "application/xhtml+xml")):
            raise FetchError("Page response is not HTML.")
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type)
        charset = match.group(1).lower() if match else "utf-8"
        if charset not in {"utf-8", "utf8", "big5", "big5-hkscs", "gb18030", "gbk"}:
            raise FetchError("Unsupported page character encoding.")
        try:
            return body.decode(charset, errors="strict")
        except (UnicodeDecodeError, LookupError):
            raise FetchError("Page character encoding could not be decoded.") from None


class _SharedFetcher(PublicFetcher):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()

    def fetch(self, source, url):
        with self.lock:
            return super().fetch(source, url)


_shared_fetchers = {}
_shared_fetchers_lock = threading.Lock()


def get_shared_fetcher(source):
    """Return a process-local per-origin fetcher; lock custom request sequences."""
    host = ORIGINS.get(source)
    if not host:
        raise FetchError("Unsupported source.")
    with _shared_fetchers_lock:
        if host not in _shared_fetchers:
            _shared_fetchers[host] = _SharedFetcher()
        return _shared_fetchers[host]
