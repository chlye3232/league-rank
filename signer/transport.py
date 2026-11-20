"""HTTP transport supporting wreq (Chrome/BoringSSL TLS) with requests fallback."""
from __future__ import annotations

import datetime
import gzip
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from .session_state import DEFAULT_COOKIE_ORIGINS, dump_cookie_jar, restore_cookie_jar
from .signer import MetaSecSigner

try:
    from wreq import Cookie, Method, Proxy
    from wreq.blocking import Client as WreqBlockingClient
    from wreq.exceptions import (
        BodyError,
        ConnectionError as WreqConnectionError,
        ConnectionResetError as WreqConnectionResetError,
        ProxyConnectionError,
        RedirectError,
        RequestError as WreqRequestError,
        TimeoutError as WreqTimeoutError,
        TlsError,
    )
    from wreq.redirect import Policy

    WREQ_AVAILABLE = True
    _WREQ_ERRORS = (
        BodyError,
        WreqConnectionError,
        WreqConnectionResetError,
        ProxyConnectionError,
        RedirectError,
        WreqRequestError,
        WreqTimeoutError,
        TlsError,
    )
except ImportError:
    Cookie = None  # type: ignore[assignment]
    Method = None  # type: ignore[assignment]
    Proxy = None  # type: ignore[assignment]
    WreqBlockingClient = None  # type: ignore[assignment]
    Policy = None  # type: ignore[assignment]
    WREQ_AVAILABLE = False
    _WREQ_ERRORS = ()


