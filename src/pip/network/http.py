"""Small standard-library HTTP primitives used by pip."""

from __future__ import annotations

import base64
import email.message
import email.utils
import io
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pip.core.urls import redact_auth_from_url
from pip.network.exceptions import (
    ConnectionFailedError,
    ConnectionTimeoutError,
    NetworkConnectionError,
    SSLVerificationError,
)
from pip.network.cache import SafeFileCache

logger = logging.getLogger(__name__)


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


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
            "Accept-Encoding": "identity",
        }
        self.proxies: dict[str, str] | None = None
        self.pip_proxy: str | None = None
        self.pip_no_proxy_env = False
        self.pip_custom_cert: str | None = None
        self.pip_client_cert: str | None = None
        self.verify: bool | str = True
        self.cert: str | None = None
        self.retries = retries
        self.resume_retries = resume_retries
        from pip.network.auth import MultiDomainBasicAuth

        self.auth: Any = MultiDomainBasicAuth(index_urls=index_urls)
        self.cache = SafeFileCache(cache) if isinstance(cache, str) else cache
        self.trusted_hosts = {host.lower().split(":", 1)[0] for host in trusted_hosts}

    @staticmethod
    def user_agent() -> str:
        import platform

        from pip.core.pip_version import get_pip_version

        return f"pip/{get_pip_version()} Python/{platform.python_version()}"

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
        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        request_url = url
        username: str | None = None
        password: str | None = None
        if self.auth is not None and hasattr(self.auth, "get_url_and_credentials"):
            request_url, username, password = self.auth.get_url_and_credentials(url)
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            request_headers["Authorization"] = f"Basic {token}"

        request = HttpRequest(method, request_url, request_headers, data)
        if method == "GET" and not stream and "Range" not in request_headers:
            cached = self.cached_response(request)
            if cached is not None:
                return cached
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                response = self.open_internal(request, timeout=timeout)
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
            if (
                response.status_code in {500, 502, 503, 520, 527}
                and attempt + 1 < attempts
            ):
                response.close()
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code == 401 and self.auth is not None:
                retry = self.retry_auth(response, request, headers or {}, data, timeout)
                if retry is not None:
                    return retry
            if method == "GET" and not stream and response.status_code == 200:
                self.cache_response(response)
            return response
        raise AssertionError("unreachable")

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
            self.cache.delete(request.url)
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
        expires_at: float | None = None
        for directive in directives:
            if directive.startswith("max-age="):
                try:
                    expires_at = time.time() + max(0, int(directive[8:]))
                except ValueError:
                    pass
                break
        if expires_at is None:
            expires = response.headers.get("Expires")
            if expires:
                try:
                    parsed_expires = email.utils.parsedate_to_datetime(expires)
                except (TypeError, ValueError, OverflowError):
                    parsed_expires = None
                if parsed_expires is not None:
                    expires_at = parsed_expires.timestamp()
        self.cache.set(
            response.url,
            json.dumps(
                {
                    "status": response.status_code,
                    "reason": response.reason,
                    "url": response.url,
                    "headers": dict(response.headers.items()),
                    "expires_at": expires_at,
                }
            ).encode("utf-8"),
        )
        self.cache.set_body(response.url, body)
        response.raw = io.BytesIO(body)
        response.content_internal = body

    def open_internal(self, request: HttpRequest, timeout: Any) -> HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        context = None
        if parsed.hostname and parsed.hostname.lower() in self.trusted_hosts:
            context = ssl.create_unverified_context()
        elif self.verify is False:
            context = ssl.create_unverified_context()
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
        return HttpResponse(
            status_code=getattr(raw, "status", getattr(raw, "code", 200)),
            reason=getattr(raw, "reason", "OK"),
            url=raw.geturl(),
            headers=headers,
            raw=raw,
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
