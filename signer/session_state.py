"""Cookie jar serialization and persistent TikTok session state."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .profiles import default_kr_persona
from .ticket_guard import TicketGuardState

try:
    from wreq import Cookie as WreqCookie, Jar as WreqJar, SameSite as WreqSameSite

    WREQ_COOKIES_AVAILABLE = True
except ImportError:
    WreqCookie = None  # type: ignore[assignment]
    WreqJar = None  # type: ignore[assignment]
    WreqSameSite = None  # type: ignore[assignment]
    WREQ_COOKIES_AVAILABLE = False

DEFAULT_COOKIE_ORIGINS = (
    "https://webcast22-normal-c-alisg.tiktokv.com",
    "https://api16-normal-c-alisg.tiktokv.com",
    "https://log-va.tiktokv.com",
    "https://www.tiktok.com",
)


def _origin(url: str, path: str = "/") -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname is None:
        raise ValueError(f"cookie origin must be an HTTP(S) URL: {url!r}")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def token_sha256(token: str) -> str:
    """Return SHA-256 hex digest of a non-empty token (used to detect Secret rotation)."""
    cleaned = token.strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest() if cleaned else ""


def dump_cookie_jar(jar: Any, origins: Iterable[str], *, strict: bool = False) -> list[dict[str, Any]]:
    """Serialize cookies from either a wreq.Jar or a requests RequestsCookieJar."""
    if WREQ_COOKIES_AVAILABLE and isinstance(jar, WreqJar):
        return _dump_wreq_jar(jar, origins, strict=strict)
    return _dump_requests_jar(jar)


def _dump_wreq_jar(jar: Any, origins: Iterable[str], *, strict: bool = False) -> list[dict[str, Any]]:
    normalized_origins = [_origin(origin) for origin in origins]
    snapshots = []
    for cookie in jar.get_all():
        path = cookie.path or "/"
        if cookie.domain is not None:
            origin = _origin(f"https://{cookie.domain.lstrip('.')}", path)
        else:
            candidates = [_origin(item, path) for item in normalized_origins]
            origin = next((candidate for candidate in candidates if jar.get(cookie.name, candidate) is not None), None)
            if origin is None:
                if strict:
                    raise RuntimeError(f"cannot determine host-only origin for cookie {cookie.name!r}")
                continue
        snapshots.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "maxAge": cookie.max_age.total_seconds() if cookie.max_age is not None else None,
            "expires": cookie.expires.isoformat() if cookie.expires is not None else None,
            "httpOnly": cookie.http_only,
            "secure": cookie.secure,
            "sameSite": "lax" if cookie.same_site_lax else "strict" if cookie.same_site_strict else None,
            "url": origin,
        })
    return snapshots


def _dump_requests_jar(jar: Any) -> list[dict[str, Any]]:
    snapshots = []
    for cookie in jar:
        domain = getattr(cookie, "domain", None)
        path = getattr(cookie, "path", "/") or "/"
        expires_ts = getattr(cookie, "expires", None)
        expires_iso = (
            datetime.fromtimestamp(expires_ts, tz=timezone.utc).isoformat()
            if isinstance(expires_ts, (int, float)) and expires_ts > 0
            else None
        )
        scheme = "https" if getattr(cookie, "secure", False) else "https"
        clean_domain = str(domain or "tiktokv.com").lstrip(".")
        snapshots.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain,
            "path": path,
            "maxAge": None,
            "expires": expires_iso,
            "httpOnly": bool(getattr(cookie, "_rest", {}).get("HttpOnly") is not None),
            "secure": bool(getattr(cookie, "secure", False)),
            "sameSite": None,
            "url": f"{scheme}://{clean_domain}{path}",
        })
    return snapshots


def restore_cookie_jar(jar: Any, snapshots: Iterable[Mapping[str, Any]]) -> None:
    """Restore serialized cookie snapshots into either wreq.Jar or RequestsCookieJar."""
    if WREQ_COOKIES_AVAILABLE and isinstance(jar, WreqJar):
        _restore_wreq_jar(jar, snapshots)
    else:
        _restore_requests_jar(jar, snapshots)


def _restore_wreq_jar(jar: Any, snapshots: Iterable[Mapping[str, Any]]) -> None:
    jar.clear()
    for snapshot in snapshots:
        same_site = snapshot.get("sameSite")
        cookie = WreqCookie(
            snapshot["name"],
            snapshot["value"],
            domain=snapshot.get("domain"),
            path=snapshot.get("path"),
            max_age=timedelta(seconds=snapshot["maxAge"]) if snapshot.get("maxAge") is not None else None,
            expires=datetime.fromisoformat(snapshot["expires"]) if snapshot.get("expires") is not None else None,
            http_only=snapshot.get("httpOnly"),
            secure=snapshot.get("secure"),
            same_site=(
                WreqSameSite.Lax
                if same_site == "lax"
                else WreqSameSite.Strict
                if same_site == "strict"
                else None
            ),
        )
        jar.add(cookie, _origin(snapshot["url"], cookie.path or "/"))


def _restore_requests_jar(jar: Any, snapshots: Iterable[Mapping[str, Any]]) -> None:
    jar.clear()
    for snapshot in snapshots:
        expires = None
        if snapshot.get("expires"):
            try:
                expires = int(datetime.fromisoformat(str(snapshot["expires"])).timestamp())
            except ValueError:
                expires = None
        domain = snapshot.get("domain")
        if not domain and snapshot.get("url"):
            domain = urlsplit(str(snapshot["url"])).hostname or ""
        jar.set(
            str(snapshot["name"]),
            str(snapshot["value"]),
            domain=domain or "",
            path=str(snapshot.get("path") or "/"),
            secure=bool(snapshot.get("secure", False)),
            expires=expires,
        )


@dataclass
class TikTokSessionState:
    """Persistent TikTok session holding rotated token, cookies, persona, and TicketGuard."""

    session_token: str = ""
    source_token_hash: str = ""
    persona: dict[str, Any] = field(default_factory=default_kr_persona)
    ticket_guard: TicketGuardState = field(default_factory=TicketGuardState)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    last_beat_at: str = ""
    last_updated_at: str = ""
    account_user_id: str = ""
    screen_name: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> TikTokSessionState:
        if not isinstance(data, Mapping):
            return cls()
        raw_persona = data.get("persona")
        persona = default_kr_persona()
        if isinstance(raw_persona, Mapping):
            persona.update(raw_persona)
        raw_cookies = data.get("cookies")
        cookies = [dict(c) for c in raw_cookies if isinstance(c, Mapping)] if isinstance(raw_cookies, list) else []
        tg = TicketGuardState.from_dict(data.get("ticketGuard") if isinstance(data.get("ticketGuard"), Mapping) else data)
        return cls(
            session_token=str(data.get("sessionToken") or ""),
            source_token_hash=str(data.get("sourceTokenHash") or ""),
            persona=persona,
            ticket_guard=tg,
            cookies=cookies,
            last_beat_at=str(data.get("lastBeatAt") or ""),
            last_updated_at=str(data.get("lastUpdatedAt") or ""),
            account_user_id=str(data.get("accountUserId") or ""),
            screen_name=str(data.get("screenName") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sourceTokenHash": self.source_token_hash,
            "sessionToken": self.session_token,
            "persona": dict(self.persona),
            "ticketGuard": self.ticket_guard.to_dict(),
            "cookies": list(self.cookies),
            "lastBeatAt": self.last_beat_at,
            "lastUpdatedAt": self.last_updated_at,
            "accountUserId": self.account_user_id,
            "screenName": self.screen_name,
        }

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        env_session_json: str = "",
        env_token: str = "",
        persona_overrides: Mapping[str, Any] | None = None,
    ) -> TikTokSessionState:
        """Load session state from env JSON or disk, reconciling env token rotation cleanly."""
        raw_data: dict[str, Any] | None = None
        if env_session_json.strip():
            try:
                parsed = json.loads(env_session_json)
                if isinstance(parsed, dict):
                    raw_data = parsed
            except json.JSONDecodeError:
                raw_data = None
        if raw_data is None and path is not None and path.is_file():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(parsed, dict):
                    raw_data = parsed
            except (OSError, json.JSONDecodeError):
                raw_data = None

        state = cls.from_dict(raw_data)
        if persona_overrides:
            state.persona.update(persona_overrides)

        cleaned_env_token = env_token.strip()
        if cleaned_env_token:
            env_hash = token_sha256(cleaned_env_token)
            if not state.session_token or (state.source_token_hash and state.source_token_hash != env_hash) or not state.source_token_hash:
                # A new TIKTOK_TT_TOKEN secret was provided: reset stale session token & cookies.
                if state.source_token_hash != env_hash:
                    state.cookies = []
                    state.ticket_guard.ticket = ""
                    state.ticket_guard.ts_sign = ""
                    state.last_beat_at = ""
                state.session_token = cleaned_env_token
                state.source_token_hash = env_hash
        state.ticket_guard.ensure_keypair()
        return state

    def save(self, path: Path) -> None:
        self.last_updated_at = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)

    def apply_response(self, headers: Mapping[str, str]) -> bool:
        """Capture rotated x-tt-token and tt-ticket-guard-server-data from a TikTok response."""
        changed = False
        for name, value in headers.items():
            if name.lower() == "x-tt-token" and str(value or "").strip():
                new_token = str(value).strip()
                if new_token != self.session_token:
                    self.session_token = new_token
                    changed = True
                break
        if self.ticket_guard.capture_server_data(headers):
            changed = True
        return changed