class TransportError(RuntimeError):
    """Raised when an HTTP transport fails at the network/TLS/proxy layer."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    content: bytes

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return default

    def json(self) -> Any:
        raw = self.content
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def _header_text(value: Any) -> str:
    if isinstance(value, memoryview):
        return bytes(value).decode("latin1")
    if isinstance(value, bytes):
        return value.decode("latin1")
    return str(value)


def _read_wreq_response(response: Any) -> HttpResponse:
    raw_bytes = bytes(response.bytes())
    response_headers: dict[str, str] = {}
    for name in response.headers.keys():
        key = _header_text(name)
        val = response.headers.get(key)
        if val is not None:
            response_headers[key] = _header_text(val)
    return HttpResponse(
        status=response.status.as_int(),
        url=str(response.url),
        headers=response_headers,
        content=raw_bytes,
    )


class SignedSession(MetaSecSigner):
    """Stateful HTTP session with TikTok 47.7.2 signing, cookies, and proxy support."""

    def __init__(
        self,
        persona: Mapping[str, Any],
        *,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
        proxies: Iterable[Any] | None = None,
        force_requests: bool = False,
    ):
        super().__init__(persona)
        self.timeout = max(1.0, float(timeout))
        self.default_headers = {
            str(name).lower(): str(value)
            for name, value in dict(headers or {}).items()
        }
        self._use_wreq = WREQ_AVAILABLE and not force_requests
        self._closed = False
        if self._use_wreq:
            client_kwargs: dict[str, Any] = {
                "cookie_store": True,
                "redirect": Policy.limited(5),
                "timeout": datetime.timedelta(seconds=self.timeout),
            }
            if proxies is not None:
                client_kwargs["proxies"] = [
                    Proxy.all(item) if isinstance(item, str) else item for item in proxies
                ]
            self._wreq_client = WreqBlockingClient(**client_kwargs)
            self._requests_session = None
        else:
            if requests is None:
                raise RuntimeError("Either wreq or requests must be installed")
            self._wreq_client = None
            self._requests_session = requests.Session()
            if proxies:
                first = next(iter(proxies), None)
                if isinstance(first, str) and first.strip():
                    self._requests_session.proxies.update({"http": first, "https": first})

    @property
    def backend_name(self) -> str:
        return "wreq" if self._use_wreq else "requests"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def cookie_jar(self) -> Any:
        self._ensure_active()
        if self._use_wreq:
            jar = self._wreq_client.cookie_jar
            if jar is None:
                raise RuntimeError("wreq cookie store is unavailable")
            return jar
        return self._requests_session.cookies

    def _ensure_active(self) -> None:
        if self._closed:
            raise RuntimeError("SignedSession is closed")

    def update_persona(self, updates: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        if updates:
            self.persona.update(updates)
        if kwargs:
            self.persona.update(kwargs)
        return dict(self.persona)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        proxy_url: str | None = None,
        sign: bool = True,
        replay: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        self._ensure_active()
        merged_headers = dict(self.default_headers)
        merged_headers.update({str(name).lower(): str(value) for name, value in dict(headers or {}).items()})
        if sign:
            signed = self.sign_request(url, method=method, headers=merged_headers, body=body, **dict(replay or {}))
            request_headers = dict(merged_headers)
            request_headers.update(signed["headers"])
        else:
            request_headers = merged_headers

        effective_timeout = max(1.0, float(self.timeout if timeout is None else timeout))
        if self._use_wreq:
            options: dict[str, Any] = {
                "headers": request_headers,
                "timeout": datetime.timedelta(seconds=effective_timeout),
            }
            if body:
                options["body"] = body
            if proxy_url:
                options["proxy"] = Proxy.all(proxy_url)
            try:
                with self._wreq_client.request(getattr(Method, method.upper()), url, **options) as response:
                    return _read_wreq_response(response)
            except _WREQ_ERRORS as error:
                raise TransportError(str(error)) from error

        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = self._requests_session.request(
                method.upper(),
                url,
                headers=request_headers,
                data=body if body else None,
                timeout=effective_timeout,
                proxies=proxies,
            )
            return HttpResponse(
                status=resp.status_code,
                url=str(resp.url),
                headers=dict(resp.headers),
                content=resp.content,
            )
        except Exception as error:
            if requests is not None and isinstance(error, requests.RequestException):
                raise TransportError(str(error)) from error
            raise

    def get(self, url: str, **options: Any) -> HttpResponse:
        return self.request("GET", url, **options)

    def post(self, url: str, **options: Any) -> HttpResponse:
        return self.request("POST", url, **options)

    def dump_cookies(
        self,
        origins: Iterable[str] = DEFAULT_COOKIE_ORIGINS,
        *,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        return dump_cookie_jar(self.cookie_jar, origins, strict=strict)

    def restore_cookies(self, snapshots: Iterable[Mapping[str, Any]]) -> None:
        restore_cookie_jar(self.cookie_jar, snapshots)

    def get_cookies(self, url: str) -> dict[str, str]:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise ValueError("cookie URL must include a host")
        host = host.lower()
        request_path = parts.path or "/"
        cookies: dict[str, str] = {}
        if self._use_wreq:
            for cookie in self.cookie_jar.get_all():
                cookie_path = cookie.path or "/"
                if request_path != cookie_path and not request_path.startswith(cookie_path.rstrip("/") + "/"):
                    continue
                if cookie.secure and parts.scheme != "https":
                    continue
                if cookie.domain is None:
                    lookup = urlunsplit((parts.scheme, parts.netloc, cookie_path, "", ""))
                    if self.cookie_jar.get(cookie.name, lookup) is None:
                        continue
                else:
                    domain = cookie.domain.lstrip(".").lower()
                    if host != domain and not host.endswith("." + domain):
                        continue
                cookies[cookie.name] = cookie.value
            return cookies

        for cookie in self.cookie_jar:
            cookie_path = getattr(cookie, "path", "/") or "/"
            if request_path != cookie_path and not request_path.startswith(cookie_path.rstrip("/") + "/"):
                continue
            domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
            if domain and host != domain and not host.endswith("." + domain):
                continue
            cookies[cookie.name] = cookie.value
        return cookies

    def set_cookie(self, name: str, value: str, url: str) -> None:
        if self._use_wreq:
            self.cookie_jar.add(Cookie(name, value, path="/"), url)
        else:
            host = urlsplit(url).hostname or ""
            self.cookie_jar.set(name, value, domain=host, path="/")

    def clear_cookies(self) -> None:
        self.cookie_jar.clear()

    def close(self) -> None:
        if not self._closed:
            if self._wreq_client is not None:
                self._wreq_client.close()
            if self._requests_session is not None:
                self._requests_session.close()
            self._closed = True

    def __enter__(self) -> SignedSession:
        self._ensure_active()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
