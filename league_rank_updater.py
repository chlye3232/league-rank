#!/usr/bin/env python3
"""Manual TikTok league ranking extractor.

The input files in ``leaguerankid`` may contain either TikTok handles
(``unique_id``) or numeric TikTok user IDs. Handles are resolved from the
public profile page and cached locally. A numeric user ID is then used as the
``anchor_id`` for TikTok's signed webcast rank-list request.
"""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from signer import argus, gorgon, ladon


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IDS_DIR = SCRIPT_DIR / "leaguerankid"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_CACHE_PATH = SCRIPT_DIR / "id_cache.json"

HOST = "webcast22-normal-c-alisg.tiktokv.com"
RANK_TYPE = 16
FIXED_ROOM_ID = "7642978304493898516"

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

DEFAULT_PARAMS = {
    "anchor_id": "7378264028956705793",
    "room_id": FIXED_ROOM_ID,
    "rank_type": str(RANK_TYPE),
    "region_type": "1",
    "gap_interval": "0",
    "use_simple_user": "true",
    "league_phase": "0",
    "device_platform": "android",
    "os": "android",
    "ssmix": "a",
    "channel": "googleplay",
    "aid": "1233",
    "app_name": "musical_ly",
    "version_code": "450203",
    "version_name": "45.2.3",
    "manifest_version_code": "2024502030",
    "update_version_code": "2024502030",
    "ab_version": "45.2.3",
    "resolution": "900*1600",
    "dpi": "320",
    "device_type": "PHY110",
    "device_brand": "OPPO",
    "language": "ko",
    "os_api": "28",
    "os_version": "9",
    "ac": "wifi",
    "is_pad": "0",
    "app_type": "normal",
    "sys_region": "KR",
    "last_install_time": "1779433522",
    "mcc_mnc": "45002",
    "timezone_name": "Asia/Seoul",
    "carrier_region_v2": "450",
    "app_language": "ko",
    "carrier_region": "KR",
    "timezone_offset": "32400",
    "host_abi": "arm64-v8a",
    "locale": "ko-KR",
    "ac2": "wifi",
    "uoo": "1",
    "op_region": "KR",
    "build_number": "45.2.3",
    "region": "KR",
    "iid": "7642608726025438992",
    "device_id": "7642361429727249936",
    "webcast_sdk_version": "4430",
    "webcast_language": "ko-KR",
    "webcast_locale": "ko_KR",
    "effect_sdk_version": "21.4.0",
    "current_network_quality_info": "{}",
}

RANK_HEADERS = {
    "Accept-Encoding": "gzip",
    "Connection": "keep-alive",
    "Host": HOST,
    "User-Agent": (
        "com.zhiliaoapp.musically/2024502030 "
        "(Linux; U; Android 9; ko_KR; PHY110; Build/PQ3B.190801.03250903;tt-ok/3.12.13.21)"
    ),
    "oec-cs-sdk-version": "v10.02.06-ov-android_V31",
    "oec-cs-si-a": "2",
    "oec-vc-sdk-version": "3.2.2.i18n",
    "passport-sdk-version": "1",
    "rpc-persist-pns-region-1": "KR|1835841",
    "rpc-persist-pns-region-2": "KR|1835841",
    "rpc-persist-pns-region-3": "KR|1835841|1841610",
    "rpc-persist-pyxis-policy-v-tnc": "1",
    "sdk-version": "2",
    "x-bd-client-key": "#rp2Zt5ZoXdNmVkV/uwVFt6AtT1y4s7EC+G48E8bK4e47HTDIt9ny5IhwZd4+4UAZq08c8lWf6Jq44PaD",
    "x-bd-kmsv": "0",
    "x-tt-pba-encode": "4000",
    "x-tt-request-tag": "n=0;nr=0;bg=0",
    "x-tt-store-region": "kr",
    "x-tt-store-region-src": "uid",
    "x-vc-bdturing-sdk-version": "2.4.2.i18n",
}

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


def parse_proxy_urls(value: str) -> list[str]:
    """Parse comma/newline separated HTTP(S) proxy URLs."""
    return ordered_unique(re.split(r"[,\r\n]+", value))


