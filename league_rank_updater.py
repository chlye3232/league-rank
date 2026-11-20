#!/usr/bin/env python3
"""Web collector variant with proxy handoff and auth-expiry short-circuiting."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from signer import (
    SignedSession,
    SignupClient,
    SignupError,
    TikTokSessionState,
    TransportError,
    build_common_params,
    build_user_agent,
    default_kr_env_overrides,
    extract_persona_from_argus,
)

_REQUEST_ERRORS: tuple[type[Exception], ...] = (
    (TransportError, requests.RequestException) if requests is not None else (TransportError,)
)
_JSON_ERRORS: tuple[type[Exception], ...] = (
    (ValueError, requests.JSONDecodeError) if requests is not None else (ValueError,)
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IDS_DIR = SCRIPT_DIR / "leaguerankid"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_CACHE_PATH = SCRIPT_DIR / "id_cache.json"

HOST = "webcast22-normal-c-alisg.tiktokv.com"
PASSPORT_BASE_URL = "https://api16-normal-c-alisg.tiktokv.com"
RANK_TYPE = 16
FIXED_ROOM_ID = "7642978304493898516"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_AUTH = 3

LEAGUE_CLASS_TYPES: dict[str, int] = {
    "A1": 2000,
    "A2": 1900,
    "A3": 1800,
    "B1": 1500,
    "B2": 1400,
    "B3": 1300,
    "B4": 1200,
    "B5": 1100,
    "C1": 1000,
    "C2": 900,
    "C3": 800,
    "C4": 700,
    "C5": 600,
    "D1": 500,
    "D2": 400,
    "D3": 300,
    "D4": 200,
    "D5": 100,
}
CLASS_TYPE_LEAGUES = {value: key for key, value in LEAGUE_CLASS_TYPES.items()}
LEAGUE_CODES = list(LEAGUE_CLASS_TYPES)

PROFILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

EMBEDDED_PROFILE_RE = re.compile(
    r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"\s+type="application/json">(.*?)</script>',
    re.DOTALL,
)


class LeagueUpdaterError(RuntimeError):
    """Base exception with a user-friendly message."""


class ConfigurationError(LeagueUpdaterError):
    pass


class TikTokRequestError(LeagueUpdaterError):
    pass


class TikTokBlockedError(TikTokRequestError):
    pass


class TikTokAuthError(TikTokRequestError):
    """Raised when TikTok rejects the login session (e.g. status_code 20003 / HTTP 401)."""


@dataclass
class Board:
    class_type: int
    ranks: list[dict[str, Any]]
    anchor: str
    user_id: str


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def load_dotenv(path: Path) -> None:
    """Load a small .env file without adding another dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def normalize_handle(value: str) -> str:
    value = value.strip()
    if value.startswith("https://") or value.startswith("http://"):
        match = re.search(r"tiktok\.com/@([^/?#]+)", value, re.IGNORECASE)
        if match:
            value = urllib.parse.unquote(match.group(1))
    return value.lstrip("@").strip()


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_handle(value)
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def load_candidate_files(ids_dir: Path) -> dict[str, list[str]]:
    if not ids_dir.is_dir():
        raise ConfigurationError(f"리그 ID 폴더가 없습니다: {ids_dir}")

    result: dict[str, list[str]] = {}
    missing: list[str] = []
    for league in LEAGUE_CODES:
        path = ids_dir / f"{league.lower()}.txt"
        if not path.is_file():
            path = ids_dir / f"{league}.txt"
        if not path.is_file():
            missing.append(path.name)
            continue
        lines = []
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
        result[league] = ordered_unique(lines)

    if missing:
        raise ConfigurationError("누락된 ID 파일: " + ", ".join(missing))
    return result


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def find_profile_user_id(payload: Any, target_handle: str) -> str | None:
    """Find the numeric user ID in TikTok's embedded profile JSON."""
    target = target_handle.casefold()
    fallback: list[str] = []
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            possible = [current]
            for key in ("user", "userInfo"):
                value = current.get(key)
                if isinstance(value, dict):
                    possible.append(value.get("user", value) if isinstance(value.get("user", value), dict) else value)
            for candidate in possible:
                unique_id = candidate.get("uniqueId") or candidate.get("unique_id")
                user_id = candidate.get("id") or candidate.get("id_str") or candidate.get("userId")
                if isinstance(user_id, (int, str)) and str(user_id).isdigit():
                    user_id = str(user_id)
                    if isinstance(unique_id, str) and unique_id.casefold() == target:
                        return user_id
                    if isinstance(unique_id, str):
                        fallback.append(user_id)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    unique_fallback = list(dict.fromkeys(fallback))
    return unique_fallback[0] if len(unique_fallback) == 1 else None


