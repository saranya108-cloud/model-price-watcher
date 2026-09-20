"""Small synchronous HTTPS GET boundary; no provider policy or retries.

Timeouts apply to socket operations, not the total elapsed request time.
Response bytes are bounded; TLS uses the standard library's verified defaults.
"""

from __future__ import annotations

import http.client
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit


class TransportError(RuntimeError):
    """Sanitized transport failure with a stable, non-sensitive reason."""

    def __init__(self, reason: str):
        # Never echo arbitrary exception text supplied by an HTTP implementation.
        if reason not in {
            "network_error", "invalid_framing", "response_too_large",
            "incomplete_body", "unsupported_encoding",
        }:
            reason = "network_error"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HttpResponse:
    """Only acquisition-relevant response data; payloads excluded from repr."""

    status: int
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)


class HttpTransport(Protocol):
    def get(
        self, url: str, *, headers: Mapping[str, str],
        timeout_seconds: float, max_response_bytes: int,
    ) -> HttpResponse:
        """Make one GET, returning a bounded response or raising TransportError."""
        ...


def validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    """Validate bounds before any request or clock activity."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds must be a number")
    try:
        valid = math.isfinite(timeout_seconds) and timeout_seconds > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("timeout_seconds must be positive and finite")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
        raise TypeError("max_response_bytes must be an integer")
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_RESERVED_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "te",
    "trailer", "upgrade", "expect", "cookie", "cookie2", "keep-alive",
}


def _request_parts(url, headers):
    if not isinstance(url, str):
        raise TypeError("url must be a string")
    if not url or any(ord(char) <= 32 or ord(char) >= 127 for char in url) or "\\" in url or "#" in url:
        raise ValueError("invalid HTTPS URL")
    invalid = False
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        invalid = (
            parts.scheme != "https" or not host or parts.username is not None
            or parts.password is not None or parts.netloc.endswith(":")
            or port == 0
        )
    except ValueError:
        invalid = True
    if invalid:
        raise ValueError("invalid HTTPS URL")
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping")
    supplied = {}
    seen = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("header names and values must be strings")
        lower = name.lower()
        if (
            _HEADER_NAME.fullmatch(name) is None or lower in seen
            or lower in _RESERVED_HEADERS or lower.startswith("proxy-")
            or any(ord(char) < 32 or ord(char) >= 127 for char in value)
        ):
            raise ValueError("invalid or reserved request header")
        seen.add(lower)
        supplied[name] = value
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return host, port, target, supplied


def _values(headers, name):
    return [value.strip() for key, value in headers if key.lower() == name]


def _framing(headers, maximum):
    lengths = _values(headers, "content-length")
    transfer = _values(headers, "transfer-encoding")
    if transfer and (lengths or len(transfer) != 1 or transfer[0].lower() != "chunked"):
        raise TransportError("invalid_framing")
    declared = None
    if lengths:
        tokens = [part.strip() for value in lengths for part in value.split(",")]
        if any(re.fullmatch(r"[0-9]+", token) is None for token in tokens):
            raise TransportError("invalid_framing")
        canonical = [token.lstrip("0") or "0" for token in tokens]
        if len(set(canonical)) != 1:
            raise TransportError("invalid_framing")
        # Bound before int conversion, including arbitrarily long untrusted values.
        bound = str(maximum)
        value = canonical[0]
        if len(value) > len(bound) or (len(value) == len(bound) and value > bound):
            raise TransportError("response_too_large")
        declared = int(value)
    encodings = _values(headers, "content-encoding")
    if encodings and (len(encodings) != 1 or encodings[0].lower() != "identity"):
        raise TransportError("unsupported_encoding")
    return declared


class HttpsTransport:
    """One fresh HTTPS connection per GET; the factory is an offline test seam."""

    def __init__(self, *, connection_factory=http.client.HTTPSConnection):
        self._connection_factory = connection_factory

    def get(
        self, url: str, *, headers: Mapping[str, str],
        timeout_seconds: float, max_response_bytes: int,
    ) -> HttpResponse:
        validate_limits(timeout_seconds, max_response_bytes)
        host, port, target, supplied = _request_parts(url, headers)
        connection = response = None
        failure = None
        result = None
        try:
            connection = self._connection_factory(host, port, timeout=timeout_seconds)
            connection.request("GET", target, headers=supplied)
            response = connection.getresponse()
            received_headers = response.getheaders()
            declared = _framing(received_headers, max_response_bytes)
            chunks = []
            size = 0
            while size <= max_response_bytes:
                chunk = response.read(min(65536, max_response_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > max_response_bytes:
                raise TransportError("response_too_large")
            if declared is not None and size != declared:
                raise TransportError("incomplete_body")
            relevant = {"content-type", "content-encoding", "content-length"}
            result = HttpResponse(
                response.status, b"".join(chunks),
                tuple((key.lower(), value) for key, value in received_headers
                      if key.lower() in relevant),
            )
        except TransportError as error:
            failure = error.reason
        except (OSError, http.client.HTTPException):
            failure = "network_error"
        finally:
            for resource in (response, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, http.client.HTTPException):
                        if failure is None:
                            failure = "network_error"
        if failure is not None:
            # Raise outside the handler: no sensitive underlying exception context.
            raise TransportError(failure)
        return result