class TikTokClient:
    def __init__(
        self,
        token: str,
        cache_path: Path,
        timeout: float = 20.0,
        retries: int = 2,
        proxy_urls: Iterable[str] = (),
    ):
        self.token = token.strip()
        self.cache_path = cache_path
        self.timeout = timeout
        self.retries = max(0, retries)
        self.proxy_urls = [url.strip() for url in proxy_urls if url.strip()]
        self.proxy_cursor = 0
        raw_cache = read_json(cache_path, {})
        self.id_cache: dict[str, str] = {
            str(key).casefold(): str(value)
            for key, value in raw_cache.items()
            if str(value).isdigit()
        } if isinstance(raw_cache, dict) else {}
        self.profile_session = requests.Session()
        self.profile_session.headers.update(PROFILE_HEADERS)
        self.rank_session = requests.Session()

    def _next_proxies(self) -> dict[str, str] | None:
        if not self.proxy_urls:
            return None
        proxy_url = self.proxy_urls[self.proxy_cursor % len(self.proxy_urls)]
        self.proxy_cursor += 1
        return {"http": proxy_url, "https": proxy_url}

    def save_cache(self) -> None:
        write_json_atomic(self.cache_path, dict(sorted(self.id_cache.items())))

    def _get_with_retries(
        self,
        session: requests.Session,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
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
                if retryable:
                    if attempt < self.retries:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                return response
            except requests.RequestException as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
        raise TikTokRequestError(f"TikTok 연결 실패: {last_error}")

    def resolve_user_id(self, handle_or_id: str) -> str:
        value = normalize_handle(handle_or_id)
        if value.isdigit():
            return value
        key = value.casefold()
        cached = self.id_cache.get(key)
        if cached:
            return cached

        encoded = urllib.parse.quote(value, safe="._-")
        response = self._get_with_retries(self.profile_session, f"https://www.tiktok.com/@{encoded}")
        if response.status_code != 200:
            raise TikTokRequestError(f"@{value} 프로필 HTTP {response.status_code}")
        user_id = parse_profile_html(response.text, value)
        if not user_id:
            raise TikTokRequestError(f"@{value}의 숫자 user_id를 찾지 못했습니다")
        self.id_cache[key] = user_id
        self.save_cache()
        return user_id

    def fetch_ranking(self, anchor_id: str) -> dict[str, Any]:
        if not self.token:
            raise ConfigurationError(".env의 TIKTOK_TT_TOKEN 값을 입력해야 합니다")

        now_seconds = time.time()
        unix_ts = int(now_seconds)
        rticket_ms = int(now_seconds * 1000)
        params = DEFAULT_PARAMS.copy()
        params.update(
            {
                "anchor_id": str(anchor_id),
                "room_id": FIXED_ROOM_ID,
                "rank_type": str(RANK_TYPE),
                "ts": str(unix_ts),
                "_rticket": str(rticket_ms),
            }
        )
        query_string = urllib.parse.urlencode(params)
        url = f"https://{HOST}/webcast/ranklist/list/v2/?{query_string}"
        aid = int(params["aid"])
        trace = f"{round(now_seconds * 1000) & 0xFFFFFFFF:x}10" + "".join(
            random.choice("0123456789abcdef") for _ in range(16)
        )

        headers = RANK_HEADERS.copy()
        headers.update(
            {
                "X-Argus": argus.Argus.get_sign(
                    queryhash=query_string,
                    data=None,
                    timestamp=unix_ts,
                    aid=aid,
                    license_id=1611921764,
                    platform=0,
                    sec_device_id="",
                    sdk_version="v04.04.05-ov-android",
                    sdk_version_int=134744640,
                ),
                "X-Ladon": ladon.Ladon.encrypt(unix_ts, 1611921764, aid),
                "X-Gorgon": gorgon.get_xgorgon(
                    params=query_string,
                    ticket=now_seconds,
                    data="",
                    cookie="",
                ),
                "X-Khronos": str(unix_ts),
                "X-SS-REQ-TICKET": str(rticket_ms),
                "x-tt-trace-id": f"00-{trace}-{trace[:16]}-01",
                "X-Tt-Token": self.token,
            }
        )

        response = self._get_with_retries(self.rank_session, url, headers=headers)
        if response.status_code in (401, 403):
            raise TikTokBlockedError(
                f"랭킹 API HTTP {response.status_code}: TIKTOK_TT_TOKEN 만료 또는 요청 차단 가능성"
            )
        if response.status_code != 200:
            raise TikTokRequestError(f"랭킹 API HTTP {response.status_code}")
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise TikTokRequestError("랭킹 API가 JSON이 아닌 응답을 반환했습니다") from error
        if not isinstance(payload, dict):
            raise TikTokRequestError("랭킹 API 응답 형식이 올바르지 않습니다")
        return payload


def parse_board(payload: dict[str, Any], anchor: str, user_id: str) -> Board:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TikTokRequestError("랭킹 응답에 data가 없습니다")
    if data.get("prompts"):
        raise TikTokRequestError(f"TikTok 안내 응답: {data.get('prompts')}")
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
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


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
    ):
        self.client = client
        self.candidates = candidates
        self.output_dir = output_dir
        self.max_tries = max(1, max_tries)
        self.request_delay = max(0.0, request_delay)
        self.keep_stale = keep_stale
        self.export_details = export_details

    def run(self, selected: list[str]) -> tuple[dict[str, Any], bool]:
        league_date = kst_today()
        created_at = datetime.now(timezone.utc).isoformat()
        latest_path = self.output_dir / "league-anchors.json"
        previous = read_json(latest_path, {})
        previous_rows = previous.get("rows", []) if isinstance(previous, dict) else []

        direct: dict[int, Board] = {}
        harvested: dict[int, Board] = {}
        probe_notes: dict[str, list[str]] = {league: [] for league in selected}

        for league in selected:
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
                except TikTokBlockedError:
                    raise
                except LeagueUpdaterError as error:
                    message = f"@{candidate}: {error}"
                    probe_notes[league].append(message)
                    print(f"      건너뜀 - {error}")
                finally:
                    if self.request_delay:
                        time.sleep(self.request_delay)

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
    parser.add_argument(
        "--league",
        action="append",
        choices=LEAGUE_CODES,
        help="특정 리그만 검사합니다. 여러 번 지정할 수 있습니다",
    )
    parser.add_argument("--max-tries", type=int, default=20, help="리그별 순서대로 검사할 최대 ID 수 (기본 20)")
    parser.add_argument("--delay", type=float, default=0.35, help="랭킹 요청 사이 대기 초 (기본 0.35)")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP 제한 시간 초 (기본 20)")
    parser.add_argument("--retries", type=int, default=2, help="일시적 오류 재시도 횟수 (기본 2)")
    parser.add_argument("--no-stale", action="store_true", help="실패한 리그의 이전 결과를 승계하지 않습니다")
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

    try:
        candidates = load_candidate_files(args.ids_dir)
        selected = list(dict.fromkeys(args.league or LEAGUE_CODES))
        if args.dry_run:
            print("ID 파일 검사 완료")
            for league in selected:
                print(f"  {league}: {len(candidates[league])}개")
            return 0

        token = os.environ.get("TIKTOK_TT_TOKEN", "")
        proxy_urls = parse_proxy_urls(os.environ.get("TIKTOK_PROXY_URLS", ""))
        client = TikTokClient(
            token,
            args.cache,
            timeout=args.timeout,
            retries=args.retries,
            proxy_urls=proxy_urls,
        )
        if args.resolve_only:
            value = normalize_handle(args.resolve_only)
            print(f"@{value} -> {client.resolve_user_id(value)}")
            return 0

        if not token:
            raise ConfigurationError(
                f"{SCRIPT_DIR / '.env'} 파일을 만들고 TIKTOK_TT_TOKEN을 입력하세요"
            )

        print(f"대상: {', '.join(selected)}")
        print(f"결과 폴더: {args.output_dir}")
        print(f"프록시: {len(proxy_urls)}개" if proxy_urls else "프록시: 사용 안 함")
        updater = LeagueUpdater(
            client=client,
            candidates=candidates,
            output_dir=args.output_dir,
            max_tries=args.max_tries,
            request_delay=args.delay,
            keep_stale=not args.no_stale,
            export_details=args.export_details,
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
        return 0 if all_fresh else 2
    except KeyboardInterrupt:
        print("\n사용자가 작업을 중단했습니다", file=sys.stderr)
        return 130
    except LeagueUpdaterError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