def parse_profile_html(html: str, target_handle: str) -> str | None:
    match = EMBEDDED_PROFILE_RE.search(html)
    if match:
        try:
            payload = json.loads(match.group(1))
            found = find_profile_user_id(payload, target_handle)
            if found:
                return found
        except json.JSONDecodeError:
            pass

    escaped_handle = re.escape(target_handle)
    patterns = (
        rf'"id":"(\d{{10,}})".{{0,1200}}?"uniqueId":"{escaped_handle}"',
        rf'"uniqueId":"{escaped_handle}".{{0,1200}}?"id":"(\d{{10,}})"',
    )
    for pattern in patterns:
        fallback = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if fallback:
            return fallback.group(1)
    return None


def parse_live_room_user(payload: Any, target_handle: str) -> tuple[str | None, str | None]:
    """Extract numeric user ID and room ID from TikTok's api-live/user/room JSON."""
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None
    user = data.get("user")
    if not isinstance(user, dict):
        return None, None
    unique_id = user.get("uniqueId") or user.get("unique_id")
    if isinstance(unique_id, str) and unique_id and unique_id.casefold() != target_handle.casefold():
        return None, None
    user_id = user.get("id") or user.get("id_str")
    if not str(user_id or "").isdigit():
        return None, None
    room_id = user.get("roomId") or user.get("room_id")
    resolved_room = str(room_id) if str(room_id or "").isdigit() and str(room_id) != "0" else None
    return str(user_id), resolved_room


def parse_proxy_urls(value: str) -> list[str]:
    """Parse comma/newline separated HTTP(S) proxy URLs."""
    return ordered_unique(re.split(r"[,\r\n]+", value))


def load_persona_overrides_from_env() -> dict[str, Any]:
    """Build optional persona overrides from env vars (JSON, captured X-Argus, device/install IDs)."""
    overrides: dict[str, Any] = {}
    captured_argus = os.environ.get("TIKTOK_CAPTURED_ARGUS", "").strip()
    if captured_argus:
        captured_url = os.environ.get("TIKTOK_CAPTURED_URL", "").strip() or None
        try:
            overrides.update(extract_persona_from_argus(captured_argus, captured_url))
        except Exception as error:
            raise ConfigurationError(f"TIKTOK_CAPTURED_ARGUS 파싱 실패: {error}") from error

    raw_persona = os.environ.get("TIKTOK_PERSONA_JSON", "").strip()
    if raw_persona:
        try:
            parsed = json.loads(raw_persona)
        except json.JSONDecodeError as error:
            raise ConfigurationError("TIKTOK_PERSONA_JSON 값이 올바른 JSON이 아닙니다") from error
        if not isinstance(parsed, dict):
            raise ConfigurationError("TIKTOK_PERSONA_JSON 값은 JSON 객체여야 합니다")
        overrides.update(parsed)

    device_id = os.environ.get("TIKTOK_DEVICE_ID", "").strip()
    install_id = os.environ.get("TIKTOK_INSTALL_ID", "").strip()
    if device_id:
        overrides["deviceId"] = device_id
    if install_id:
        overrides["installId"] = install_id
    return overrides


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


