"""Read public web information without creating a browser session."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from email.message import Message
from functools import cache
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


_USER_AGENT = "OSIA Public Web Research/1.0"
_SEARCH_BYTES = 512 * 1024
_PAGE_BYTES = 2 * 1024 * 1024
_PAGE_CHARS = 8_000
_HTTP_REDIRECTS = {301, 302, 303, 307, 308}
_RSS_TYPES = ("application/rss+xml", "application/xml", "text/xml")
_PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/json")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class _PublicWebError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Response:
    content_type: str = ""
    charset: str | None = None
    body: bytes = b""
    truncated: bool = False
    redirect: str | None = None


def _public_address(address: str) -> bool:
    if "%" in address:
        return False
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.is_site_local or ip.sixtofour is not None or ip.teredo is not None:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _public_url(url: str) -> str:
    """Normalize a public HTTP(S) URL without resolving it."""
    if len(url) > 8192 or any(ch.isspace() or ord(ch) < 32 for ch in url) or "\\" in url:
        raise ValueError("invalid URL")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.username is not None or parts.password is not None:
        raise ValueError("public HTTP(S) URL required")
    if parts.port not in {None, 443 if parts.scheme == "https" else 80}:
        raise ValueError("standard HTTP(S) port required")
    host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (
            len(labels) < 2 or len(host) > 253 or host.endswith(_LOCAL_SUFFIXES)
            or not all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
        ):
            raise ValueError("public hostname required") from None
    else:
        if not _public_address(host):
            raise ValueError("public address required")
        if literal.version == 6:
            host = f"[{host}]"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    return urlunsplit((parts.scheme, host, path, query, ""))


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # A trailing dot prevents the OS resolver from trying LAN search suffixes.
        addresses = tuple(dict.fromkeys(
            entry[4][0] for entry in socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM)
        ))
    else:
        addresses = (host,)
    if not addresses or not all(_public_address(address) for address in addresses):
        raise _PublicWebError("public_address_required", "The address must resolve to the public Internet.")
    return addresses


@cache
def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise _PublicWebError("public_web_timeout", "The public web request timed out.")
    return seconds


def _request_once(url: str, accept: tuple[str, ...], limit: int, deadline: float) -> _Response:
    parts = urlsplit(url)
    host = parts.hostname
    port = 443 if parts.scheme == "https" else 80
    addresses = _resolve_public_addresses(host, port)
    target = urlunsplit(("", "", parts.path, parts.query, ""))
    for address in addresses:
        connection = http.client.HTTPConnection(host, port)
        try:
            # Connect to the checked IP. HTTP Host and TLS SNI still use the
            # original hostname, so it cannot be resolved a second time.
            connection.sock = socket.create_connection((address, port), min(4.0, _remaining(deadline)))
            if parts.scheme == "https":
                connection.sock = _tls_context().wrap_socket(connection.sock, server_hostname=host)
            connection.sock.settimeout(min(5.0, _remaining(deadline)))
            connection.request("GET", target, headers={
                "Host": parts.netloc,
                "User-Agent": _USER_AGENT,
                "Accept": ", ".join(accept),
                "Accept-Encoding": "identity",
            })
            with connection.getresponse() as response:
                if response.status in _HTTP_REDIRECTS:
                    destination = response.getheader("Location")
                    if not destination:
                        raise _PublicWebError("public_web_redirect_failed", "The public redirect had no destination.")
                    return _Response(redirect=destination)
                if response.status != 200:
                    raise _PublicWebError("public_web_http_error", f"The public server returned HTTP {response.status}.")
                content_type = response.headers.get_content_type() if response.getheader("Content-Type") else ""
                if content_type not in accept:
                    raise _PublicWebError("public_web_unsupported_content", "The response is not supported public text.")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise _PublicWebError("public_web_unsupported_content", "The public response uses an unsupported encoding.")
                data = bytearray()
                while len(data) <= limit:
                    _remaining(deadline)
                    chunk = response.read1(min(32_768, limit + 1 - len(data)))
                    if not chunk:
                        if response.length not in {None, 0}:
                            raise http.client.IncompleteRead(bytes(data), response.length)
                        break
                    data.extend(chunk)
                return _Response(
                    content_type=content_type,
                    charset=response.headers.get_content_charset(),
                    body=bytes(data[:limit]),
                    truncated=len(data) > limit,
                )
        except (OSError, http.client.HTTPException):
            # A different checked address may still be reachable.
            continue
        finally:
            connection.close()
    raise _PublicWebError("public_web_connection_failed", "The public server could not be reached.")


def _get(url: str, accept: tuple[str, ...], limit: int, deadline: float) -> tuple[str, _Response]:
    for _ in range(6):
        try:
            url = _public_url(url)
        except ValueError:
            raise _PublicWebError("public_url_required", "Use a public HTTP or HTTPS URL without credentials.") from None
        try:
            response = _request_once(url, accept, limit, deadline)
        except OSError:
            raise _PublicWebError("public_web_connection_failed", "The public server could not be reached.") from None
        if response.redirect is None:
            return url, response
        try:
            url = urljoin(url, response.redirect)
        except ValueError:
            break
    raise _PublicWebError("public_web_redirect_failed", "The public page did not resolve to a readable destination.")


class _HTMLCharset(HTMLParser):
    def __init__(self):
        super().__init__()
        self.charset = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.charset:
            return
        attributes = dict(attrs)
        self.charset = attributes.get("charset")
        if not self.charset and (attributes.get("http-equiv") or "").casefold() == "content-type":
            header = Message()
            header["Content-Type"] = attributes.get("content") or ""
            self.charset = header.get_content_charset()


def _decode(data: bytes, charset: str | None, *, html: bool = False) -> str:
    for marker, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(marker):
            return data.decode(encoding, errors="replace")
    declared = [charset] if charset else []
    if html and not charset:
        parser = _HTMLCharset()
        try:
            parser.feed(data[:4096].decode("latin-1"))
        except AssertionError:
            # HTMLParser rejects some malformed marked declarations.
            pass
        if parser.charset:
            declared.append(parser.charset)
    for label in declared:
        try:
            encoding = codecs.lookup(label.strip()).name
            # HTML's legacy Latin-1 and ASCII labels decode as Windows-1252.
            if html and encoding in {"iso8859-1", "ascii"}:
                encoding = "cp1252"
            return data.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


class _SearchResults(HTMLParser):
    """Parse the result links and snippets from DuckDuckGo's HTML version."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._field = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "result__a" in classes:
            href = attributes.get("href") or ""
            try:
                target = urlsplit(urljoin("https://duckduckgo.com", href))
            except ValueError:
                return
            if target.hostname == "duckduckgo.com" and target.path == "/l/":
                href = parse_qs(target.query).get("uddg", [""])[0]
            self.results.append({"title": "", "url": href, "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "a":
            self._field = None

    def handle_data(self, data):
        if self._field:
            self.results[-1][self._field] += data


class _PageText(HTMLParser):
    _IGNORED = {"head", "script", "style", "noscript", "template", "svg", "nav", "footer"}
    _BLOCKS = {"br", "p", "div", "li", "section", "article", "main", "pre", "tr",
               "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.title = ""
        self._ignored = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORED:
            self._ignored.append(tag)
        elif tag == "title" and self._ignored == ["head"]:
            self._in_title = True
        elif tag in self._BLOCKS and not self._ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self._ignored and tag == self._ignored[-1]:
            self._ignored.pop()
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCKS and not self._ignored:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._ignored:
            self.parts.append(data)


def _bing_results(data: bytes) -> list[dict]:
    # Search feeds never need document types or entities.
    if b"<!DOCTYPE" in data.upper():
        raise ValueError("invalid search feed")
    feed = ET.fromstring(data)
    if feed.tag != "rss":
        raise ValueError("invalid search feed")
    return [
        {"title": item.findtext("title", ""), "url": item.findtext("link", ""),
         "snippet": item.findtext("description", "")}
        for item in feed.findall("./channel/item")
    ]


def _format_results(rows: list[dict]) -> list[dict]:
    results = []
    seen = set()
    for row in rows:
        try:
            url = _public_url(row["url"].strip())
        except ValueError:
            continue
        title = " ".join(row["title"].split())[:240]
        if title and url not in seen and len(url) <= 2048:
            seen.add(url)
            results.append({"title": title, "url": url, "snippet": " ".join(row["snippet"].split())[:600]})
        if len(results) >= 8:
            break
    return results


def _failure(error: _PublicWebError) -> dict:
    return {"ok": False, "text": f"ERROR: {error}", "error": {"code": error.code}}


def search_public_web(query: str) -> dict:
    """Get public search results through HTTP, without browser state."""
    query = " ".join(query.split())
    if not query:
        return _failure(_PublicWebError("empty_search_query", "Provide a public search query."))
    deadline = time.monotonic() + 18.0
    had_response = False
    for source, endpoint, params, accept in (
        ("Bing", "https://www.bing.com/search", {"q": query, "format": "rss"}, _RSS_TYPES),
        ("DuckDuckGo", "https://html.duckduckgo.com/html/", {"q": query}, ("text/html",)),
    ):
        try:
            _, response = _get(
                endpoint + "?" + urlencode(params), accept, _SEARCH_BYTES,
                min(deadline, time.monotonic() + 9.0),
            )
            if response.truncated:
                continue
            if source == "Bing":
                rows = _bing_results(response.body)
            else:
                parser = _SearchResults()
                parser.feed(_decode(response.body, response.charset, html=True))
                rows = parser.results
            had_response = True
            results = _format_results(rows)
        except (_PublicWebError, ET.ParseError, ValueError, AssertionError):
            continue
        if results:
            entries = [
                f"{index}. {row['title']}\nURL: {row['url']}\n{row['snippet']}"
                for index, row in enumerate(results, start=1)
            ]
            return {
                "ok": True, "source": source, "results": results,
                "text": f"Public search results from {source}.\n\n" + "\n\n".join(entries),
            }
    if had_response:
        return _failure(_PublicWebError("public_search_no_results", "No usable public results were returned. Try a more specific query."))
    return _failure(_PublicWebError("public_search_unavailable", "The direct public search services did not return usable results."))


def read_public_web_page(url: str, offset: int = 0) -> dict:
    """Read bounded text from a public page without scripts or a browser."""
    try:
        final_url, response = _get(
            url.strip(), _PAGE_TYPES, _PAGE_BYTES, time.monotonic() + 18.0,
        )
    except _PublicWebError as error:
        return _failure(error)
    is_html = response.content_type in {"text/html", "application/xhtml+xml"}
    content = _decode(response.body, response.charset, html=is_html)
    title = ""
    if is_html:
        parser = _PageText()
        try:
            parser.feed(content)
        except AssertionError:
            return _failure(_PublicWebError(
                "public_page_invalid_html", "The public response could not be parsed as HTML.",
            ))
        content = "".join(parser.parts)
        title = " ".join(parser.title.split())[:240]
    text = "\n".join(line for raw in content.splitlines() if (line := " ".join(raw.split())))
    if not text:
        return _failure(_PublicWebError("public_page_no_text", "The public response contained no readable text."))
    if offset >= len(text):
        return _failure(_PublicWebError("public_page_offset_unavailable", "The requested offset is beyond the readable text."))
    end = min(len(text), offset + _PAGE_CHARS)
    next_offset = end if end < len(text) else None
    result = f"Public page: {title or final_url}\nURL: {final_url}\n\n{text[offset:end]}"
    if next_offset is not None:
        result += f"\n\nMore text available at offset {next_offset}."
    if response.truncated:
        result += "\nThe downloaded response was truncated."
    return {
        "ok": True, "url": final_url, "title": title, "text": result,
        "next_offset": next_offset, "truncated": response.truncated,
    }
