#!/usr/bin/env python3
"""Build the public ranking JSON without exposing collector-only fields."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUTPUT_SNAPSHOT = ROOT / "output" / "league-anchors.json"
SEED_SNAPSHOT = ROOT / "seed" / "league-anchors.json"
PUBLIC_DATA = ROOT / "public" / "data" / "rankings.json"

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


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"스냅샷을 읽지 못했습니다: {path} ({error})") from error
    if not isinstance(value, dict):
        raise SystemExit(f"스냅샷 형식이 올바르지 않습니다: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_public(snapshot: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {league: [] for league in LEAGUE_CLASS_TYPES}
    seen: dict[str, set[str]] = {league: set() for league in LEAGUE_CLASS_TYPES}

    for row in snapshot.get("rows", []):
        if not isinstance(row, dict):
            continue
        league = CLASS_TYPE_LEAGUES.get(row.get("class_type"))
        handle = str(row.get("unique_id") or "").strip()
        try:
            rank = int(row.get("rank"))
        except (TypeError, ValueError):
            continue
        key = handle.casefold()
        if not league or not handle or key in seen[league]:
            continue
        seen[league].add(key)
        grouped[league].append({"rank": rank, "id": handle})

    for items in grouped.values():
        items.sort(key=lambda item: item["rank"])

    leagues = [
        {"code": league, "count": len(grouped[league]), "items": grouped[league]}
        for league in LEAGUE_CLASS_TYPES
    ]
    return {
        "total": sum(item["count"] for item in leagues),
        "leagues": leagues,
    }


def build_seed(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for row in snapshot.get("rows", []):
        if not isinstance(row, dict):
            continue
        if row.get("class_type") not in CLASS_TYPE_LEAGUES:
            continue
        handle = str(row.get("unique_id") or "").strip()
        if not handle:
            continue
        rows.append(
            {
                "class_type": row.get("class_type"),
                "rank": row.get("rank"),
                "unique_id": handle,
            }
        )
    return {
        "leagueDate": snapshot.get("leagueDate"),
        "resolvedAt": snapshot.get("resolvedAt"),
        "leagues": {},
        "rows": rows,
        "summary": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-seed", action="store_true")
    args = parser.parse_args()

    source = OUTPUT_SNAPSHOT if OUTPUT_SNAPSHOT.exists() else SEED_SNAPSHOT
    snapshot = read_json(source)
    public = build_public(snapshot)
    if public["total"] < 1:
        raise SystemExit("공개할 랭킹 ID가 없습니다.")
    write_json_atomic(PUBLIC_DATA, public)
    if args.update_seed:
        write_json_atomic(SEED_SNAPSHOT, build_seed(snapshot))
    print(f"공개 데이터 생성 완료: {public['total']} IDs / {len(public['leagues'])} leagues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
