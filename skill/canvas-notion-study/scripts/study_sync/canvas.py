"""Read-only Canvas collection and snapshot construction.

The module intentionally keeps the Canvas boundary small: ``CanvasClient``
implements same-origin GETs, JSON/list pagination, and bounded byte downloads;
``collect_snapshot`` turns those calls into the stable snapshot shape described
in ``CONTRACT.md``.  Every per-object API failure is represented as a warning
so one unavailable Canvas endpoint does not erase an otherwise useful run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import html as html_module
import inspect
import json
import re
import time
from pathlib import Path
import os
import tempfile
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .coverage import object_ids, record_read, summarize, utc_now

try:  # package import when called as study_sync.canvas
    from .archive import DEFAULT_MAX_FILE_SIZE, archive_file
except ImportError:  # direct script import
    from archive import DEFAULT_MAX_FILE_SIZE, archive_file  # type: ignore


CANVAS_API_PREFIX = "/api/v1"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
KNOWN_MODES = frozenset({"course", "hub", "ignore", "review"})


class CanvasError(RuntimeError):
    """Base class for read-only Canvas client errors."""


class CanvasNetworkError(CanvasError):
    """Retryable transport failure for a GET request."""


class CanvasOriginError(CanvasError, ValueError):
    """Raised when a URL is outside the configured Canvas origin."""


class CanvasHTTPError(CanvasError):
    """HTTP response error with secrets excluded from its text representation."""

    def __init__(self, status: int, url: str, *, headers: Mapping[str, Any] | None = None):
        self.status = int(status)
        self.url = _safe_url(url)
        self.headers = dict(headers or {})
        super().__init__(f"Canvas request failed with HTTP {self.status} at {self.url}")


class CanvasResponseError(CanvasError):
    """Malformed or unsupported injected HTTP response."""


class CanvasDownloadTooLarge(CanvasError):
    """Raised when a byte response exceeds a caller's configured limit."""


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only when their destination has the same origin."""

    def __init__(self, origin: tuple[str, str, int | None], *, allow_external: bool = False) -> None:
        super().__init__()
        self.origin = origin
        self.allow_external = allow_external

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        try:
            destination = CanvasClient._origin_tuple(newurl)
        except (ValueError, AttributeError) as exc:
            raise CanvasOriginError("redirect target is not a valid Canvas URL") from exc
        parts = urlsplit(newurl)
        if parts.username or parts.password:
            raise CanvasOriginError(f"Canvas redirect is outside configured origin: {_safe_url(newurl)}")
        if destination != self.origin:
            if not self.allow_external or parts.scheme.lower() != "https":
                raise CanvasOriginError(f"Canvas redirect is outside configured origin: {_safe_url(newurl)}")
            # A Canvas file may redirect to a signed HTTPS CDN URL.  Keep its
            # query signature, but strip every credential-bearing header.
            safe_headers = {
                key: value
                for key, value in req.headers.items()
                if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
            }
            return Request(newurl, headers=safe_headers, method="GET")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_url(url: str) -> str:
    """Remove credentials and query values from URLs used in diagnostics."""

    try:
        parts = urlsplit(str(url))
        if not parts.scheme or not parts.netloc:
            return parts.path or str(url)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = ""
        try:
            if parts.port:
                port = f":{parts.port}"
        except ValueError:
            pass
        # Paths are useful for diagnostics; query strings may contain tokens.
        return urlunsplit((parts.scheme, f"{host}{port}", parts.path, "", ""))
    except Exception:
        return "<canvas-url>"


def _header(headers: Mapping[str, Any] | None, name: str, default: Any = None) -> Any:
    if not headers:
        return default
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return default


def _normalise_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {str(k): str(v) for k, v in headers.items()}
    except AttributeError:
        return {}


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "code", None)
    try:
        return int(status) if status is not None else 200
    except (TypeError, ValueError):
        return 200


def _body_from_response(response: Any, *, max_bytes: int | None = None) -> Any:
    """Extract a response body from supported HTTP adapters."""

    if isinstance(response, (bytes, bytearray, memoryview, str, list, dict, tuple)):
        return response
    if hasattr(response, "read"):
        reader = response.read
        if max_bytes is not None:
            try:
                # A real urllib response honours the size argument, keeping
                # unknown-length downloads bounded to max_bytes + 1 bytes.
                return reader(max_bytes + 1)
            except TypeError:
                # Some response adapters expose read() without a size argument.
                return reader()
        return reader()
    content = getattr(response, "content", None)
    if content is not None:
        return content
    data = getattr(response, "data", None)
    if data is not None:
        return data
    json_method = getattr(response, "json", None)
    if callable(json_method):
        return json_method()
    text = getattr(response, "text", None)
    if text is not None:
        return text
    return None


def _normalise_response(response: Any, *, max_bytes: int | None = None) -> tuple[int, dict[str, str], Any]:
    """Accept response envelopes as well as urllib and requests responses."""

    if isinstance(response, tuple):
        if len(response) == 3 and isinstance(response[0], int):
            return int(response[0]), _normalise_headers(response[1]), response[2]
        if len(response) == 2:
            if isinstance(response[0], int):
                return int(response[0]), {}, response[1]
            if isinstance(response[1], Mapping):
                return 200, _normalise_headers(response[1]), response[0]
    if isinstance(response, Mapping):
        # A raw JSON object is a supported response value. Treat it
        # as a response envelope only when status/headers make that explicit,
        # or when the object consists solely of conventional envelope keys.
        # Canvas objects themselves may contain ``body`` or ``content`` and
        # must retain their other metadata fields (especially ``url``).
        status_value = response.get("status", response.get("status_code"))
        status_is_numeric = isinstance(status_value, (int, float)) and not isinstance(status_value, bool)
        if status_value is not None:
            try:
                int(status_value)
                status_is_numeric = True
            except (TypeError, ValueError):
                status_is_numeric = False
        envelope_keys = {"status", "status_code", "body", "content", "data", "headers"}
        looks_like_envelope = status_is_numeric or "headers" in response
        looks_like_envelope = looks_like_envelope or (
            bool(set(response).intersection({"body", "content", "data"}))
            and set(response).issubset(envelope_keys)
        )
        if looks_like_envelope:
            status = status_value if status_value is not None else 200
            body = response.get("body", response.get("content", response.get("data")))
            return int(status), _normalise_headers(response.get("headers")), body
        return 200, {}, response
    status = _response_status(response)
    response_headers = _normalise_headers(getattr(response, "headers", None))
    if max_bytes is not None:
        content_length = _header(response_headers, "Content-Length")
        try:
            if content_length is not None and int(content_length) > max_bytes:
                raise CanvasDownloadTooLarge("Canvas response exceeds configured size limit")
        except ValueError:
            pass
    return status, response_headers, _body_from_response(response, max_bytes=max_bytes)


def _decode_json(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, (dict, list, int, float, bool)):
        return body
    if isinstance(body, memoryview):
        body = body.tobytes()
    if isinstance(body, bytearray):
        body = bytes(body)
    if isinstance(body, bytes):
        if not body.strip():
            return None
        try:
            body = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CanvasResponseError("Canvas returned non-UTF-8 JSON") from exc
    if isinstance(body, str):
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CanvasResponseError("Canvas returned malformed JSON") from exc
    return body


def _request_retry_after(error: CanvasHTTPError) -> float | None:
    value = _header(error.headers, "Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 30.0))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            delay = date.timestamp() - time.time()
            return max(0.0, min(delay, 30.0))
        except (TypeError, ValueError, OverflowError):
            return None


class CanvasClient:
    """Minimal same-origin, read-only Canvas API client.

    ``transport``/``http_get`` are dependency-injection hooks.  They may be a
    callable accepting ``(url, headers, timeout)`` or a urllib-style opener
    exposing ``open(request, timeout=...)``. A transport can also return
    ``(status, headers, body)``, ``(body, headers)``, a raw JSON value, or a
    response object with ``status``/``headers``/``read`` attributes.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float = 30,
        *,
        opener: Any = None,
        transport: Callable[..., Any] | Any = None,
        http_get: Callable[..., Any] | Any = None,
        retries: int = 2,
        backoff_seconds: float = 0.25,
        sleeper: Callable[[float], Any] = time.sleep,
        user_agent: str = "canvas-notion-study/1",
        max_pages: int = 1000,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url is required")
        parsed = urlsplit(base_url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("base_url must be an http(s) origin without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        self.base_url = base_url.rstrip("/")
        self._origin = self._origin_tuple(self.base_url)
        self.token = token
        self.timeout = float(timeout)
        self.opener = opener
        self.transport = transport if transport is not None else http_get
        self.retries = max(0, int(retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.sleeper = sleeper
        self.user_agent = user_agent
        self.max_pages = max(1, int(max_pages))
        self.coverage_log: list[dict[str, Any]] = []
        self.last_pagination: dict[str, Any] = {}
        self._urllib_opener = build_opener(_SameOriginRedirectHandler(self._origin))

    @staticmethod
    def _origin_tuple(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().rstrip(".")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("invalid URL port") from exc
        if port is None:
            port = 443 if scheme == "https" else 80
        return scheme, host, port

    def _resolve_url(self, path: str, *, allow_external: bool = False) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("Canvas path is required")
        if path.startswith("//"):
            raise CanvasOriginError("protocol-relative URL is outside the Canvas origin")
        parts = urlsplit(path)
        if parts.scheme or parts.netloc:
            url = path
        elif path.startswith("/"):
            url = f"{self.base_url.split('://', 1)[0]}://{urlsplit(self.base_url).netloc}{path}"
        else:
            url = urljoin(self.base_url.rstrip("/") + "/", path)
        try:
            origin = self._origin_tuple(url)
        except ValueError as exc:
            raise CanvasOriginError("invalid Canvas URL") from exc
        if origin != self._origin:
            if allow_external and urlsplit(url).scheme.lower() == "https":
                resolved = urlsplit(url)
                if resolved.username or resolved.password:
                    raise CanvasOriginError("Canvas URL contains credentials")
                return url
            raise CanvasOriginError(f"Canvas URL is outside configured origin: {_safe_url(url)}")
        resolved = urlsplit(url)
        if resolved.username or resolved.password:
            raise CanvasOriginError("Canvas URL contains credentials")
        return url

    @staticmethod
    def _encode_params(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> str:
        if not params:
            return url
        if isinstance(params, Mapping):
            pairs: list[tuple[str, Any]] = []
            for key, value in params.items():
                if isinstance(value, (list, tuple)):
                    pairs.extend((str(key), item) for item in value)
                else:
                    pairs.append((str(key), value))
        else:
            pairs = [(str(key), value) for key, value in params]
        parts = urlsplit(url)
        existing = parse_qsl(parts.query, keep_blank_values=True)
        query = urlencode(existing + pairs, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    def _call_transport(self, target: Any, url: str, headers: Mapping[str, str]) -> Any:
        if hasattr(target, "open") and callable(target.open):
            request = Request(url, method="GET", headers=dict(headers))
            try:
                return target.open(request, timeout=self.timeout)
            except TypeError:
                return target.open(request)
        for method_name in ("get", "request"):
            method = getattr(target, method_name, None)
            if callable(method):
                return self._call_transport(method, url, headers)
        if not callable(target):
            raise TypeError("injected Canvas transport is not callable")

        # Prefer named arguments and support positional transport callbacks.
        try:
            signature = inspect.signature(target)
            params = signature.parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs: dict[str, Any] = {}
            if accepts_kwargs or "headers" in params:
                kwargs["headers"] = dict(headers)
            if accepts_kwargs or "timeout" in params:
                kwargs["timeout"] = self.timeout
            if kwargs:
                return target(url, **kwargs)
            if len(params) >= 3:
                return target(url, dict(headers), self.timeout)
            if len(params) == 2:
                return target(url, dict(headers))
            return target(url)
        except (TypeError, ValueError):
            # Builtins and callable objects can lack inspectable signatures.
            try:
                return target(url, dict(headers), self.timeout)
            except TypeError:
                try:
                    return target(url, dict(headers))
                except TypeError:
                    return target(url)

    def _request_once(
        self,
        url: str,
        *,
        parse_json: bool,
        max_bytes: int | None = None,
        allow_external_redirects: bool = False,
    ) -> tuple[Any, dict[str, str]]:
        headers = {"Accept": "*/*" if not parse_json else "application/json", "User-Agent": self.user_agent}
        if self.token and self._origin_tuple(url) == self._origin:
            token_text = str(self.token)
            headers["Authorization"] = token_text if token_text.lower().startswith("bearer ") else f"Bearer {token_text}"
        default_target = self._urllib_opener
        if allow_external_redirects and self.opener is None and self.transport is None:
            default_target = build_opener(_SameOriginRedirectHandler(self._origin, allow_external=True))
        target = self.opener or self.transport or default_target
        try:
            if target is self._urllib_opener or (self.opener is None and self.transport is None and target is default_target):
                request = Request(url, method="GET", headers=headers)
                response = target.open(request, timeout=self.timeout)
            else:
                response = self._call_transport(target, url, headers)
            status, response_headers, body = _normalise_response(response, max_bytes=max_bytes)
            if status < 200 or status >= 300:
                raise CanvasHTTPError(status, url, headers=response_headers)
            if max_bytes is not None:
                content_length = _header(response_headers, "Content-Length")
                try:
                    if content_length is not None and int(content_length) > max_bytes:
                        raise CanvasDownloadTooLarge("Canvas response exceeds configured size limit")
                except ValueError:
                    pass
            if not parse_json:
                if body is None:
                    raw = b""
                elif isinstance(body, bytes):
                    raw = body
                elif isinstance(body, (bytearray, memoryview)):
                    raw = bytes(body)
                elif isinstance(body, str):
                    raw = body.encode("utf-8")
                else:
                    raw = json.dumps(body).encode("utf-8")
                if max_bytes is not None and len(raw) > max_bytes:
                    raise CanvasDownloadTooLarge("Canvas response exceeds configured size limit")
                return raw, response_headers
            return _decode_json(body), response_headers
        except HTTPError as exc:
            response_headers = _normalise_headers(getattr(exc, "headers", None))
            raise CanvasHTTPError(exc.code, url, headers=response_headers) from exc
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise CanvasNetworkError(f"Canvas network request failed ({type(exc).__name__})") from exc
        finally:
            # urllib responses need closing; other adapters may not expose
            # close or may be context-managed by their owner.
            try:
                if "response" in locals() and hasattr(response, "close"):
                    response.close()
            except Exception:
                pass

    def _request(
        self,
        path: str,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        *,
        parse_json: bool = True,
        max_bytes: int | None = None,
        allow_external_redirects: bool = False,
        allow_external: bool = False,
    ) -> tuple[Any, dict[str, str]]:
        url = self._encode_params(self._resolve_url(path, allow_external=allow_external), params)
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                return self._request_once(
                    url,
                    parse_json=parse_json,
                    max_bytes=max_bytes,
                    allow_external_redirects=allow_external_redirects,
                )
            except (CanvasHTTPError, CanvasError) as exc:
                retryable = isinstance(exc, CanvasHTTPError) and exc.status in RETRYABLE_STATUS_CODES
                retryable = retryable or isinstance(exc, CanvasNetworkError)
                if not retryable or attempt >= attempts - 1:
                    raise
                delay = _request_retry_after(exc) if isinstance(exc, CanvasHTTPError) else None
                if delay is None:
                    delay = self.backoff_seconds * (2**attempt)
                if delay:
                    self.sleeper(delay)
        raise AssertionError("unreachable")

    def get(self, path: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None) -> Any:
        """Perform one same-origin GET and decode its JSON response."""

        body, _ = self._request(path, params, parse_json=True)
        return body

    def get_bytes(
        self,
        path: str,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        *,
        max_bytes: int | None = None,
        allow_external: bool = False,
    ) -> bytes:
        """Perform one same-origin GET and return bytes, with an optional limit."""

        body, _ = self._request(
            path,
            params,
            parse_json=False,
            max_bytes=max_bytes,
            allow_external=allow_external,
            allow_external_redirects=allow_external,
        )
        return body

    download = get_bytes

    def download_to(self, path: str, destination: str | Path, *, max_bytes: int | None = None,
                    allow_external: bool = False, chunk_size: int = 1024 * 1024) -> dict[str, Any]:
        """Stream a download into an atomic local file, hashing as bytes arrive."""
        url = self._resolve_url(path, allow_external=allow_external)
        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self.retries + 1):
            response = None
            temporary_name = None
            try:
                headers = {"Accept": "*/*", "User-Agent": self.user_agent}
                if self.token and self._origin_tuple(url) == self._origin:
                    token = str(self.token)
                    headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
                target = self.opener or self.transport
                if target is None:
                    target = build_opener(_SameOriginRedirectHandler(self._origin, allow_external=allow_external))
                response = self._call_transport(target, url, headers)
                # Accept streaming responses and raw response envelopes.
                if hasattr(response, "read"):
                    status = _response_status(response)
                    response_headers = _normalise_headers(getattr(response, "headers", None))
                    body = response
                else:
                    status, response_headers, body = _normalise_response(response)
                if not 200 <= status < 300:
                    raise CanvasHTTPError(status, url, headers=response_headers)
                try:
                    declared = int(_header(response_headers, "Content-Length"))
                except (ValueError, TypeError):
                    declared = None
                if max_bytes is not None and declared is not None and declared > max_bytes:
                    raise CanvasDownloadTooLarge("Canvas response exceeds configured size limit")
                fd, temporary_name = tempfile.mkstemp(prefix=".stream-", dir=target_path.parent)
                size = 0
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as stream:
                    while True:
                        if hasattr(body, "read"):
                            read_size = chunk_size if max_bytes is None else min(chunk_size, max_bytes - size + 1)
                            try:
                                chunk = body.read(read_size)
                            except TypeError:
                                chunk, body = body.read(), None
                        else:
                            chunk, body = body, None
                        if chunk is None or chunk == b"" or chunk == "":
                            break
                        if isinstance(chunk, str):
                            chunk = chunk.encode()
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise CanvasResponseError("Canvas byte response is not binary")
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise CanvasDownloadTooLarge("Canvas response exceeds configured size limit")
                        stream.write(chunk)
                        digest.update(chunk)
                        if body is None:
                            break
                    stream.flush()
                    os.fsync(stream.fileno())
                if declared is not None and declared != size:
                    raise CanvasNetworkError("Canvas download length does not match Content-Length")
                os.replace(temporary_name, target_path)
                return {"local_path": str(target_path), "sha256": digest.hexdigest(), "size": size,
                        "content_type": _header(response_headers, "Content-Type")}
            except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, CanvasError) as exc:
                if isinstance(exc, HTTPError):
                    error = CanvasHTTPError(exc.code, url, headers=_normalise_headers(exc.headers))
                elif isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)):
                    error = CanvasNetworkError(f"Canvas network request failed ({type(exc).__name__})")
                else:
                    error = exc
                retryable = isinstance(error, CanvasNetworkError) or isinstance(error, CanvasHTTPError) and error.status in RETRYABLE_STATUS_CODES
                if not retryable or attempt == self.retries:
                    raise error from exc
                delay = _request_retry_after(error) if isinstance(error, CanvasHTTPError) else None
                self.sleeper(self.backoff_seconds * 2**attempt if delay is None else delay)
            finally:
                if response is not None and hasattr(response, "close"):
                    response.close()
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
        raise AssertionError("unreachable")

    def list(
        self,
        path: str,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
    ) -> list[Any]:
        """Fetch all pages of a Canvas list using its RFC 5988 Link header."""

        results: list[Any] = []
        self.last_partial_results = results
        next_path: str | None = path
        next_params = params
        self.last_pagination = {"page_count": 0, "pages": [], "ids": [], "complete": False}
        seen_urls: set[str] = set()
        for _page in range(self.max_pages):
            if next_path is None:
                break
            request_url = self._encode_params(self._resolve_url(next_path), next_params)
            if request_url in seen_urls:
                raise CanvasError("Canvas pagination repeated a page")
            seen_urls.add(request_url)
            body, headers = self._request(next_path, next_params, parse_json=True)
            next_params = None
            if body is None:
                page_items: list[Any] = []
            elif isinstance(body, list):
                page_items = body
            elif isinstance(body, Mapping):
                if isinstance(body.get("items"), list):
                    page_items = list(body["items"])
                elif isinstance(body.get("data"), list):
                    page_items = list(body["data"])
                else:
                    page_items = [body]
            else:
                raise CanvasResponseError("Canvas list response is not an array")
            results.extend(page_items)
            ids = object_ids(page_items)
            self.last_pagination["pages"].append({"page": _page + 1, "ids": ids, "count": len(page_items), "retrieved_at": utc_now()})
            self.last_pagination["page_count"] = _page + 1
            self.last_pagination["ids"].extend(ids)
            next_path = self._next_link(_header(headers, "Link"))
            if next_path is None:
                self.last_pagination["complete"] = True
                return results
        else:
            raise CanvasError(f"Canvas pagination exceeded {self.max_pages} pages")
        return results

    def _next_link(self, link_header: Any) -> str | None:
        if not link_header:
            return None
        text = str(link_header)
        # Canvas emits one or more ``<url>; rel="next"`` entries.  This
        # scanner avoids splitting commas that happen inside a URL query.
        for match in re.finditer(r"<([^>]+)>\s*;\s*([^,]+)", text):
            url, attributes = match.group(1), match.group(2)
            relations = re.findall(r"(?:^|;)\s*rel\s*=\s*[\"']?([^\"';,\s]+)", attributes, re.I)
            if any("next" in relation.lower().split() for relation in relations):
                return self._resolve_url(url)
        # Accept Link headers that omit quotes around rel or use a plain
        # comma-free header.
        match = re.search(r"<([^>]+)>[^,]*\brel\s*=\s*[\"']?next\b", text, re.I)
        return self._resolve_url(match.group(1)) if match else None


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.anchors: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value for key, value in attrs}
        if tag.lower() == "a" and attrs_map.get("href"):
            self._anchor = {"href": attrs_map["href"], "label": ""}
            self.anchors.append(self._anchor)
        for key in ("href", "src", "data"):  # data catches simple embedded refs
            value = attrs_map.get(key)
            if value:
                self.urls.append(value)

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["label"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._anchor = None


def extract_external_resource_refs(html: Any, base_url: str, source_page_url: str) -> list[dict[str, Any]]:
    """Index explicitly linked external resources without fetching them."""
    parser = _LinkParser()
    parser.feed(str(html or ""))
    parser.close()
    origin = CanvasClient._origin_tuple(base_url)
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in parser.anchors:
        try:
            url = urljoin(source_page_url, anchor["href"])
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
            continue
        try:
            if CanvasClient._origin_tuple(url) == origin:
                continue
        except ValueError:
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if url in seen:
            continue
        seen.add(url)
        refs.append({
            "id": "external-" + hashlib.sha256(url.encode()).hexdigest()[:24],
            "resource_type": "external_link",
            "title": re.sub(r"\s+", " ", anchor["label"]).strip() or parts.hostname,
            "source_url": url,
            "discovered_on": source_page_url,
            "download_status": "link_only",
            "extraction_status": "not_fetched",
            "text": f"Canvas 页面引用的外部资料链接；此记录保存入口，尚未获取外部站点正文。来源页面：{source_page_url}",
        })
    return refs


def extract_html_text(value: Any) -> str:
    """Convert Canvas HTML fields into compact visible text."""

    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_module.unescape(value))
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


html_to_text = extract_html_text


_FILE_PATH_RE = re.compile(r"(?:^|/)files/(\d+)(?:/|$)", re.I)


def extract_html_file_refs(html: Any, base_url: str | None = None) -> list[dict[str, Any]]:
    """Find same-origin Canvas file links in arbitrary HTML.

    Canvas pages sometimes contain file links even when the Files index is
    unavailable.  Numeric Canvas file IDs are preferred; path-only links still
    receive a stable URL-derived source ID so they can be archived.
    """

    if not html:
        return []
    parser = _LinkParser()
    try:
        parser.feed(str(html))
        parser.close()
    except Exception:
        pass
    raw_values = list(parser.urls)
    # Also catch Markdown/plain HTML fragments without an anchor parser.
    raw_values.extend(re.findall(r"(?:https?://[^\s\"'<>]+|/[^\s\"'<>]*files/[^\s\"'<>]+)", str(html), re.I))
    seen: set[str] = set()
    refs: list[dict[str, Any]] = []
    base_origin: tuple[str, str, int | None] | None = None
    if base_url:
        try:
            base_origin = CanvasClient._origin_tuple(base_url)
        except ValueError:
            base_origin = None
    for raw in raw_values:
        raw = html_module.unescape(str(raw)).strip().rstrip(")],.;")
        if not raw or raw.startswith(("#", "mailto:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, raw) if base_url else raw
        parts = urlsplit(absolute)
        if base_origin and parts.scheme and parts.netloc:
            try:
                if CanvasClient._origin_tuple(absolute) != base_origin:
                    continue
            except ValueError:
                continue
        match = _FILE_PATH_RE.search(parts.path)
        file_id = match.group(1) if match else None
        if "files" not in parts.path.lower():
            continue
        canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        stable_path = parts.path
        if file_id:
            file_marker = re.search(r"/courses/[^/]+/files/\d+", parts.path, re.I)
            if not file_marker:
                file_marker = re.search(r"/files/\d+", parts.path, re.I)
            if file_marker:
                stable_path = file_marker.group(0)
        stable_source_url = urlunsplit((parts.scheme, parts.netloc, stable_path, "", ""))
        source_id = f"file:{file_id}" if file_id else f"file-url:{hashlib.sha256(canonical.encode()).hexdigest()[:20]}"
        if source_id in seen:
            continue
        seen.add(source_id)
        name = Path(parts.path).name or (f"file-{file_id}" if file_id else "file")
        if (name.lower() in {"download", "preview", "file"} or name.isdigit()) and file_id:
            name = f"file-{file_id}"
        refs.append(
            {
                "id": int(file_id) if file_id else None,
                "source_id": source_id,
                "url": canonical,
                "download_url": canonical,
                "display_name": name,
                "source_url": stable_source_url,
            }
        )
    return refs


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _warning(scope: str, code: str, message: str, object_id: Any = None) -> dict[str, Any]:
    value: dict[str, Any] = {"scope": scope, "code": code, "message": str(message)}
    if object_id is not None:
        value["object_id"] = object_id
    return value


def _error_warning(scope: str, code: str, exc: Exception, object_id: Any = None) -> dict[str, Any]:
    # Exception text can contain a full URL or a server response.  Class name
    # is enough to diagnose a per-endpoint failure without leaking a token.
    message = f"Canvas request failed ({type(exc).__name__})"
    if isinstance(exc, CanvasHTTPError):
        message = f"Canvas request failed with HTTP {exc.status}"
    return _warning(scope, code, message, object_id)


def _term_evidence(course: Mapping[str, Any], term: Mapping[str, Any]) -> bool:
    expected_values = {str(term.get("key", "")), str(term.get("label", ""))}
    expected = {value.lower() for value in expected_values if value}
    expected.update(re.sub(r"[^a-z0-9]+", "", value.lower()) for value in expected_values if value)
    nested_terms: list[Mapping[str, Any]] = []
    for key in ("term", "enrollment_term"):
        value = course.get(key)
        if isinstance(value, Mapping):
            nested_terms.append(value)
    for candidate in nested_terms:
        raw_values = {str(candidate.get(key, "")) for key in ("id", "name", "sis_term_id", "key", "label")}
        values = {value.lower() for value in raw_values if value}
        values.update(re.sub(r"[^a-z0-9]+", "", value.lower()) for value in raw_values if value)
        if expected.intersection(values):
            return True
        start = candidate.get("start_at") or candidate.get("start")
        end = candidate.get("end_at") or candidate.get("end")
        if _interval_overlaps(start, end, term.get("start"), term.get("end")):
            return True
    return _interval_overlaps(
        course.get("start_at") or course.get("start"),
        course.get("end_at") or course.get("end"),
        term.get("start"),
        term.get("end"),
    )


def _date_value(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _interval_overlaps(start_a: Any, end_a: Any, start_b: Any, end_b: Any) -> bool:
    a0, a1 = _date_value(start_a), _date_value(end_a)
    b0, b1 = _date_value(start_b), _date_value(end_b)
    if None in (a0, a1, b0, b1):
        return False
    return a0 <= b1 and b0 <= a1


def _calendar_event_in_term(event: Mapping[str, Any], term: Mapping[str, Any]) -> bool:
    """Keep events in the configured window when Canvas returns extra history."""

    event_start = event.get("start_at") or event.get("start") or event.get("date")
    event_end = event.get("end_at") or event.get("end") or event_start
    if not event_start and not event_end:
        return True
    start = _date_value(event_start)
    end = _date_value(event_end)
    term_start = _date_value(term.get("start"))
    term_end = _date_value(term.get("end"))
    if None in (start, end, term_start, term_end):
        return True
    return start <= term_end and term_start <= end


def _title_is_hub(title: Any) -> bool:
    lowered = str(title or "").lower()
    return any(token in lowered for token in ("hub", "orientation", "portal", "community", "first year", "student life", "announcements"))


def _title_is_course(title: Any, course_code: Any) -> bool:
    if course_code and str(course_code).strip():
        return True
    text = str(title or "").strip()
    words = re.findall(r"[A-Za-z]{3,}", text)
    has_course_number = bool(re.search(r"(?:\b\d{1,3}\b|\b(?:I|II|III|IV|V|VI)\b)", text, re.I))
    return (len(words) >= 2 or has_course_number) and not _title_is_hub(title)


def _course_matches_enrollment_scope(course: Mapping[str, Any], requested: Any) -> bool:
    """Enforce active scope when the API returns enrollment state."""

    if str(requested or "active").lower() != "active":
        return True
    values: list[Any] = [course.get("enrollment_state")]
    enrollment = course.get("enrollment")
    if isinstance(enrollment, Mapping):
        values.append(enrollment.get("enrollment_state") or enrollment.get("state"))
    values = [value for value in values if value is not None]
    if values and all(str(value).lower() not in {"active", "available"} for value in values):
        return False
    workflow = str(course.get("workflow_state") or "").lower()
    return workflow not in {"unpublished", "deleted"}


def _infer_mode(course: Mapping[str, Any], term: Mapping[str, Any], explicit: Mapping[str, Any]) -> str:
    course_id = str(course.get("id", ""))
    if course_id in explicit:
        value = str(explicit[course_id]).lower()
        return value if value in KNOWN_MODES else "review"
    current = _term_evidence(course, term)
    code = course.get("course_code") or course.get("sis_course_id")
    title = course.get("name") or course.get("course_name")
    if current and _title_is_hub(title):
        return "hub"
    if current and _title_is_course(title, code):
        return "course"
    if current and not code and title:
        # Current-term containers without a Canvas course code are retained as
        # hubs so useful notices/tasks survive even when their title is simply
        # a programme acronym (for example "EngSci" or "PLD").
        return "hub"
    if code or _title_is_course(title, code):
        return "review"
    return "ignore"


def _course_url(client: CanvasClient, course: Mapping[str, Any]) -> str:
    value = course.get("html_url")
    try:
        return client._resolve_url(str(value)) if value else client._resolve_url(f"/courses/{course.get('id')}")
    except (CanvasOriginError, ValueError):
        return client._resolve_url(f"/courses/{course.get('id')}")


def _record_with_text(record: Mapping[str, Any], source_url: str | None = None, html_fields: Sequence[str] = ()) -> dict[str, Any]:
    output = dict(record)
    source = source_url or output.get("source_url") or output.get("html_url") or output.get("url")
    if source:
        output["source_url"] = str(source)
    for key in html_fields:
        value = output.get(key)
        if value:
            output["text"] = extract_html_text(value)
            break
    return output


def _previous_courses(previous: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(previous, Mapping):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for course in previous.get("courses", []) or []:
        if isinstance(course, Mapping) and course.get("id") is not None:
            result[str(course["id"])] = course
    return result


def _safe_list(
    client: Any,
    path: str,
    params: Mapping[str, Any],
    *,
    warnings: list[dict[str, Any]],
    scope: str,
    code: str,
    object_id: Any = None,
    fallback: list[Any] | None = None,
) -> tuple[list[Any], bool]:
    if hasattr(client, "last_pagination"):
        client.last_pagination = {}
        client.last_partial_results = []
    try:
        value = client.list(path, params=params)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            value = list(value)
        else:
            value = [value] if value is not None else []
        return record_read(client, path, params, value, True, pagination=getattr(client, "last_pagination", None)), True
    except Exception as exc:
        warnings.append(_error_warning(scope, code, exc, object_id))
        prior_values = list(fallback or [])
        partial = list(getattr(client, "last_partial_results", []))
        partial_ids = set(object_ids(partial))
        value = partial + [item for item in prior_values if not set(object_ids(item)).intersection(partial_ids)]
        value = record_read(client, path, params, value, False,
                            pagination=getattr(client, "last_pagination", None), error=type(exc).__name__)
        # Partial fresh pages remain usable, but the endpoint is explicitly
        # incomplete and retained records keep their old timestamps.
        for item in value[:len(partial)]:
            if isinstance(item, dict):
                item.update(coverage_status="fresh", retrieved_at=utc_now())
        return value, False


def _safe_get(
    client: Any,
    path: str,
    *,
    warnings: list[dict[str, Any]],
    scope: str,
    code: str,
    object_id: Any = None,
    fallback: Any = None,
) -> tuple[Any, bool]:
    try:
        return record_read(client, path, None, client.get(path), True), True
    except Exception as exc:
        warnings.append(_error_warning(scope, code, exc, object_id))
        return record_read(client, path, None, fallback, False, error=type(exc).__name__), False


def _id_path(value: Any) -> str:
    return quote(str(value), safe="")


def _nested_source_url(client: Any, path: str, value: Any = None) -> str:
    if isinstance(value, Mapping):
        for key in ("html_url", "source_url", "url"):
            candidate = value.get(key)
            if candidate:
                try:
                    # Canvas Page.url is a wiki slug, not a browser URL.
                    # Resolving it at the origin breaks its relative links.
                    if not str(candidate).startswith("/") and not urlsplit(str(candidate)).scheme:
                        continue
                    return client._resolve_url(str(candidate))
                except Exception:
                    pass
    try:
        return client._resolve_url(path)
    except Exception:
        return path


def _normalize_file_record(client: Any, course_id: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    file_id = record.get("id")
    path = f"/courses/{_id_path(course_id)}/files/{_id_path(file_id)}" if file_id is not None else f"/courses/{_id_path(course_id)}/files"
    output = dict(record)
    # Keep verifier/signed query strings out of stable source links.  The raw
    # Canvas URL remains available as ``url``/``download_url`` for a fetch.
    output["source_url"] = _nested_source_url(client, path)
    if file_id is not None:
        output["source_id"] = f"file:{file_id}"
        # Some Canvas-compatible deployments expose the file metadata API URL
        # in ``url``/``download_url``.  Never send that JSON endpoint to the
        # byte archiver; retain a signed non-metadata URL when available and
        # otherwise use the stable course download route.
        download_candidate = None
        for key in ("download_url", "url"):
            candidate = output.get(key)
            if candidate and not _is_file_metadata_url(candidate):
                download_candidate = candidate
                break
        if download_candidate is None:
            download_candidate = client._resolve_url(f"{path}/download")
        output["download_url"] = download_candidate
    elif output.get("url") and not output.get("download_url"):
        output["download_url"] = output["url"]
    return output


def _is_file_metadata_url(value: Any) -> bool:
    try:
        path = urlsplit(str(value)).path.rstrip("/")
        return bool(re.search(r"/api/v1/files/\d+$", path, re.I))
    except Exception:
        return False


def _enrich_file_reference(
    client: Any,
    file_ref: Mapping[str, Any],
    *,
    warnings: list[dict[str, Any]],
    course_id: Any,
) -> dict[str, Any]:
    """Resolve metadata for a referenced file without mistaking JSON for bytes."""

    file_id = file_ref.get("id")
    if file_id is None:
        return dict(file_ref)
    metadata, ok = _safe_get(
        client,
        f"/api/v1/files/{_id_path(file_id)}",
        warnings=warnings,
        scope="file",
        code="file_metadata_unavailable",
        object_id=file_id,
        fallback=None,
    )
    if not ok or not isinstance(metadata, Mapping):
        return dict(file_ref)
    enriched = dict(file_ref)
    enriched.update(metadata)
    enriched["id"] = file_id
    enriched["source_id"] = str(file_ref.get("source_id") or f"file:{file_id}")
    # Stable browser URL must never inherit a verifier/signed query from the
    # API response.  The raw URL remains only as a download candidate.
    enriched["source_url"] = file_ref.get("source_url") or client._resolve_url(
        f"/courses/{_id_path(course_id)}/files/{_id_path(file_id)}"
    )
    candidate = metadata.get("download_url") or metadata.get("url")
    if candidate and not _is_file_metadata_url(candidate):
        enriched["download_url"] = candidate
        enriched["url"] = candidate
    else:
        fallback_download = file_ref.get("download_url") or file_ref.get("url")
        if fallback_download:
            enriched["download_url"] = fallback_download
            enriched["url"] = fallback_download
    return enriched


def _content_records(course):
    """Yield content containers with a stable human-reviewable source location."""
    if course.get("mode") != "hub":
        yield course, {"kind": "syllabus", "object_id": course.get("id"), "source_url": course.get("html_url")}
    for kind in ("announcements", "assignments", "pages", "modules", "calendar_events"):
        if course.get("mode") == "hub" and kind not in {"announcements", "assignments", "calendar_events"}:
            continue
        for record in course.get(kind, []) or []:
            if isinstance(record, Mapping):
                yield record, {"kind": kind, "object_id": record.get("id", record.get("page_id", record.get("url"))),
                               "source_url": record.get("source_url") or record.get("html_url") or course.get("html_url")}


def _walk_content(record, location, *, root=False):
    for key, value in record.items():
        # The course root contributes its syllabus only; child collections
        # receive their own source identities from _content_records.
        if root and key != "syllabus_body":
            continue
        field = dict(location, field=str(location.get("field", "")) + ("." if location.get("field") else "") + key)
        if key in {"syllabus_body", "message", "description", "body", "details"} and isinstance(value, str):
            yield "html", value, field
        elif key in {"attachment", "attachments"}:
            values = value if isinstance(value, list) else [value]
            for attachment in values:
                if isinstance(attachment, Mapping):
                    yield "attachment", attachment, field
        elif isinstance(value, Mapping):
            yield from _walk_content(value, field)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    nested = dict(field)
                    if item.get("source_url"):
                        nested["source_url"] = item["source_url"]
                    yield from _walk_content(item, nested)


def _file_refs_from_course(course: Mapping[str, Any], base_url: str, course_id: Any = None) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}

    def add(ref, location):
        existing = by_source.setdefault(str(ref["source_id"]), dict(ref, discovered_from=[]))
        if location not in existing["discovered_from"]:
            existing["discovered_from"].append(location)

    for module in course.get("modules", []) or []:
        if not isinstance(module, Mapping):
            continue
        for item in module.get("items", []) or []:
            if not isinstance(item, Mapping) or str(item.get("type") or "").lower() != "file":
                continue
            content_id = item.get("content_id")
            if content_id is None:
                match = _FILE_PATH_RE.search(urlsplit(str(item.get("url") or item.get("html_url") or "")).path)
                content_id = match.group(1) if match else None
            if content_id is None or not str(content_id).isdigit():
                continue
            file_id = int(content_id)
            source = f"{base_url.rstrip('/')}/courses/{_id_path(course_id)}/files/{file_id}"
            ref = {"id": file_id, "source_id": f"file:{file_id}", "source_url": source,
                   "url": source + "/download", "download_url": source + "/download",
                   "display_name": item.get("title") or item.get("filename") or f"file-{file_id}",
                   "module_item_id": item.get("id")}
            mime = item.get("content_type") or item.get("content-type") or item.get("mime_type")
            if mime:
                ref["content_type"] = mime
            add(ref, {"kind": "modules", "object_id": module.get("id"), "module_item_id": item.get("id"),
                      "source_url": item.get("source_url") or module.get("source_url"), "field": "items"})
    for record, location in _content_records(course):
        for kind, value, found in _walk_content(record, location, root=record is course):
            if kind == "html":
                for ref in extract_html_file_refs(value, base_url):
                    add(ref, found)
                continue
            attachment = dict(value)
            raw_url = attachment.get("download_url") or attachment.get("url")
            candidates = extract_html_file_refs(raw_url, base_url) if raw_url else []
            if attachment.get("id") is not None:
                file_id = attachment["id"]
                source = f"{base_url.rstrip('/')}/courses/{_id_path(course_id)}/files/{_id_path(file_id)}"
                ref = {"id": file_id, "source_id": f"file:{file_id}", "source_url": source,
                       "download_url": raw_url or source + "/download"}
            elif candidates:
                ref = candidates[0]
            else:
                continue
            ref.update(attachment)
            ref.setdefault("display_name", attachment.get("filename") or f"file-{ref.get('id')}")
            add(ref, found)
    return list(by_source.values())


def _page_refs(html, base_url, course_id, source_url):
    parser = _LinkParser()
    parser.feed(str(html or ""))
    pattern = re.compile(r"/(?:api/v1/)?courses/" + re.escape(str(course_id)) + r"/pages/([^/]+)(?:/|$)")
    for raw in parser.urls:
        try:
            url = urljoin(source_url or base_url, raw)
            if CanvasClient._origin_tuple(url) != CanvasClient._origin_tuple(base_url):
                continue
            match = pattern.search(urlsplit(url).path)
            if match:
                from urllib.parse import unquote
                yield unquote(match.group(1))
        except ValueError:
            continue


def _collect_announcement_replies(client, announcement, course_id, warnings, prior):
    topic_id = announcement.get("id")
    if topic_id is None:
        return
    # A known zero count is authoritative; unknown counts must be checked.
    if announcement.get("discussion_subentry_count") == 0 and announcement.get("coverage_status") != "stale":
        announcement["replies"] = []
        announcement["replies_status"] = "fresh"
        return
    path = f"/api/v1/courses/{_id_path(course_id)}/discussion_topics/{_id_path(topic_id)}/entries"
    entries, ok = _safe_list(client, path, {"per_page": 100}, warnings=warnings, scope="announcement",
                             code="announcement_replies_unavailable", object_id=topic_id, fallback=(prior or {}).get("replies", []))
    normalized = []
    all_replies_ok = ok
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        source = str(announcement.get("source_url") or "") + f"#entry-{entry.get('id')}"
        item = _record_with_text(entry, source, ("message", "body"))
        recent = entry.get("recent_replies", entry.get("replies", [])) or []
        # recent_replies is complete when Canvas explicitly says false. In
        # older API responses without the flag, check its paginated endpoint.
        if entry.get("id") is not None and (entry.get("has_more_replies") or recent and "has_more_replies" not in entry):
            recent, nested_ok = _safe_list(client, path + f"/{_id_path(entry['id'])}/replies", {"per_page": 100},
                                   warnings=warnings, scope="announcement", code="entry_replies_unavailable",
                                   object_id=entry["id"], fallback=recent)
            all_replies_ok = all_replies_ok and nested_ok
        item["replies"] = [_record_with_text(reply, str(announcement.get("source_url") or "") + f"#entry-{reply.get('id')}",
                                             ("message", "body")) for reply in recent if isinstance(reply, Mapping)]
        normalized.append(item)
    announcement["replies"] = normalized
    announcement["replies_status"] = "fresh" if all_replies_ok else "stale" if entries else "unavailable"


def _course_endpoint_params(term: Mapping[str, Any], *, context_code: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"per_page": 100}
    if context_code:
        params["context_codes[]"] = [context_code]
    if term.get("start"):
        params["start_date"] = term["start"]
    if term.get("end"):
        params["end_date"] = term["end"]
    return params


def _collect_course(
    client: CanvasClient,
    raw_course: Mapping[str, Any],
    mode: str,
    term: Mapping[str, Any],
    config: Any,
    archive_dir: Path | str,
    previous_course: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Collect one course while isolating endpoint failures to that course."""

    course_id = raw_course.get("id")
    course_id_text = str(course_id)
    course_url = _course_url(client, raw_course)
    course: dict[str, Any] = dict(raw_course)
    course.update(
        {
            "id": course_id,
            "name": raw_course.get("name") or raw_course.get("course_name") or course_id_text,
            "course_code": raw_course.get("course_code") or raw_course.get("sis_course_id") or "",
            "mode": mode,
            "html_url": course_url,
            "announcements": [],
            "assignments": [],
            "modules": [],
            "pages": [],
            "files": [],
            "calendar_events": [],
            "warnings": [],
        }
    )
    if course.get("syllabus_body"):
        course["syllabus_text"] = extract_html_text(course["syllabus_body"])
    warnings = course["warnings"]
    prior = previous_course or {}
    coverage_start = len(getattr(client, "coverage_log", []))

    # An unexpected-term candidate is retained for explicit review, but its
    # historical task/notice surface is not imported until the user chooses a
    # mode.  This avoids silently pulling in whole past academic years.
    if mode == "review":
        return course, list(warnings)

    # Hub and formal courses both retain current-term notices, tasks, and
    # timetable entries.  Review-mode courses get the same lightweight view;
    # their modules/files are intentionally deferred until a user chooses them.
    context_code = f"course_{course_id}"
    announcement_params = _course_endpoint_params(term, context_code=context_code)
    if mode == "course":
        # Canvas defaults to a narrow recent window. Formal courses require
        # all readable history; the course-specific supplement below has no
        # date filter and also covers unusual dates beyond this broad range.
        announcement_params.update(start_date="1970-01-01T00:00:00Z", end_date="2100-01-01T00:00:00Z")
    announcements, _ = _safe_list(
        client,
        "/api/v1/announcements",
        announcement_params,
        warnings=warnings,
        scope="course",
        code="announcements_unavailable",
        object_id=course_id,
        fallback=prior.get("announcements", []),
    )
    course["announcements"] = [
        _record_with_text(
            item,
            _nested_source_url(client, f"/courses/{_id_path(course_id)}/discussion_topics/{_id_path(item.get('id'))}", item),
            ("message", "body", "description"),
        )
        if isinstance(item, Mapping)
        else item
        for item in announcements
    ]
    if mode == "course":
        supplemental, _ = _safe_list(
            client, f"/api/v1/courses/{_id_path(course_id)}/discussion_topics",
            {"per_page": 100, "only_announcements": "true", "filter_by": "all"},
            warnings=warnings, scope="course", code="announcement_topics_unavailable", object_id=course_id,
        )
        combined = {str(item.get("id")): item for item in course["announcements"] if isinstance(item, Mapping)}
        for item in supplemental:
            if not isinstance(item, Mapping):
                continue
            normalized = _record_with_text(item, _nested_source_url(client,
                f"/courses/{_id_path(course_id)}/discussion_topics/{_id_path(item.get('id'))}", item), ("message", "body", "description"))
            combined[str(item.get("id"))] = {**combined.get(str(item.get("id")), {}), **normalized}
        course["announcements"] = list(combined.values())
    prior_announcements = {str(item.get("id")): item for item in prior.get("announcements", []) if isinstance(item, Mapping)}
    for announcement in course["announcements"]:
        if isinstance(announcement, dict):
            _collect_announcement_replies(client, announcement, course_id, warnings, prior_announcements.get(str(announcement.get("id"))))

    assignment_params = _course_endpoint_params(term)
    assignment_params.update(
        {
            "include[]": ["submission", "all_dates"],
            "override_assignment_dates": "true",
        }
    )
    assignments, _ = _safe_list(
        client,
        f"/api/v1/courses/{_id_path(course_id)}/assignments",
        assignment_params,
        warnings=warnings,
        scope="course",
        code="assignments_unavailable",
        object_id=course_id,
        fallback=prior.get("assignments", []),
    )
    course["assignments"] = [
        _record_with_text(
            item,
            _nested_source_url(client, f"/courses/{_id_path(course_id)}/assignments/{_id_path(item.get('id'))}", item),
            ("description", "message", "body"),
        )
        if isinstance(item, Mapping)
        else item
        for item in assignments
    ]

    calendar_params = _course_endpoint_params(term, context_code=context_code)
    calendar_events, _ = _safe_list(
        client,
        "/api/v1/calendar_events",
        calendar_params,
        warnings=warnings,
        scope="course",
        code="calendar_unavailable",
        object_id=course_id,
        fallback=prior.get("calendar_events", []),
    )
    calendar_events = [
        item
        for item in calendar_events
        if not isinstance(item, Mapping) or _calendar_event_in_term(item, term)
    ]
    course["calendar_events"] = [
        _record_with_text(
            item,
            _nested_source_url(client, f"/calendar_events/{_id_path(item.get('id'))}", item),
            ("description", "details", "body"),
        )
        if isinstance(item, Mapping)
        else item
        for item in calendar_events
    ]

    # A course-mode record is the only one that triggers the potentially large
    # content/archive pass.  This keeps hubs useful and review candidates
    # cheap while preserving their notices and assignments above.
    if mode == "course":
        module_params = {"per_page": 100, "include[]": ["items"]}
        modules, _ = _safe_list(
            client,
            f"/api/v1/courses/{_id_path(course_id)}/modules",
            module_params,
            warnings=warnings,
            scope="course",
            code="modules_unavailable",
            object_id=course_id,
            fallback=prior.get("modules", []),
        )
        normalized_modules: list[Any] = []
        for module in modules:
            if not isinstance(module, Mapping):
                normalized_modules.append(module)
                continue
            module_id = module.get("id")
            module_copy = _record_with_text(
                module,
                _nested_source_url(client, f"/courses/{_id_path(course_id)}/modules/{_id_path(module_id)}", module),
                ("description", "body"),
            )
            embedded_items = module.get("items") if isinstance(module.get("items"), list) else []
            if module_id is not None:
                items, _ = _safe_list(
                    client,
                    f"/api/v1/courses/{_id_path(course_id)}/modules/{_id_path(module_id)}/items",
                    {"per_page": 100, "include[]": ["content_details"]},
                    warnings=warnings,
                    scope="course",
                    code="module_items_unavailable",
                    object_id=module_id,
                    fallback=embedded_items,
                )
            else:
                items = list(embedded_items)
            module_copy["items"] = [
                _record_with_text(
                    item,
                    _nested_source_url(
                        client,
                        f"/courses/{_id_path(course_id)}/modules/{_id_path(module_id)}/items/{_id_path(item.get('id'))}",
                        item,
                    ),
                    ("description", "body"),
                )
                if isinstance(item, Mapping)
                else item
                for item in items
            ]
            normalized_modules.append(module_copy)
        course["modules"] = normalized_modules

        pages, _ = _safe_list(
            client,
            f"/api/v1/courses/{_id_path(course_id)}/pages",
            {"per_page": 100, "sort": "title"},
            warnings=warnings,
            scope="course",
            code="pages_unavailable",
            object_id=course_id,
            fallback=prior.get("pages", []),
        )
        # A wiki homepage has its own endpoint. The Pages index and Modules
        # can both omit it even when the student can read the homepage.
        if str(raw_course.get("default_view") or "").lower() == "wiki":
            front, _ = _safe_get(
                client,
                f"/api/v1/courses/{_id_path(course_id)}/front_page",
                warnings=warnings,
                scope="course",
                code="front_page_unavailable",
                object_id=course_id,
                fallback=next((p for p in prior.get("pages", []) if isinstance(p, Mapping) and p.get("front_page")), None),
            )
            if isinstance(front, Mapping) and (front.get("url") or front.get("page_id")):
                front = {**front, "front_page": True}
                match = next((i for i, p in enumerate(pages) if isinstance(p, Mapping) and (
                    (front.get("page_id") is not None and str(p.get("page_id")) == str(front["page_id"]))
                    or (front.get("url") and p.get("url") == front["url"])
                )), None)
                if match is None:
                    pages.append(front)
                else:
                    pages[match] = {**pages[match], **front}
        # A queue resolves every same-course Page linked by the index,
        # modules, syllabus, announcements, assignments, or another Page.
        # Page IDs and slugs alias the same record and are fetched once.
        queue = []
        queued = set()
        prior_pages = {str(page.get("url") or page.get("page_id")): page for page in prior.get("pages", []) if isinstance(page, Mapping)}

        def enqueue(page):
            key = str(page.get("url") or page.get("page_id") or "")
            if "/pages/" in key:
                key = key.rsplit("/pages/", 1)[-1]
            key = urlsplit(key).path
            if key and key not in queued:
                queued.add(key)
                queue.append({**page, "url": key})

        for page in pages:
            if isinstance(page, Mapping):
                enqueue(page)
        for module in course["modules"]:
            if not isinstance(module, Mapping):
                continue
            for item in module.get("items", []):
                if isinstance(item, Mapping) and str(item.get("type") or "").lower() == "page":
                    enqueue({"url": item.get("page_url") or item.get("url"), "title": item.get("title"), "module_item_id": item.get("id")})
        for record, location in _content_records(course):
            for kind, value, found in _walk_content(record, location, root=record is course):
                if kind == "html":
                    for key in _page_refs(value, client.base_url, course_id, str(found.get("source_url") or course_url)):
                        enqueue({"url": key, "discovered_from": [found]})
        normalized_pages = []
        resolved_ids = set()
        resolved_keys = set()
        cursor = 0
        while cursor < len(queue):
            page = queue[cursor]
            cursor += 1
            key = str(page["url"])
            if key in resolved_keys:
                continue
            if page.get("page_id") is not None and str(page["page_id"]) in resolved_ids:
                continue
            page_copy = dict(page)
            if "body" not in page_copy or page_copy.get("coverage_status") == "stale":
                old_page = prior_pages.get(key)
                fetched, ok = _safe_get(client, f"/api/v1/courses/{_id_path(course_id)}/pages/{quote(key, safe='')}",
                                       warnings=warnings, scope="course", code="page_body_unavailable",
                                       object_id=page.get("page_id") or key, fallback=old_page)
                if isinstance(fetched, Mapping):
                    page_copy.update(fetched)
                elif not ok:
                    page_copy["coverage_status"] = "unavailable"
            page_copy = _record_with_text(page_copy, _nested_source_url(client,
                f"/courses/{_id_path(course_id)}/pages/{quote(key, safe='')}", page_copy), ("body", "description"))
            actual_id = page_copy.get("page_id")
            if actual_id is not None and str(actual_id) in resolved_ids:
                continue
            if actual_id is not None:
                resolved_ids.add(str(actual_id))
                resolved_keys.add(str(actual_id))
            resolved_keys.add(key)
            if page_copy.get("url"):
                queued.add(str(page_copy["url"]))
                resolved_keys.add(str(page_copy["url"]))
            normalized_pages.append(page_copy)
            for linked_key in _page_refs(page_copy.get("body"), client.base_url, course_id, page_copy["source_url"]):
                enqueue({"url": linked_key, "discovered_from": [{"kind": "pages", "object_id": actual_id or key,
                          "source_url": page_copy["source_url"], "field": "body"}]})
        course["pages"] = normalized_pages
        external_by_url: dict[str, dict[str, Any]] = {}
        for page in normalized_pages:
            if not isinstance(page, Mapping):
                continue
            for ref in extract_external_resource_refs(page.get("body"), client.base_url, str(page.get("source_url") or course_url)):
                external_by_url.setdefault(ref["source_url"], ref)
        course["resources"] = list(external_by_url.values())

    if mode == "course":
        files, files_ok = _safe_list(
            client,
            f"/api/v1/courses/{_id_path(course_id)}/files",
            {"per_page": 100, "sort": "name"},
            warnings=warnings,
            scope="course",
            code="files_index_unavailable",
            object_id=course_id,
            fallback=prior.get("files", []),
        )
    else:
        # Hubs archive only files explicitly referenced by their current
        # announcements, readable replies, assignments, or calendar entries.
        files, files_ok = [], True
    normalized_files: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        if item.get("id") is None and item.get("source_id"):
            normalized = dict(item)
            normalized.setdefault("source_url", normalized.get("url"))
        elif item.get("id") is not None:
            normalized = _normalize_file_record(client, course_id, item)
        else:
            continue
        index_location = {"kind": "files_index", "object_id": course_id,
                          "source_url": course_url + "/files", "field": "files"}
        locations = list(normalized.get("discovered_from", []))
        if index_location not in locations:
            locations.append(index_location)
        normalized["discovered_from"] = locations
        by_source[str(normalized["source_id"])] = normalized
    # Syllabus/page/assignment links remain discoverable even when the
    # Files index is forbidden or omits referenced material.
    for ref in _file_refs_from_course(course, client.base_url, course_id):
        ref = _enrich_file_reference(client, ref, warnings=warnings, course_id=course_id)
        existing = by_source.get(str(ref["source_id"]))
        if existing is None:
            by_source[str(ref["source_id"])] = ref
        else:
            # A failed Files index commonly falls back to the prior
            # snapshot.  Overlay fresh reference metadata while retaining
            # only archive-local fields from that prior record; setdefault
            # would leave stale JSON metadata URLs and names in place.
            archive_fields = {
                "local_path",
                "sha256",
                "download_status",
                "extracted_text",
                "extraction_status",
                "source_marker",
            }
            locations = list(existing.get("discovered_from", []))
            locations.extend(location for location in ref.get("discovered_from", []) if location not in locations)
            for key, value in ref.items():
                if key not in archive_fields | {"discovered_from"} and value is not None:
                    existing[key] = value
            existing["discovered_from"] = locations
    download_files = bool(_cfg(config, "download_files", True))
    extract_documents = bool(_cfg(config, "extract_documents", True))
    limit = _cfg(config, "max_file_size", _cfg(config, "file_size_limit", DEFAULT_MAX_FILE_SIZE))
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_FILE_SIZE
        warnings.append(_warning("course", "invalid_file_size_limit", "invalid file size limit; default used", course_id))
    if limit is not None and limit < 0:
        limit = DEFAULT_MAX_FILE_SIZE
        warnings.append(_warning("course", "invalid_file_size_limit", "negative file size limit; default used", course_id))
    prior_files = prior.get("files", []) if isinstance(prior, Mapping) else []
    for source_id, file_record in by_source.items():
        if download_files and archive_dir is not None:
            archived = archive_file(
                client,
                file_record,
                archive_dir,
                previous=prior_files,
                max_file_size=limit,
                extract_documents=extract_documents,
            )
        else:
            archived = dict(file_record)
            archived["download_status"] = "not_requested"
        if archived.get("warning"):
            warnings.append(_warning("file", "file_archive_warning", str(archived["warning"]), source_id))
        normalized_files.append(archived)
    course["files"] = normalized_files
    if not files_ok and prior.get("files") and not normalized_files:
        course["files"] = list(prior["files"])
    course["coverage"] = summarize(getattr(client, "coverage_log", [])[coverage_start:])
    course["coverage"]["file_discovery"] = [{"id": item.get("id"), "source_id": item.get("source_id"),
        "discovered_from": item.get("discovered_from", []), "download_status": item.get("download_status"),
        "metadata_status": item.get("coverage_status", "unavailable")} for item in course["files"]]
    return course, list(warnings)


def collect_snapshot(
    client: CanvasClient,
    config: Any,
    archive_dir: Path | str,
    previous: Mapping[str, Any] | None = None,
    *,
    progress: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Collect a canonical Canvas snapshot using only read-only API calls.

    ``config`` must contain a ``term`` mapping.  Optional keys include
    ``course_modes``, ``course_ids``, ``download_files``,
    ``extract_documents``, and ``max_file_size``/``file_size_limit``.
    """

    from datetime import datetime, timezone

    term_value = _cfg(config, "term", {}) or {}
    coverage_start = len(getattr(client, "coverage_log", []))
    term = dict(term_value) if isinstance(term_value, Mapping) else {}
    warnings: list[dict[str, Any]] = []
    if not term.get("start"):
        warnings.append(_warning("config", "term_start_missing", "term.start is missing; announcement query may be broad"))

    # Always verify the owner against Canvas on each collection.  A prior user
    # object is only a recoverable display fallback when the profile endpoint
    # is temporarily unavailable.
    profile, profile_ok = _safe_get(
        client,
        "/api/v1/users/self/profile",
        warnings=warnings,
        scope="profile",
        code="profile_unavailable",
        fallback=None,
    )
    if not profile_ok or not isinstance(profile, Mapping) or profile.get("id") is None:
        profile, profile_ok = _safe_get(
            client,
            "/api/v1/users/self",
            warnings=warnings,
            scope="profile",
            code="profile_self_unavailable",
            fallback=(previous or {}).get("user", {}) if isinstance(previous, Mapping) else {},
        )
    profile = profile if isinstance(profile, Mapping) else {}
    expected_owner = _cfg(config, "owner_id")
    if expected_owner is not None:
        actual_owner = profile.get("id")
        if actual_owner is None:
            raise ValueError("Canvas owner identity could not be verified before collection")
        if str(actual_owner) != str(expected_owner):
            raise ValueError("Canvas identity differs from this instance; use a separate configuration")
    user = {
        "id": profile.get("id"),
        "name": profile.get("name") or profile.get("short_name") or profile.get("login_id") or "Student",
        "time_zone": profile.get("time_zone") or profile.get("timezone") or term.get("timezone"),
    }

    explicit_ids_value = _cfg(config, "course_ids", []) or []
    explicit_ids = {str(value) for value in explicit_ids_value}
    explicit_modes_value = _cfg(config, "course_modes", {}) or {}
    explicit_modes = {str(key): value for key, value in explicit_modes_value.items()} if isinstance(explicit_modes_value, Mapping) else {}
    for course_id in explicit_ids:
        explicit_modes.setdefault(course_id, "course")

    course_params: dict[str, Any] = {
        "per_page": 100,
        "include[]": ["term", "syllabus_body"],
        # Match the probe's student-visible enrollment scope.  Explicit
        # course_ids can still request a known course directly below, while
        # inactive/unpublished historical catalog rows stay out of a normal
        # sync run.
        "enrollment_state": _cfg(config, "enrollment_state", "active"),
    }
    raw_courses, courses_ok = _safe_list(
        client,
        "/api/v1/courses",
        course_params,
        warnings=warnings,
        scope="courses",
        code="courses_unavailable",
        fallback=(previous or {}).get("courses", []) if isinstance(previous, Mapping) else [],
    )
    if explicit_ids:
        raw_courses = [
            item
            for item in raw_courses
            if isinstance(item, Mapping) and str(item.get("id")) in explicit_ids
        ]
    else:
        raw_courses = [
            item
            for item in raw_courses
            if isinstance(item, Mapping)
            and _course_matches_enrollment_scope(item, course_params.get("enrollment_state", "active"))
        ]
    # If an explicitly requested course was not in the list, fetch it directly
    # so a filtered Canvas enrollment list cannot silently drop it.
    known_ids = {str(item.get("id")) for item in raw_courses if isinstance(item, Mapping) and item.get("id") is not None}
    for course_id in sorted(explicit_ids - known_ids):
        fetched, _ = _safe_get(
            client,
            f"/api/v1/courses/{_id_path(course_id)}",
            warnings=warnings,
            scope="course",
            code="course_unavailable",
            object_id=course_id,
            fallback=None,
        )
        if isinstance(fetched, Mapping):
            raw_courses.append(fetched)

    prior_by_id = _previous_courses(previous)
    snapshot_courses: list[dict[str, Any]] = []
    for raw in raw_courses:
        if not isinstance(raw, Mapping) or raw.get("id") is None:
            continue
        course_id = str(raw["id"])
        mode = _infer_mode(raw, term, explicit_modes)
        course, course_warnings = _collect_course(
            client,
            raw,
            mode,
            term,
            config,
            archive_dir,
            prior_by_id.get(course_id),
        ) if mode != "ignore" else (
            {
                **dict(raw),
                "id": raw.get("id"),
                "name": raw.get("name") or raw.get("course_name") or course_id,
                "course_code": raw.get("course_code") or raw.get("sis_course_id") or "",
                "mode": "ignore",
                "html_url": _course_url(client, raw),
                "announcements": [],
                "assignments": [],
                "modules": [],
                "pages": [],
                "files": [],
                "calendar_events": [],
                "warnings": [],
            },
            [],
        )
        if mode == "review":
            course.setdefault("warnings", []).append(
                _warning("course", "unexpected_term_review", "course term/title did not match the configured term; review before import", raw.get("id"))
            )
            course_warnings.append(course["warnings"][-1])
        snapshot_courses.append(course)
        warnings.extend(course_warnings)
        callback = progress or _cfg(config, "progress_callback")
        if callable(callback):
            try:
                callback(
                    {
                        "event": "course_collected",
                        "course_id": course.get("id"),
                        "name": course.get("name"),
                        "mode": course.get("mode"),
                        "counts": {
                            key: len(course.get(key, []) or [])
                            for key in ("announcements", "assignments", "modules", "pages", "files", "calendar_events")
                        },
                        "warning_count": len(course.get("warnings", []) or []),
                    }
                )
            except Exception as exc:
                warnings.append(_error_warning("collection", "progress_callback_failed", exc, course.get("id")))

    # Preserve a previous collection if course enumeration failed completely;
    # this keeps local archives and user review state visible during outages.
    if not courses_ok and not snapshot_courses and isinstance(previous, Mapping):
        snapshot_courses = [dict(course) for course in previous.get("courses", []) if isinstance(course, Mapping)]

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    hubs = [course for course in snapshot_courses if course.get("mode") == "hub"]
    stats = {
        "course_count": len(snapshot_courses),
        "formal_course_count": sum(course.get("mode") == "course" for course in snapshot_courses),
        "hub_count": len(hubs),
        "review_count": sum(course.get("mode") == "review" for course in snapshot_courses),
        "warning_count": len(warnings),
        "profile_verified": bool(profile_ok and profile.get("id") is not None),
        "courses_request_succeeded": bool(courses_ok),
    }
    return {
        "schema_version": 1,
        "canvas_origin": client.base_url,
        "generated_at": generated_at,
        "user": user,
        "term": term,
        "courses": snapshot_courses,
        "hubs": hubs,
        "warnings": warnings,
        "stats": stats,
        "coverage": summarize(getattr(client, "coverage_log", [])[coverage_start:]),
    }


__all__ = [
    "CANVAS_API_PREFIX",
    "CanvasClient",
    "CanvasDownloadTooLarge",
    "CanvasError",
    "CanvasHTTPError",
    "CanvasNetworkError",
    "CanvasOriginError",
    "CanvasResponseError",
    "collect_snapshot",
    "extract_html_file_refs",
    "extract_html_text",
    "html_to_text",
]
