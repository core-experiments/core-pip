"""Errors raised by cpip's network services."""

from __future__ import annotations

from typing import Literal

from cpip.core.errors import CpipError, DiagnosticCpipError


class NetworkConnectionError(CpipError):
    def __init__(self, error_msg: str, response=None, request=None) -> None:
        self.response = response
        self.request = request or getattr(response, "request", None)
        self.error_msg = error_msg
        super().__init__(error_msg, response, request)

    def __str__(self) -> str:
        return str(self.error_msg)


class IncompleteDownloadError(CpipError):
    """A network download ended before the expected number of bytes arrived."""


class ConnectionFailedError(DiagnosticCpipError):
    reference = "connection-failed"

    def __init__(self, url: str, host: str, error: Exception) -> None:
        super().__init__(
            message=(f"Failed to connect to {host} while fetching {url}"),
            context=str(error),
            hint_stmt=(
                "Are you connected to the Internet? If so, check whether your system "
                f"can connect to {host} before trying again."
            ),
        )


class ConnectionTimeoutError(DiagnosticCpipError):
    reference = "connection-timeout"

    def __init__(
        self,
        url: str,
        host: str,
        *,
        kind: Literal["connect", "read"],
        timeout: float,
    ) -> None:
        context = f"{host} didn't respond within {timeout} seconds"
        if kind == "connect":
            context += " (while establishing a connection)"
        super().__init__(message=f"Unable to fetch {url}", context=context)


class SSLMissingError(DiagnosticCpipError):
    reference = "ssl-missing"

    def __init__(self, url: str) -> None:
        super().__init__(
            message=f"Failed to establish a secure connection for {url}",
            context="The 'ssl' module is unavailable but required for HTTPS URLs",
        )


class SSLVerificationError(DiagnosticCpipError):
    reference = "ssl-verification-failed"

    def __init__(self, url: str, host: str, error: Exception) -> None:
        super().__init__(
            message=(
                f"Failed to establish a secure connection to {host} while fetching {url}"
            ),
            context=str(error),
            hint_stmt="You may need to use --cert or check your proxy/firewall configuration",
        )


class ProxyConnectionError(DiagnosticCpipError):
    reference = "proxy-connection-failed"

    def __init__(self, url: str, proxy: str, error: Exception) -> None:
        super().__init__(
            message=(f"Failed to connect to proxy {proxy} while fetching {url}"),
            context=str(error),
            hint_stmt="This is likely a proxy configuration issue.",
        )


class InvalidWheel(CpipError):
    def __init__(self, location: str, name: str) -> None:
        self.location = location
        self.name = name

    def __str__(self) -> str:
        return f"Wheel '{self.name}' located at {self.location} is invalid."


__all__ = [
    "ConnectionFailedError",
    "ConnectionTimeoutError",
    "IncompleteDownloadError",
    "InvalidWheel",
    "NetworkConnectionError",
    "ProxyConnectionError",
    "SSLMissingError",
    "SSLVerificationError",
]
