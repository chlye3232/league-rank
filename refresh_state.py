#!/usr/bin/env python3
"""Prepare grouped refreshes, merge partial results, and keep private history."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
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
GROUP_LEAGUES = {
    "A": ["A1", "A2", "A3"],
    "B": ["B1", "B2", "B3", "B4", "B5"],
    "C": ["C1", "C2", "C3", "C4", "C5"],
    "D": ["D1", "D2", "D3", "D4", "D5"],
    "all": LEAGUE_CODES,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def write_step_summary(title: str, successful: list[str], failed: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"## {title}",
        "",
        f"- 성공: {', '.join(successful) if successful else '없음'}",
        f"- 실패: {', '.join(failed) if failed else '없음'}",
    ]
    with Path(path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def parse_leagues(value: str) -> list[str]:
    requested = {item.strip().upper() for item in value.split(",") if item.strip()}
    selected = [league for league in LEAGUE_CODES if league in requested]
    if not selected or len(selected) != len(requested):
        raise SystemExit("올바르지 않은 리그 목록입니다")
    return selected


def snapshot_path(selected: list[str]) -> Path:
    if selected == LEAGUE_CODES:
        return LATEST_SNAPSHOT
    return OUTPUT_DIR / ("partial-" + "-".join(selected).lower() + ".json")


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


def merge_successes(latest: dict[str, Any], partial: dict[str, Any], successful: list[str]) -> None:
    successful_types = {LEAGUE_CLASS_TYPES[league] for league in successful}
    latest_rows = latest.get("rows", []) if isinstance(latest.get("rows"), list) else []
    partial_rows = partial.get("rows", []) if isinstance(partial.get("rows"), list) else []
    merged_rows = [
        row for row in latest_rows
        if isinstance(row, dict) and row.get("class_type") not in successful_types
    ]
    merged_rows.extend(
        row for row in partial_rows
        if isinstance(row, dict) and row.get("class_type") in successful_types
    )
    merged_rows.sort(key=lambda row: (-int(row.get("class_type", 0)), int(row.get("rank", 999999))))
    latest["rows"] = merged_rows

    latest_leagues = latest.get("leagues", {}) if isinstance(latest.get("leagues"), dict) else {}
    partial_leagues = partial.get("leagues", {}) if isinstance(partial.get("leagues"), dict) else {}
    for league in successful:
        if isinstance(partial_leagues.get(league), dict):
            latest_leagues[league] = partial_leagues[league]
    latest["leagues"] = latest_leagues

    latest_summary = latest.get("summary", []) if isinstance(latest.get("summary"), list) else []
    summaries = {
        str(item.get("league", "")).upper(): item
        for item in latest_summary
        if isinstance(item, dict)
    }
    partial_summary = partial.get("summary", []) if isinstance(partial.get("summary"), list) else []
    for item in partial_summary:
        league = str(item.get("league", "")).upper() if isinstance(item, dict) else ""
        if league in successful:
            summaries[league] = item
    latest["summary"] = [summaries[league] for league in LEAGUE_CODES if league in summaries]
    latest["lastPartialRefreshAt"] = now_iso()


def prepare(group: str) -> int:
    normalized = group if group == "all" else group.upper()
    selected = GROUP_LEAGUES.get(normalized)
    if not selected:
        raise SystemExit(f"알 수 없는 리그 그룹: {group}")
    write_github_outputs(leagues=",".join(selected), count=len(selected))
    print(f"{normalized} 그룹: {', '.join(selected)}")
    return 0


def record_attempt(group: str, phase: str, league_value: str, exit_code: int, log_file: str) -> int:
    selected = parse_leagues(league_value)
    attempt_path = snapshot_path(selected)
    attempt = read_json(attempt_path, {}) if exit_code in (0, 2) else {}
    successful = snapshot_successes(attempt, selected)
    failed = [league for league in selected if league not in successful]

    if successful and attempt_path != LATEST_SNAPSHOT:
        latest = read_json(LATEST_SNAPSHOT, {})
        if not isinstance(latest, dict) or not isinstance(attempt, dict):
            successful = []
            failed = selected
        else:
            merge_successes(latest, attempt, successful)
            write_json_atomic(LATEST_SNAPSHOT, latest)

    recorded_at = now_iso()
    state = {
        "version": 2,
        "runId": os.environ.get("GITHUB_RUN_ID", "local"),
        "group": group,
        "phase": phase,
        "recordedAt": recorded_at,
        "exitCode": exit_code,
        "attemptedLeagues": selected,
        "successfulLeagues": successful,
        "failedLeagues": failed,
    }
    write_json_atomic(RETRY_STATE, state)
    append_history(
        {
            "type": "league-refresh-attempt",
            **state,
            "logFile": Path(log_file).name if log_file else None,
        }
    )
    write_step_summary(f"{group} 그룹 · {phase}", successful, failed)
    write_github_outputs(
        publish=bool(successful),
        successful=",".join(successful),
        failed=",".join(failed),
        failed_count=len(failed),
        fatal=exit_code not in (0, 2),
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="그룹별 리그 갱신 상태를 비공개로 관리합니다")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--group", required=True)

    record = commands.add_parser("record-attempt")
    record.add_argument("--group", required=True)
    record.add_argument("--phase", choices=("direct", "proxy", "delayed"), required=True)
    record.add_argument("--leagues", required=True)
    record.add_argument("--exit-code", type=int, required=True)
    record.add_argument("--log-file", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "prepare":
        return prepare(args.group)
    if args.command == "record-attempt":
        return record_attempt(args.group, args.phase, args.leagues, args.exit_code, args.log_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