class TikTokClient:
    def __init__(
        self,
        token: str,
        cache_path: Path,
        timeout: float = 20.0,
        retries: int = 2,
        proxy_urls: Iterable[str] = (),
        *,
        session_path: Path | None = None,
        session_state: TikTokSessionState | None = None,
        persona_overrides: Mapping[str, Any] | None = None,
        beat_interval_minutes: float = 60.0,
        enable_token_beat: bool = True,
        force_requests: bool = False,
    ):
        self.cache_path = cache_path
        self.timeout = timeout
        self.retries = max(0, retries)
        self.proxy_urls = [url.strip() for url in proxy_urls if url.strip()]
        self.proxy_cursor = 0
        self.session_path = session_path
        self.beat_interval_minutes = beat_interval_minutes
        self.enable_token_beat = enable_token_beat
        self._beat_checked = False

        if session_state is not None:
            self.session_state = session_state
            if token.strip() and not self.session_state.session_token:
                self.session_state.session_token = token.strip()
        else:
            self.session_state = TikTokSessionState.load(
                session_path,
                env_session_json=os.environ.get("TIKTOK_SESSION_JSON", ""),
                env_token=token,
                persona_overrides=persona_overrides,
            )

        self.token = self.session_state.session_token
        raw_cache = read_json(cache_path, {})
        self.id_cache: dict[str, str] = {
            str(key).casefold(): str(value)
            for key, value in raw_cache.items()
            if str(value).isdigit()
        } if isinstance(raw_cache, dict) else {}
        self.room_cache: dict[str, str] = {}

        self.signed_session = SignedSession(
            self.session_state.persona,
            timeout=timeout,
            force_requests=force_requests,
        )
        if self.session_state.cookies:
            try:
                self.signed_session.restore_cookies(self.session_state.cookies)
            except Exception:
                pass
        self.profile_session = self.signed_session
        self.rank_session = self.signed_session

    def close(self) -> None:
        self.signed_session.close()

    def __enter__(self) -> TikTokClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @property
    def active_token(self) -> str:
        return self.session_state.session_token or self.token

    def _next_proxy_url(self) -> str | None:
        if not self.proxy_urls:
            return None
        proxy_url = self.proxy_urls[self.proxy_cursor % len(self.proxy_urls)]
        self.proxy_cursor += 1
        return proxy_url

    def _next_proxies(self) -> dict[str, str] | None:
        proxy_url = self._next_proxy_url()
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def save_cache(self) -> None:
        write_json_atomic(self.cache_path, dict(sorted(self.id_cache.items())))

    def persist_session(self, *, force: bool = False) -> None:
        if self.session_path is None:
            return
        try:
            self.session_state.cookies = self.signed_session.dump_cookies(strict=False)
        except Exception:
            pass
        if force or self.session_state.session_token or self.session_state.cookies:
            self.session_state.save(self.session_path)

    def _get_with_retries(
        self,
        session: Any,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        sign: bool = False,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                if isinstance(session, SignedSession):
                    response = session.get(
                        url,
                        headers=headers,
                        proxy_url=self._next_proxy_url(),
                        sign=sign,
                    )
                else:
                    response = session.get(
                        url,
                        headers=headers,
                        timeout=self.timeout,
                        proxies=self._next_proxies(),
                    )
                retryable = (
                    response.status_code == 429
                    or response.status_code >= 500
                    or (response.status_code == 403 and bool(self.proxy_urls))
                )
                if retryable and attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return response
            except _REQUEST_ERRORS as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
        raise TikTokRequestError(f"TikTok 연결 실패: {last_error}")

    def _should_beat(self, *, force: bool = False) -> bool:
        if force:
            return True
        if not self.enable_token_beat or self._beat_checked or not self.active_token:
            return False
        if not self.session_state.last_beat_at:
            return True
        try:
            last = datetime.fromisoformat(self.session_state.last_beat_at)
            age_minutes = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            return age_minutes >= max(0.0, self.beat_interval_minutes)
        except ValueError:
            return True

    def ensure_session_alive(self, *, force_beat: bool = False) -> None:
        if not self._should_beat(force=force_beat):
            return
        self._beat_checked = True
        env_overrides = default_kr_env_overrides()
        persona = self.session_state.persona
        common_params = build_common_params(persona, env_overrides)
        common_headers = {"user-agent": build_user_agent(persona, env_overrides)}
        signup_state = {
            "status": "completed",
            "nextAction": "completed",
            "sessionToken": self.active_token,
            **self.session_state.ticket_guard.to_dict(),
        }
        signup = SignupClient(
            self.signed_session,
            base_url=PASSPORT_BASE_URL,
            state=signup_state,
            common_headers=common_headers,
            query_params=common_params,
            proxy_url=self._next_proxy_url(),
        )
        try:
            signup.token_beat(scene="boot", first_beat=not bool(self.session_state.last_beat_at))
            self.session_state.session_token = str(signup.state.get("sessionToken") or self.active_token)
            self.session_state.ticket_guard.ticket = str(signup.state.get("ticketGuardTicket") or self.session_state.ticket_guard.ticket)
            self.session_state.ticket_guard.ts_sign = str(signup.state.get("ticketGuardTsSign") or self.session_state.ticket_guard.ts_sign)
            self.session_state.last_beat_at = datetime.now(timezone.utc).isoformat()
            self.token = self.session_state.session_token
            self.persist_session(force=True)
            print("세션 유지(token_beat) 완료", flush=True)
        except (SignupError, TransportError) as error:
            print(f"세션 유지(token_beat) 건너뜀: {error}", flush=True)

    def resolve_user_id(self, handle_or_id: str) -> str:
        value = normalize_handle(handle_or_id)
        if value.isdigit():
            return value
        key = value.casefold()
        cached = self.id_cache.get(key)
        if cached:
            return cached

        encoded = urllib.parse.quote(value, safe="._-")
        live_url = f"https://www.tiktok.com/api-live/user/room/?aid=1988&uniqueId={encoded}&sourceType=54"
        live_response = self._get_with_retries(
            self.profile_session,
            live_url,
            headers=PROFILE_HEADERS,
            sign=False,
        )
        if live_response.status_code == 200:
            try:
                user_id, room_id = parse_live_room_user(live_response.json(), value)
                if user_id:
                    if room_id:
                        self.room_cache[user_id] = room_id
                    self.id_cache[key] = user_id
                    self.save_cache()
                    return user_id
            except Exception:
                pass

        response = self._get_with_retries(
            self.profile_session,
            f"https://www.tiktok.com/@{encoded}",
            headers=PROFILE_HEADERS,
            sign=False,
        )
        if response.status_code != 200:
            raise TikTokRequestError(f"@{value} 프로필 HTTP {response.status_code}")
        user_id = parse_profile_html(response.text, value)
        if not user_id:
            raise TikTokRequestError(f"@{value}의 숫자 user_id를 찾지 못했습니다")
        self.id_cache[key] = user_id
        self.save_cache()
        return user_id

    def fetch_ranking(self, anchor_id: str, room_id: str | None = None) -> dict[str, Any]:
        token = self.active_token
        if not token:
            raise ConfigurationError(".env의 TIKTOK_TT_TOKEN 또는 세션 파일을 설정해야 합니다")

        now_ns = time.time_ns()
        unix_ts = now_ns // 1_000_000_000
        rticket_ms = now_ns // 1_000_000
        persona = self.session_state.persona
        env_overrides = default_kr_env_overrides()
        common_params = build_common_params(persona, env_overrides, now_ms=rticket_ms)
        resolved_room = room_id or self.room_cache.get(str(anchor_id)) or FIXED_ROOM_ID

        endpoint_params = {
            "anchor_id": str(anchor_id),
            "room_id": str(resolved_room),
            "rank_type": str(RANK_TYPE),
            "region_type": "1",
            "gap_interval": "0",
            "use_simple_user": "true",
            "league_phase": "0",
            "webcast_sdk_version": "4430",
            "webcast_language": "ko-KR",
            "webcast_locale": "ko_KR",
            "effect_sdk_version": "21.4.0",
            "current_network_quality_info": "{}",
            **common_params,
            "ac": "wifi",
            "ac2": "wifi",
            "uoo": "1",
        }
        query_string = urllib.parse.urlencode(endpoint_params)
        url = f"https://{HOST}/webcast/ranklist/list/v2/?{query_string}"
        trace = f"{rticket_ms & 0xFFFFFFFF:x}10" + "".join(
            random.choice("0123456789abcdef") for _ in range(16)
        )

        headers = {
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
            "Host": HOST,
            "User-Agent": build_user_agent(persona, env_overrides),
            "passport-sdk-version": "1",
            "sdk-version": "2",
            "x-tt-bypass-dp": "1",
            "x-tt-request-tag": "n=0;nr=0;bg=0",
            "x-tt-store-region": "kr",
            "x-tt-store-region-src": "uid",
            "x-tt-trace-id": f"00-{trace}-{trace[:16]}-01",
            "x-tt-token": token,
            **self.session_state.ticket_guard.build_headers(url, has_token=True, timestamp=unix_ts),
        }

        response = self._get_with_retries(self.rank_session, url, headers=headers, sign=True)
        if self.session_state.apply_response(response.headers):
            self.token = self.session_state.session_token
            self.persist_session(force=True)
        else:
            self.persist_session(force=False)

        if response.status_code == 401:
            raise TikTokAuthError("랭킹 API HTTP 401: TikTok 로그인 세션 만료 또는 미인증")
        if response.status_code == 403:
            raise TikTokBlockedError(
                "랭킹 API HTTP 403: 요청 차단 또는 TIKTOK_TT_TOKEN/프록시 확인 필요"
            )
        if response.status_code != 200:
            raise TikTokRequestError(f"랭킹 API HTTP {response.status_code}")
        try:
            payload = response.json()
        except _JSON_ERRORS as error:
            raise TikTokRequestError("랭킹 API가 JSON이 아닌 응답을 반환했습니다") from error
        if not isinstance(payload, dict):
            raise TikTokRequestError("랭킹 API 응답 형식이 올바르지 않습니다")
        return payload


def parse_board(payload: dict[str, Any], anchor: str, user_id: str) -> Board:
    status_code = payload.get("status_code")
    data = payload.get("data")
    if not isinstance(data, dict):
        if status_code == 20003:
            raise TikTokAuthError("TikTok 로그인 세션 만료/미인증 (status_code=20003)")
        raise TikTokRequestError("랭킹 응답에 data가 없습니다")

    message = str(data.get("message") or "")
    prompts = data.get("prompts")
    if status_code == 20003 or "doesn't login" in message.lower() or "not login" in message.lower():
        detail = prompts or message or "먼저 로그인하세요"
        raise TikTokAuthError(
            f"TikTok 로그인 세션 만료/미인증 (status_code={status_code}): {detail}"
        )
    if prompts:
        raise TikTokRequestError(f"TikTok 안내 응답: {prompts}")

    rank_view = data.get("rank_view")
    if not isinstance(rank_view, dict):
        raise TikTokRequestError("랭킹 응답에 rank_view가 없습니다")
    extra = rank_view.get("rank_extra_info") or {}
    class_extra = extra.get("class_extra") if isinstance(extra, dict) else {}
    class_type = class_extra.get("class_type") if isinstance(class_extra, dict) else None
    ranks = rank_view.get("ranks")
    if not isinstance(class_type, int) or not isinstance(ranks, list) or not ranks:
        raise TikTokRequestError("class_type 또는 순위 목록이 비어 있습니다")
    return Board(class_type=class_type, ranks=ranks, anchor=anchor, user_id=user_id)


def make_row_id(date: str, class_type: int, rank: int) -> int:
    return int(f"{date.replace('-', '')}{class_type:04d}{rank:03d}")


def map_board_rows(board: Board, league_date: str, created_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(board.ranks, 1):
        if not isinstance(item, dict):
            continue
        rank_user = item.get("rank_user")
        if not isinstance(rank_user, dict):
            continue
        user_id = rank_user.get("id_str") or rank_user.get("id")
        rank = item.get("rank", index)
        if not str(user_id or "").isdigit():
            continue
        try:
            rank_number = int(rank)
        except (TypeError, ValueError):
            continue
        avatar = rank_user.get("avatar_thumb")
        avatar_urls = avatar.get("url_list") if isinstance(avatar, dict) else None
        profile_image_url = avatar_urls[0] if isinstance(avatar_urls, list) and avatar_urls else None
        room_id = item.get("room_id_str") or item.get("room_id") or "0"
        rows.append(
            {
                "id": make_row_id(league_date, board.class_type, rank_number),
                "league_date": league_date,
                "class_type": board.class_type,
                "rank": rank_number,
                "user_id": str(user_id),
                "score": int(item.get("score") or 0),
                "created_at": created_at,
                "nickname": rank_user.get("nickname"),
                "unique_id": rank_user.get("display_id") or rank_user.get("unique_id"),
                "profile_image_url": profile_image_url,
                "room_id_str": str(room_id),
            }
        )
    return sorted(rows, key=lambda row: row["rank"])


def kst_today() -> str:
    try:
        tz: Any = ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        tz = timezone(timedelta(hours=9))
    return datetime.now(tz).date().isoformat()


class LeagueUpdater:
    def __init__(
        self,
        client: TikTokClient,
        candidates: dict[str, list[str]],
        output_dir: Path,
        max_tries: int,
        request_delay: float,
        keep_stale: bool,
        export_details: bool,
        *,
        force_beat: bool = False,
    ):
        self.client = client
        self.candidates = candidates
        self.output_dir = output_dir
        self.max_tries = max(1, max_tries)
        self.request_delay = max(0.0, request_delay)
        self.keep_stale = keep_stale
        self.export_details = export_details
        self.force_beat = force_beat
        self.last_auth_failed = False

    def run(self, selected: list[str]) -> tuple[dict[str, Any], bool]:
        self.client.ensure_session_alive(force_beat=self.force_beat)

        league_date = kst_today()
        created_at = datetime.now(timezone.utc).isoformat()
        latest_path = self.output_dir / "league-anchors.json"
        previous = read_json(latest_path, {})
        previous_rows = previous.get("rows", []) if isinstance(previous, dict) else []

        direct: dict[int, Board] = {}
        harvested: dict[int, Board] = {}
        probe_notes: dict[str, list[str]] = {league: [] for league in selected}
        direct_blocked = False
        auth_failed = False

        for league in selected:
            if auth_failed:
                probe_notes[league].append("TikTok 로그인 세션 만료로 중단됨")
                continue
            if direct_blocked:
                probe_notes[league].append("직접 접속 차단으로 프록시 단계에 넘김")
                continue

            expected = LEAGUE_CLASS_TYPES[league]
            if expected in harvested:
                print(f"[{league}] 앞선 조회 결과를 재사용합니다")
                continue

            values = self.candidates[league][: self.max_tries]
            print(f"\n[{league}] class_type={expected}, 후보 {len(values)}명 검사")
            for index, candidate in enumerate(values, 1):
                try:
                    user_id = self.client.resolve_user_id(candidate)
                    print(f"  {index:02d}. @{candidate} -> {user_id} 조회", flush=True)
                    payload = self.client.fetch_ranking(user_id)
                    board = parse_board(payload, candidate, user_id)
                    actual_league = CLASS_TYPE_LEAGUES.get(board.class_type, f"class-{board.class_type}")
                    print(f"      실제 {actual_league}, {len(board.ranks)}개 순위")
                    harvested.setdefault(board.class_type, board)
                    probe_notes[league].append(f"@{candidate}: {actual_league}")
                    if board.class_type == expected:
                        direct[expected] = board
                        break
                except TikTokAuthError as error:
                    message = f"@{candidate}: {error}"
                    probe_notes[league].append(message)
                    print(f"      인증 만료 오류 - 나머지 검사를 중단합니다: {error}")
                    auth_failed = True
                    break
                except TikTokBlockedError as error:
                    message = f"@{candidate}: {error}"
                    probe_notes[league].append(message)
                    print(f"      직접 접속 차단 - 이 리그부터 프록시 단계에 넘깁니다: {error}")
                    direct_blocked = True
                    break
                except LeagueUpdaterError as error:
                    message = f"@{candidate}: {error}"
                    probe_notes[league].append(message)
                    print(f"      건너뜀 - {error}")
                finally:
                    if self.request_delay:
                        time.sleep(self.request_delay)

        self.last_auth_failed = auth_failed
        rows: list[dict[str, Any]] = []
        leagues: dict[str, Any] = {}
        summary: list[dict[str, Any]] = []
        all_fresh = True

        for league in selected:
            class_type = LEAGUE_CLASS_TYPES[league]
            board = direct.get(class_type) or harvested.get(class_type)
            if board:
                mapped = map_board_rows(board, league_date, created_at)
                source = "match" if class_type in direct else "harvest"
                rows.extend(mapped)
                leagues[league] = {
                    "classType": class_type,
                    "anchors": [
                        {"userId": row["user_id"], "nickname": row.get("nickname") or ""}
                        for row in mapped[:40]
                    ],
                    "ok": True,
                }
                summary.append(
                    {
                        "league": league,
                        "classType": class_type,
                        "count": len(mapped),
                        "ok": True,
                        "source": source,
                        "anchor": board.anchor,
                        "anchorUserId": board.user_id,
                        "probes": probe_notes[league],
                    }
                )
                continue

            carried = [row for row in previous_rows if row.get("class_type") == class_type] if self.keep_stale else []
            rows.extend(carried)
            previous_league = previous.get("leagues", {}).get(league, {}) if isinstance(previous, dict) else {}
            leagues[league] = {
                "classType": class_type,
                "anchors": previous_league.get("anchors", []),
                "ok": False,
            }
            source = "stale" if carried else "none"
            summary.append(
                {
                    "league": league,
                    "classType": class_type,
                    "count": len(carried),
                    "ok": False,
                    "source": source,
                    "anchor": None,
                    "anchorUserId": None,
                    "probes": probe_notes[league],
                }
            )
            all_fresh = False

        rows.sort(key=lambda row: (-int(row.get("class_type", 0)), int(row.get("rank", 999999))))
        snapshot: dict[str, Any] = {
            "leagueDate": league_date,
            "resolvedAt": created_at,
            "leagues": leagues,
            "rows": rows,
            "summary": summary,
        }
        if isinstance(previous, dict) and previous.get("rows"):
            snapshot["previousLeagueDate"] = previous.get("leagueDate")
            snapshot["previousResolvedAt"] = previous.get("resolvedAt")

        if selected == LEAGUE_CODES:
            write_json_atomic(latest_path, snapshot)
        else:
            partial_name = "partial-" + "-".join(selected).lower() + ".json"
            write_json_atomic(self.output_dir / partial_name, snapshot)

        if self.export_details:
            run_dir = self.output_dir / league_date
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(run_dir / f"league_rankings_{league_date}.json", snapshot)
            write_json_atomic(run_dir / "summary.json", summary)

            for item in summary:
                class_type = item["classType"]
                league_rows = [row for row in rows if row.get("class_type") == class_type]
                write_json_atomic(run_dir / f"league_rankings_{league_date}_{class_type}.json", league_rows)

            csv_path = run_dir / f"league_rankings_{league_date}.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
                fields = [
                    "league",
                    "class_type",
                    "rank",
                    "user_id",
                    "score",
                    "nickname",
                    "unique_id",
                    "profile_image_url",
                    "room_id_str",
                    "created_at",
                ]
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            "league": CLASS_TYPE_LEAGUES.get(row.get("class_type"), ""),
                            **{field: row.get(field) for field in fields if field != "league"},
                        }
                    )

        return snapshot, all_fresh


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TikTok A1~D5 리그 순위를 수동으로 추출합니다")
    parser.add_argument("--ids-dir", type=Path, default=DEFAULT_IDS_DIR, help="a1.txt~d5.txt 폴더")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="결과 저장 폴더")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH, help="unique_id -> user_id 캐시")
    parser.add_argument("--session-file", type=Path, default=None, help="TikTok 세션/쿠키 상태 파일")
    parser.add_argument(
        "--league",
        action="append",
        choices=LEAGUE_CODES,
        help="특정 리그만 검사합니다. 여러 번 지정할 수 있습니다",
    )
    parser.add_argument("--max-tries", type=int, default=50, help="리그별 순서대로 검사할 최대 ID 수 (기본 50)")
    parser.add_argument("--delay", type=float, default=0.35, help="랭킹 요청 사이 대기 초 (기본 0.35)")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP 제한 시간 초 (기본 20)")
    parser.add_argument("--retries", type=int, default=2, help="일시적 오류 재시도 횟수 (기본 2)")
    parser.add_argument("--no-stale", action="store_true", help="실패한 리그의 이전 결과를 승계하지 않습니다")
    parser.add_argument("--beat", action="store_true", help="실행 시작 시 token_beat를 강제로 실행합니다")
    parser.add_argument("--no-beat", action="store_true", help="실행 시작 시 token_beat를 생략합니다")
    parser.add_argument(
        "--export-details",
        action="store_true",
        help="날짜별 JSON, 리그별 JSON, CSV도 추가로 내보냅니다",
    )
    parser.add_argument("--dry-run", action="store_true", help="파일만 검사하고 TikTok에는 접속하지 않습니다")
    parser.add_argument("--resolve-only", metavar="HANDLE", help="@아이디를 숫자 user_id로만 변환합니다")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console()
    load_dotenv(SCRIPT_DIR / ".env")
    args = parse_args(argv)

    client: TikTokClient | None = None
    try:
        candidates = load_candidate_files(args.ids_dir)
        selected = list(dict.fromkeys(args.league or LEAGUE_CODES))
        if args.dry_run:
            print("ID 파일 검사 완료")
            for league in selected:
                print(f"  {league}: {len(candidates[league])}개")
            return EXIT_OK

        token = os.environ.get("TIKTOK_TT_TOKEN", "")
        proxy_urls = parse_proxy_urls(os.environ.get("TIKTOK_PROXY_URLS", ""))
        session_path = args.session_file or Path(
            os.environ.get("TIKTOK_SESSION_FILE", "").strip() or str(args.output_dir / "tiktok-session.json")
        )
        persona_overrides = load_persona_overrides_from_env()
        enable_beat = False if args.no_beat else env_bool("TIKTOK_TOKEN_BEAT", True)
        beat_interval = env_float("TIKTOK_BEAT_INTERVAL_MINUTES", 60.0)

        client = TikTokClient(
            token,
            args.cache,
            timeout=args.timeout,
            retries=args.retries,
            proxy_urls=proxy_urls,
            session_path=session_path,
            persona_overrides=persona_overrides,
            beat_interval_minutes=beat_interval,
            enable_token_beat=enable_beat,
        )
        if args.resolve_only:
            value = normalize_handle(args.resolve_only)
            print(f"@{value} -> {client.resolve_user_id(value)}")
            return EXIT_OK

        if not client.active_token:
            raise ConfigurationError(
                f"{SCRIPT_DIR / '.env'}에 TIKTOK_TT_TOKEN을 입력하거나 tiktok_auth.py로 세션을 발급하세요"
            )

        print(f"대상: {', '.join(selected)}")
        print(f"결과 폴더: {args.output_dir}")
        print(f"전송 계층: {client.signed_session.backend_name} (47.7.2 + TicketGuard)")
        print(f"프록시: {len(proxy_urls)}개" if proxy_urls else "프록시: 사용 안 함")
        updater = LeagueUpdater(
            client=client,
            candidates=candidates,
            output_dir=args.output_dir,
            max_tries=args.max_tries,
            request_delay=args.delay,
            keep_stale=not args.no_stale,
            export_details=args.export_details,
            force_beat=args.beat,
        )
        snapshot, all_fresh = updater.run(selected)
        print("\n" + "=" * 68)
        for item in snapshot["summary"]:
            state = "성공" if item["ok"] else "실패"
            print(
                f"{item['league']:>2}  {state:<2}  {item['count']:>4}명  "
                f"source={item['source']} anchor={item.get('anchor') or '-'}"
            )
        saved_path = (
            args.output_dir / "league-anchors.json"
            if selected == LEAGUE_CODES
            else args.output_dir / ("partial-" + "-".join(selected).lower() + ".json")
        )
        print(f"저장 완료: {saved_path}")
        if updater.last_auth_failed:
            print(
                "인증 만료 오류: TIKTOK_TT_TOKEN 또는 세션을 새로 발급해 교체해야 합니다.",
                file=sys.stderr,
            )
            return EXIT_AUTH
        return EXIT_OK if all_fresh else EXIT_PARTIAL
    except KeyboardInterrupt:
        print("\n사용자가 작업을 중단했습니다", file=sys.stderr)
        return 130
    except TikTokAuthError as error:
        print(f"인증 만료 오류: {error}", file=sys.stderr)
        return EXIT_AUTH
    except LeagueUpdaterError as error:
        print(f"오류: {error}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
