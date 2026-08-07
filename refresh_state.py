#!/usr/bin/env python3
"""Keep private refresh history and merge one-time league retries."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
LATEST_SNAPSHOT = OUTPUT_DIR / "league-anchors.json"
RETRY_STATE = OUTPUT_DIR / "retry-state.json"
HISTORY_FILE = ROOT / "logs" / "refresh-history.jsonl"

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
LEAGUE_CODES = list(LEAGUE_CLASS_TYPES)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def next_retry_slot(value: datetime) -> datetime:
    return value + timedelta(minutes=5)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_history(event: dict[str, Any]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as history:
        history.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_github_outputs(**values: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if isinstance(value, bool):
                value = str(value).lower()
            output.write(f"{key}={value}\n")


def write_step_summary(title: str, successful: list[str], failed: list[str], note: str = "") -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## {title}",
        "",
        f"- 성공: {', '.join(successful) if successful else '없음'}",
        f"- 실패: {', '.join(failed) if failed else '없음'}",
    ]
    if note:
        lines.append(f"- {note}")
    with Path(path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def valid_leagues(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    selected = {str(value).upper() for value in values}
    return [league for league in LEAGUE_CODES if league in selected]


def snapshot_successes(snapshot: Any, selected: list[str]) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    summaries = snapshot.get("summary", [])
    if not isinstance(summaries, list):
        return []
    succeeded = {
        str(item.get("league", "")).upper()
        for item in summaries
        if isinstance(item, dict) and item.get("ok") is True
    }
    return [league for league in selected if league in succeeded]


def record_full(exit_code: int, log_file: str) -> int:
    recorded_at = now_utc()
    snapshot = read_json(LATEST_SNAPSHOT, {})
    successful = snapshot_successes(snapshot, LEAGUE_CODES) if exit_code in (0, 2) else []
    failed = [league for league in LEAGUE_CODES if league not in successful]
    publish = exit_code in (0, 2) and isinstance(snapshot, dict) and bool(snapshot.get("rows"))
    retry_after = next_retry_slot(recorded_at) if failed else None

    state = {
        "version": 1,
        "sourceRunId": os.environ.get("GITHUB_RUN_ID", "local"),
        "recordedAt": recorded_at.isoformat(),
        "firstExitCode": exit_code,
        "pendingLeagues": failed,
        "retryAfter": retry_after.isoformat() if retry_after else None,
        "retryCompleted": not bool(failed),
        "remainingFailedLeagues": [],
    }
    write_json_atomic(RETRY_STATE, state)
    append_history(
        {
            "type": "full-refresh",
            "runId": state["sourceRunId"],
            "recordedAt": state["recordedAt"],
            "exitCode": exit_code,
            "successfulLeagues": successful,
            "failedLeagues": failed,
            "retryAfter": state["retryAfter"],
            "logFile": Path(log_file).name if log_file else None,
        }
    )

    note = "실패 리그는 약 5분 뒤 한 번 재시도합니다." if failed else "모든 리그가 갱신되었습니다."
    write_step_summary("1시간 정기 갱신", successful, failed, note)
    write_github_outputs(publish=publish, fatal=exit_code not in (0, 2), failed=bool(failed))
    return 0


def prepare_retry() -> int:
    state = read_json(RETRY_STATE, {})
    pending = valid_leagues(state.get("pendingLeagues")) if isinstance(state, dict) else []
    completed = bool(state.get("retryCompleted")) if isinstance(state, dict) else True
    needed = bool(pending) and not completed
    write_github_outputs(needed=needed, leagues=",".join(pending))
    if needed:
        print("재시도 대상: " + ", ".join(pending))
    else:
        print("재시도할 실패 리그가 없습니다.")
    return 0


def merge_successful_retry(latest: dict[str, Any], partial: dict[str, Any], successful: list[str]) -> None:
    successful_types = {LEAGUE_CLASS_TYPES[league] for league in successful}
    latest_rows = latest.get("rows", []) if isinstance(latest.get("rows"), list) else []
    partial_rows = partial.get("rows", []) if isinstance(partial.get("rows"), list) else []
    merged_rows = [row for row in latest_rows if row.get("class_type") not in successful_types]
    merged_rows.extend(row for row in partial_rows if row.get("class_type") in successful_types)
    merged_rows.sort(key=lambda row: (-int(row.get("class_type", 0)), int(row.get("rank", 999999))))
    latest["rows"] = merged_rows

    latest_leagues = latest.get("leagues", {}) if isinstance(latest.get("leagues"), dict) else {}
    partial_leagues = partial.get("leagues", {}) if isinstance(partial.get("leagues"), dict) else {}
    for league in successful:
        if isinstance(partial_leagues.get(league), dict):
            latest_leagues[league] = partial_leagues[league]
    latest["leagues"] = latest_leagues

    latest_summary = latest.get("summary", []) if isinstance(latest.get("summary"), list) else []
    summary_by_league = {
        str(item.get("league", "")).upper(): item
        for item in latest_summary
        if isinstance(item, dict)
    }
    partial_summary = partial.get("summary", []) if isinstance(partial.get("summary"), list) else []
    for item in partial_summary:
        if isinstance(item, dict) and str(item.get("league", "")).upper() in successful:
            summary_by_league[str(item["league"]).upper()] = item
    latest["summary"] = [summary_by_league[league] for league in LEAGUE_CODES if league in summary_by_league]
    latest["lastRetryAt"] = now_utc().isoformat()


def record_retry(exit_code: int, log_file: str) -> int:
    recorded_at = now_utc()
    state = read_json(RETRY_STATE, {})
    selected = valid_leagues(state.get("pendingLeagues")) if isinstance(state, dict) else []
    partial_path = OUTPUT_DIR / ("partial-" + "-".join(selected).lower() + ".json")
    partial = read_json(partial_path, {}) if selected else {}
    latest = read_json(LATEST_SNAPSHOT, {})

    successful = snapshot_successes(partial, selected) if exit_code in (0, 2) else []
    if successful and isinstance(latest, dict) and isinstance(partial, dict):
        merge_successful_retry(latest, partial, successful)
        write_json_atomic(LATEST_SNAPSHOT, latest)
    failed = [league for league in selected if league not in successful]

    if not isinstance(state, dict):
        state = {}
    state.update(
        {
            "retryCompleted": True,
            "retriedAt": recorded_at.isoformat(),
            "retryExitCode": exit_code,
            "pendingLeagues": [],
            "remainingFailedLeagues": failed,
        }
    )
    write_json_atomic(RETRY_STATE, state)
    append_history(
        {
            "type": "five-minute-retry",
            "runId": os.environ.get("GITHUB_RUN_ID", "local"),
            "sourceRunId": state.get("sourceRunId"),
            "recordedAt": recorded_at.isoformat(),
            "exitCode": exit_code,
            "attemptedLeagues": selected,
            "successfulLeagues": successful,
            "failedLeagues": failed,
            "logFile": Path(log_file).name if log_file else None,
        }
    )

    note = "재시도 후에도 실패한 리그는 다음 정기 갱신에서 다시 시도합니다." if failed else "재시도가 완료되었습니다."
    write_step_summary("실패 리그 5분 후 재시도", successful, failed, note)
    write_github_outputs(publish=bool(successful), unresolved=bool(failed), fatal=exit_code not in (0, 2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="비공개 리그 갱신 이력과 재시도 상태를 관리합니다")
    subparsers = parser.add_subparsers(dest="command", required=True)

    full = subparsers.add_parser("record-full")
    full.add_argument("--exit-code", type=int, required=True)
    full.add_argument("--log-file", default="")

    subparsers.add_parser("prepare-retry")

    retry = subparsers.add_parser("record-retry")
    retry.add_argument("--exit-code", type=int, required=True)
    retry.add_argument("--log-file", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "record-full":
        return record_full(args.exit_code, args.log_file)
    if args.command == "prepare-retry":
        return prepare_retry()
    if args.command == "record-retry":
        return record_retry(args.exit_code, args.log_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
