"""Small standard-library HTTP primitives used by cpip."""

from __future__ import annotations

import base64
import email.message
import email.utils
import gzip
import io
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from cpip.network.exceptions import (
    ConnectionFailedError,
    ConnectionTimeoutError,
    NetworkConnectionError,
    SSLVerificationError,
)
logger = logging.getLogger(__name__)
RETRY_STATUS_CODES = frozenset((500, 502, 503, 520, 527))
MAX_IDLE_CONNECTIONS_PER_ORIGIN = 8


class HttpRequest:
    __slots__ = ("method", "url", "headers", "body")

    def __init__(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = headers if headers is not None else {}
        self.body = body


class HttpResponse:
    """A transport-neutral HTTP response backed by a binary stream."""

    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        url: str,
        headers: Mapping[str, str] | email.message.Message,
        raw: Any,
        request: HttpRequest | None = None,
        history: list[HttpResponse] | None = None,
        from_cache: bool = False,
    ) -> None:
        self.status_code = status_code
        self.reason = reason
        self.url = url
        self.headers = headers
        self.raw = raw
        self.request = request
        self.history = history or []
        self.from_cache = from_cache
        self.content_internal: bytes | None = None

    @property
    def content(self) -> bytes:
        if self.content_internal is None:
            self.content_internal = self.raw.read()
        return self.content_internal

    @property
    def text(self) -> str:
        content_type = self.headers.get("Content-Type", "")
        charset = "utf-8"
        for value in content_type.split(";")[1:]:
            key, separator, encoding = value.strip().partition("=")
            if separator and key.lower() == "charset":
                charset = encoding.strip().strip('"') or charset
                break
        return self.content.decode(charset, "replace")

    def iter_content(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        while True:
            chunk = self.raw.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        kind = "Client" if self.status_code < 500 else "Server"
        raise NetworkConnectionError(
            f"{self.status_code} {kind} Error: {self.reason} for url: {self.url}",
            response=self,
        )

    def close(self) -> None:
        close = getattr(self.raw, "close", None)
        if close is not None:
            close()


class PersistentConnectionPool:
    """A bounded pool of reusable connections for one HTTP origin."""

    def __init__(self, create_connection: Any, max_connections: int) -> None:
        self.create_connection = create_connection
        self.max_connections = max_connections
        self.condition = threading.Condition()
        self.idle: list[Any] = []
        self.created = 0

    def acquire(self) -> Any:
        with self.condition:
            while not self.idle and self.created >= self.max_connections:
                self.condition.wait()
            if self.idle:
                return self.idle.pop()
            self.created += 1
        try:
            return self.create_connection()
        except BaseException:
            with self.condition:
                self.created -= 1
                self.condition.notify()
            raise

    def release(self, connection: Any) -> None:
        with self.condition:
            self.idle.append(connection)
            self.condition.notify()

    def discard(self, connection: Any) -> None:
        del connection
        with self.condition:
            self.created -= 1
            self.condition.notify()

    def close(self) -> None:
        with self.condition:
            idle = self.idle
            self.idle = []
            self.created -= len(idle)
        for connection in idle:
            connection.close()


class InFlightRequest:
    """State shared by callers waiting for one network request."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: tuple[int, str, str, Mapping[str, str] | email.message.Message, bytes] | None = None
        self.error: BaseException | None = None


class NetworkStats:
    """Optional counters for diagnosing network behavior."""

    __slots__ = (
        "cache_hits",
        "coalesced_waiters",
        "network_requests",
        "catalog_requests",
        "metadata_requests",
        "pypi_json_requests",
        "artifact_requests",
        "other_requests",
    )

    def __init__(self) -> None:
        self.cache_hits = 0
        self.coalesced_waiters = 0
        self.network_requests = 0
        self.catalog_requests = 0
        self.metadata_requests = 0
        self.pypi_json_requests = 0
        self.artifact_requests = 0
        self.other_requests = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "coalesced_waiters": self.coalesced_waiters,
            "network_requests": self.network_requests,
            "catalog_requests": self.catalog_requests,
            "metadata_requests": self.metadata_requests,
            "pypi_json_requests": self.pypi_json_requests,
            "artifact_requests": self.artifact_requests,
            "other_requests": self.other_requests,
        }


def request_kind(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if "/simple/" in path and path.endswith("/"):
        return "catalog"
    if path.endswith(".metadata"):
        return "metadata"
    if "/pypi/" in path and path.endswith("/json"):
        return "pypi_json"
    if path.endswith((".whl", ".tar.gz", ".zip", ".tar.bz2", ".tar.xz")):
        return "artifact"
    return "other"


def decode_response_body(body: bytes, headers: Any) -> bytes:
    if str(headers.get("Content-Encoding", "")).lower() != "gzip":
        return body
    body = gzip.decompress(body)
    del headers["Content-Encoding"]
    headers["Content-Length"] = str(len(body))
    return body


def timeout_value(
    timeout: float | tuple[float | None, float | None] | None,
) -> float | None:
    if isinstance(timeout, tuple):
        return timeout[0] or timeout[1]
    return timeout


class NetworkSession:
    """HTTP(S) client implemented entirely with Python's standard library."""

    timeout: float | tuple[float | None, float | None] | None = None

    def __init__(
        self,
        *,
        retries: int = 0,
        resume_retries: int = 0,
        trusted_hosts: Sequence[str] = (),
        index_urls: list[str] | None = None,
        cache: Any = None,
    ) -> None:
        self.headers: dict[str, str] = {
            "User-Agent": self.user_agent(),
            "Accept-Encoding": "gzip",
        }
        self.proxies: dict[str, str] | None = None
        self.cpip_proxy: str | None = None
        self.cpip_no_proxy_env = False
        self.cpip_custom_cert: str | None = None
        self.cpip_client_cert: str | None = None
        self.verify: bool | str = True
        self.cert: str | None = None
        self.retries = retries
        self.resume_retries = resume_retries
        from cpip.network.auth import MultiDomainBasicAuth

        self.auth: Any = MultiDomainBasicAuth(index_urls=index_urls)
        if isinstance(cache, str):
            from cpip.network.cache import SafeFileCache

            self.cache = SafeFileCache(cache)
        else:
            self.cache = cache
        self.trusted_hosts = {host.lower().split(":", 1)[0] for host in trusted_hosts}
        self.connection_pools: dict[tuple[Any, ...], PersistentConnectionPool] = {}
        self.connection_pools_lock = threading.Lock()
        self.inflight_requests: dict[tuple[Any, ...], InFlightRequest] = {}
        self.inflight_requests_lock = threading.Lock()
        self.network_stats = (
            NetworkStats()
            if os.environ.get("CPIP_BENCH_NETWORK_STATS") == "1"
            else None
        )

    @staticmethod
    def user_agent() -> str:
        import platform

        from cpip.core._execution_context import current_version

        version = current_version()
        if version is None:
            from cpip.core.cpip_version import get_cpip_version

            version = get_cpip_version()

        return f"cpip/{version} Python/{platform.python_version()}"

    def add_trusted_host(
        self, host: str, source: str | None = None, suppress_logging: bool = False
    ) -> None:
        del source, suppress_logging
        self.trusted_hosts.add(host.lower().split(":", 1)[0])

    def update_index_urls(self, new_index_urls: list[str]) -> None:
        if self.auth is not None:
            self.auth.index_urls = new_index_urls

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("HEAD", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
    ) -> HttpResponse:
        from cpip.core.urls import redact_auth_from_url

        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        if stream:
            request_headers["Accept-Encoding"] = "identity"
        request_url = url
        username: str | None = None
        password: str | None = None
        if self.auth is not None and hasattr(self.auth, "get_url_and_credentials"):
            request_url, username, password = self.auth.get_url_and_credentials(url)
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            request_headers["Authorization"] = f"Basic {token}"

        request = HttpRequest(method, request_url, request_headers, data)
        cached_metadata = None
        if method == "GET" and not stream and "Range" not in request_headers:
            cached = self.cached_response(request)
            if cached is not None:
                if self.network_stats is not None:
                    self.network_stats.cache_hits += 1
                return cached
            cached_metadata = self.stale_cache_metadata(request)
            if cached_metadata is not None:
                for name in ("etag", "last-modified"):
                    value = cached_metadata.get(name)
                    if value:
                        request_headers[name.title()] = str(value)
                request.headers = request_headers
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                response = self.open_coalesced(request, timeout=timeout, stream=stream)
            except TimeoutError as exc:
                if attempt + 1 == attempts:
                    raise ConnectionTimeoutError(
                        redact_auth_from_url(request_url),
                        urllib.parse.urlsplit(request_url).hostname or "unknown host",
                        kind="connect",
                        timeout=timeout_value(timeout or self.timeout) or 0,
                    ) from exc
                time.sleep(0.25 * (2**attempt))
                continue
            except ssl.SSLError as exc:
                raise SSLVerificationError(
                    redact_auth_from_url(request_url),
                    urllib.parse.urlsplit(request_url).hostname or "unknown host",
                    exc,
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt + 1 == attempts:
                    raise ConnectionFailedError(
                        redact_auth_from_url(request_url),
                        urllib.parse.urlsplit(request_url).hostname or "unknown host",
                        exc,
                    ) from exc
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in RETRY_STATUS_CODES and attempt + 1 < attempts:
                response.close()
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code == 401 and self.auth is not None:
                retry = self.retry_auth(response, request, headers or {}, data, timeout)
                if retry is not None:
                    return retry
            if response.status_code == 304 and cached_metadata is not None:
                response.close()
                return self.revalidated_response(request, cached_metadata)
            if method == "GET" and not stream and response.status_code == 200:
                self.cache_response(response)
            return response
        raise AssertionError("unreachable")

    def open_coalesced(
        self, request: HttpRequest, timeout: Any, *, stream: bool = False
    ) -> HttpResponse:
        if request.method != "GET" or stream or "Range" in request.headers:
            return self.open_internal(request, timeout, stream=stream)

        key = (
            request.method,
            request.url,
            tuple(sorted((name.lower(), value) for name, value in request.headers.items())),
        )
        with self.inflight_requests_lock:
            flight = self.inflight_requests.get(key)
            if flight is None:
                flight = InFlightRequest()
                self.inflight_requests[key] = flight
                owner = True
            else:
                owner = False

        if not owner:
            if self.network_stats is not None:
                self.network_stats.coalesced_waiters += 1
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.response is None:
                raise NetworkConnectionError(
                    f"coalesced request completed without a response: {request.url}"
                )
            status, reason, url, headers, body = flight.response
            return HttpResponse(
                status_code=status,
                reason=reason,
                url=url,
                headers=headers,
                raw=io.BytesIO(body),
                request=request,
            )

        try:
            if self.network_stats is not None:
                self.network_stats.network_requests += 1
                kind = request_kind(request.url)
                setattr(
                    self.network_stats,
                    f"{kind}_requests",
                    getattr(self.network_stats, f"{kind}_requests") + 1,
                )
            response = self.open_internal(request, timeout, stream=stream)
            body = response.content
            flight.response = (
                response.status_code,
                response.reason,
                response.url,
                response.headers,
                body,
            )
            return response
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self.inflight_requests_lock:
                self.inflight_requests.pop(key, None)
            flight.event.set()

    def cached_response(self, request: HttpRequest) -> HttpResponse | None:
        if self.cache is None:
            return None
        metadata = self.cache.get(request.url)
        body = self.cache.get_body(request.url)
        if metadata is None or body is None:
            if body is not None:
                body.close()
            return None
        try:
            values = json.loads(metadata.decode("utf-8"))
            headers = values["headers"]
            status = int(values["status"])
            reason = str(values["reason"])
            expires_at = values.get("expires_at")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            body.close()
            return None
        if expires_at is not None and float(expires_at) <= time.time():
            body.close()
            return None
        response_headers = email.message.Message()
        for name, value in headers.items():
            response_headers[name] = str(value)
        return HttpResponse(
            status_code=status,
            reason=reason,
            url=request.url,
            headers=response_headers,
            raw=body,
            request=request,
            from_cache=True,
        )

    def stale_cache_metadata(self, request: HttpRequest) -> dict[str, Any] | None:
        if self.cache is None:
            return None
        metadata = self.cache.get(request.url)
        if metadata is None:
            return None
        try:
            values = json.loads(metadata.decode("utf-8"))
            if not isinstance(values, dict):
                return None
            expires_at = values.get("expires_at")
            if expires_at is None or float(expires_at) > time.time():
                return None
            return values
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def revalidated_response(
        self, request: HttpRequest, metadata: dict[str, Any]
    ) -> HttpResponse:
        headers = metadata.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        expires_at = self.cache_expiry(headers)
        if expires_at is None:
            expires_at = time.time()
        updated = dict(metadata)
        updated["expires_at"] = expires_at
        self.cache.set(request.url, json.dumps(updated).encode("utf-8"))
        body = self.cache.get_body(request.url)
        if body is None:
            raise NetworkConnectionError(
                f"Cached response body missing for url: {request.url}"
            )
        response_headers = email.message.Message()
        for name, value in headers.items():
            response_headers[name] = str(value)
        return HttpResponse(
            status_code=int(metadata.get("status", 200)),
            reason=str(metadata.get("reason", "OK")),
            url=request.url,
            headers=response_headers,
            raw=body,
            request=request,
            from_cache=True,
        )

    @staticmethod
    def cache_expiry(
        headers: Mapping[str, str] | email.message.Message,
    ) -> float | None:
        cache_control = headers.get("Cache-Control", "")
        for directive in cache_control.split(","):
            directive = directive.strip().lower()
            if directive.startswith("max-age="):
                try:
                    return time.time() + max(0, int(directive[8:]))
                except ValueError:
                    break
        expires = headers.get("Expires")
        if expires:
            try:
                return email.utils.parsedate_to_datetime(expires).timestamp()
            except (TypeError, ValueError, OverflowError):
                pass
        return None

    def cache_response(self, response: HttpResponse) -> None:
        if self.cache is None:
            return
        cache_control = response.headers.get("Cache-Control", "")
        directives = {
            part.strip().lower() for part in cache_control.split(",") if part.strip()
        }
        if "no-store" in directives:
            return
        body = response.content
        expires_at = self.cache_expiry(response.headers)
        self.cache.set(
            response.url,
            json.dumps(
                {
                    "status": response.status_code,
                    "reason": response.reason,
                    "url": response.url,
                    "headers": dict(response.headers.items()),
                    "expires_at": expires_at,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
            ).encode("utf-8"),
        )
        self.cache.set_body(response.url, body)
        response.raw = io.BytesIO(body)
        response.content_internal = body

    def open_internal(
        self, request: HttpRequest, timeout: Any, *, stream: bool = False
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        if (
            not stream
            and parsed.scheme in {"http", "https"}
            and self.proxies is None
        ):
            return self.open_persistent(request, parsed, timeout)
        return self.open_with_urllib(request, timeout, stream=stream)

    def open_persistent(
        self,
        request: HttpRequest,
        parsed: urllib.parse.SplitResult,
        timeout: Any,
    ) -> HttpResponse:
        import http.client

        hostname = parsed.hostname
        if hostname is None:
            return self.open_with_urllib(request, timeout)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        key = (parsed.scheme, hostname, port, str(self.verify), self.cert)

        def create_connection() -> Any:
            if parsed.scheme == "https":
                if hostname.lower() in self.trusted_hosts or self.verify is False:
                    context = ssl._create_unverified_context()
                else:
                    context = ssl.create_default_context(
                        cafile=self.verify if isinstance(self.verify, str) else None
                    )
                    if self.cert:
                        context.load_cert_chain(self.cert)
                return http.client.HTTPSConnection(
                    hostname, port, context=context, timeout=timeout_value(timeout or self.timeout)
                )
            return http.client.HTTPConnection(
                hostname, port, timeout=timeout_value(timeout or self.timeout)
            )

        with self.connection_pools_lock:
            pool = self.connection_pools.get(key)
            if pool is None:
                pool = PersistentConnectionPool(
                    create_connection, MAX_IDLE_CONNECTIONS_PER_ORIGIN
                )
                self.connection_pools[key] = pool
        connection = pool.acquire()
        connection.timeout = timeout_value(timeout or self.timeout)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        try:
            connection.request(
                request.method,
                target,
                body=request.body,
                headers=request.headers,
            )
            raw = connection.getresponse()
            body = raw.read()
            body = decode_response_body(body, raw.msg)
            if raw.status in range(300, 400):
                connection.close()
                pool.discard(connection)
                return self.open_with_urllib(request, timeout)
            if raw.will_close:
                connection.close()
                pool.discard(connection)
            else:
                pool.release(connection)
            return HttpResponse(
                status_code=raw.status,
                reason=raw.reason,
                url=request.url,
                headers=raw.msg,
                raw=io.BytesIO(body),
                request=request,
            )
        except (http.client.HTTPException, OSError) as exc:
            connection.close()
            pool.discard(connection)
            raise urllib.error.URLError(exc) from exc

    def open_with_urllib(
        self, request: HttpRequest, timeout: Any, *, stream: bool = False
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        context = None
        if parsed.hostname and parsed.hostname.lower() in self.trusted_hosts:
            context = getattr(ssl, "create_unverified_context")()
        elif self.verify is False:
            context = getattr(ssl, "create_unverified_context")()
        else:
            context = ssl.create_default_context(
                cafile=self.verify if isinstance(self.verify, str) else None
            )
            if self.cert:
                context.load_cert_chain(self.cert)
        opener_handlers: list[Any] = []
        if self.proxies is not None:
            opener_handlers.append(urllib.request.ProxyHandler(self.proxies))
        if parsed.scheme == "https":
            opener_handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*opener_handlers)
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        try:
            raw = opener.open(req, timeout=timeout_value(timeout or self.timeout))
        except urllib.error.HTTPError as exc:
            raw = exc
        except urllib.error.URLError as exc:
            if parsed.scheme != "file":
                raise
            reason = type(exc.reason).__name__
            headers = email.message.Message()
            raw = io.BytesIO(f"{reason}: {exc.reason}".encode())
            return HttpResponse(
                status_code=404,
                reason=reason,
                url=request.url,
                headers=headers,
                raw=raw,
                request=request,
            )
        headers = raw.headers
        body = raw if stream else io.BytesIO(decode_response_body(raw.read(), headers))
        return HttpResponse(
            status_code=getattr(raw, "status", getattr(raw, "code", 200)),
            reason=getattr(raw, "reason", "OK"),
            url=raw.geturl(),
            headers=headers,
            raw=body,
            request=request,
        )

    def retry_auth(
        self,
        response: HttpResponse,
        request: HttpRequest,
        headers: Mapping[str, str],
        data: bytes | None,
        timeout: Any,
    ) -> HttpResponse | None:
        if not hasattr(self.auth, "credentials_after_401"):
            return None
        username, password, credentials = self.auth.credentials_after_401(response.url)
        if username is None or password is None:
            return None
        retry_headers = dict(headers)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        retry_headers["Authorization"] = f"Basic {token}"
        response.close()
        retry = self.request(
            request.method,
            response.url,
            headers=retry_headers,
            data=data,
            timeout=timeout,
        )
        if credentials is not None and retry.status_code < 400:
            try:
                self.auth.keyring_provider.save_auth_info(
                    credentials.url, credentials.username, credentials.password
                )
            except Exception:
                logger.exception("Failed to save credentials")
        return retry
