#!/usr/bin/env python3
"""TikTok session check, captured X-Argus persona import, and SMS OTP login helper."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from league_rank_updater import (
    DEFAULT_CACHE_PATH,
    DEFAULT_OUTPUT_DIR,
    PASSPORT_BASE_URL,
    TikTokAuthError,
    TikTokClient,
    configure_console,
    load_dotenv,
    load_persona_overrides_from_env,
    parse_board,
    parse_proxy_urls,
)
from signer import (
    DeviceRegistrationClient,
    RegistrationResponseError,
    SignedSession,
    SignupClient,
    SignupError,
    TikTokSessionState,
    build_common_params,
    build_user_agent,
    default_kr_env_overrides,
    default_kr_persona,
    extract_persona_from_argus,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SESSION_FILE = DEFAULT_OUTPUT_DIR / "tiktok-session.json"


def _resolve_session_path(arg_path: Path | None) -> Path:
    if arg_path is not None:
        return arg_path
    env_path = os.environ.get("TIKTOK_SESSION_FILE", "").strip()
    return Path(env_path) if env_path else DEFAULT_SESSION_FILE


def _mask(secret: str) -> str:
    if not secret:
        return "(없음)"
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]} (len={len(secret)})"


def cmd_check(args: argparse.Namespace) -> int:
    session_path = _resolve_session_path(args.session_file)
    token = args.token or os.environ.get("TIKTOK_TT_TOKEN", "")
    proxies = parse_proxy_urls(args.proxy or os.environ.get("TIKTOK_PROXY_URLS", ""))
    persona_overrides = load_persona_overrides_from_env()

    with TikTokClient(
        token,
        DEFAULT_CACHE_PATH,
        proxy_urls=proxies,
        session_path=session_path,
        persona_overrides=persona_overrides,
        enable_token_beat=False,
    ) as client:
        if not client.active_token:
            print("오류: 활성 TIKTOK_TT_TOKEN 또는 세션 파일이 없습니다.", file=sys.stderr)
            return 1

        print(f"전송 계층: {client.signed_session.backend_name}")
        print(f"세션 파일: {session_path}")
        print(f"토큰 상태: {_mask(client.active_token)}")
        print(f"기기 ID: {client.session_state.persona.get('deviceId')}")

        env_overrides = default_kr_env_overrides()
        persona = client.session_state.persona
        signup = SignupClient(
            client.signed_session,
            base_url=PASSPORT_BASE_URL,
            state={
                "status": "completed",
                "nextAction": "completed",
                "sessionToken": client.active_token,
                **client.session_state.ticket_guard.to_dict(),
            },
            common_headers={"user-agent": build_user_agent(persona, env_overrides)},
            query_params=build_common_params(persona, env_overrides),
            proxy_url=proxies[0] if proxies else None,
        )
        try:
            signup.token_beat(scene="boot", first_beat=True)
            client.session_state.session_token = str(signup.state.get("sessionToken") or client.active_token)
            client.session_state.last_beat_at = datetime.now(timezone.utc).isoformat()
            print("token_beat: 성공")
        except Exception as error:
            print(f"token_beat: 실패/건너뜀 ({error})")

        try:
            info = signup.get_account_info(scene="login")
            print(f"account_info: uid={info.get('accountUserId') or '-'}, screenName={info.get('screenName') or '-'}")
            if info.get("accountUserId"):
                client.session_state.account_user_id = str(info["accountUserId"])
            if info.get("screenName"):
                client.session_state.screen_name = str(info["screenName"])
        except Exception as error:
            print(f"account_info: 실패/건너뜀 ({error})")

        try:
            payload = client.fetch_ranking(args.anchor_id)
            board = parse_board(payload, "probe", args.anchor_id)
            print(f"랭킹 API 테스트: 성공 (class_type={board.class_type}, {len(board.ranks)}명)")
            client.persist_session(force=True)
            return 0
        except TikTokAuthError as error:
            print(f"랭킹 API 테스트: 인증 만료 ({error})", file=sys.stderr)
            return 3
        except Exception as error:
            print(f"랭킹 API 테스트: 오류 ({error})", file=sys.stderr)
            return 1


def cmd_from_argus(args: argparse.Namespace) -> int:
    session_path = _resolve_session_path(args.session_file)
    extracted = extract_persona_from_argus(args.argus.strip(), args.url.strip() if args.url else None)
    state = TikTokSessionState.load(
        session_path,
        env_token=args.token or os.environ.get("TIKTOK_TT_TOKEN", ""),
        persona_overrides=extracted,
    )
    state.save(session_path)

    print(f"세션 페르소나 저장 완료: {session_path}")
    print(f"추출된 필드: {', '.join(sorted(extracted))}")
    if args.print_secret:
        print("\n--- TIKTOK_PERSONA_JSON ---")
        print(json.dumps(state.persona, ensure_ascii=False, separators=(",", ":")))
        print("\n--- TIKTOK_SESSION_JSON ---")
        print(json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    session_path = _resolve_session_path(args.session_file)
    proxies = parse_proxy_urls(args.proxy or os.environ.get("TIKTOK_PROXY_URLS", ""))
    proxy_url = proxies[0] if proxies else None
    persona = {**default_kr_persona(device_id="", install_id=""), **load_persona_overrides_from_env(), "allowEmptyIdentity": True}
    env_overrides = default_kr_env_overrides()

    with SignedSession(persona, timeout=20.0) as session:
        client = DeviceRegistrationClient(
            session,
            persona,
            common_headers={"user-agent": build_user_agent(persona, env_overrides)},
            overrides=env_overrides,
            query_params=build_common_params(persona, env_overrides),
        )
        try:
            identity = client.register(proxy_url=proxy_url)
        except (RegistrationResponseError, RuntimeError) as error:
            print(f"기기 등록 실패: {error}", file=sys.stderr)
            return 1

    state = TikTokSessionState.load(session_path, env_token=os.environ.get("TIKTOK_TT_TOKEN", ""), persona_overrides=identity)
    state.save(session_path)
    print(f"기기 등록 성공: deviceId={identity['deviceId']}, installId={identity['installId']}")
    print(f"저장 완료: {session_path}")
    return 0


def cmd_sms_login(args: argparse.Namespace) -> int:
    session_path = _resolve_session_path(args.session_file)
    proxies = parse_proxy_urls(args.proxy or os.environ.get("TIKTOK_PROXY_URLS", ""))
    proxy_url = proxies[0] if proxies else None
    state = TikTokSessionState.load(session_path, persona_overrides=load_persona_overrides_from_env())
    persona = dict(state.persona)
    env_overrides = default_kr_env_overrides()

    with SignedSession(persona, timeout=25.0) as session:
        if args.register_device:
            reg_persona = {**persona, "deviceId": "", "installId": "", "allowEmptyIdentity": True}
            reg_client = DeviceRegistrationClient(
                session,
                reg_persona,
                common_headers={"user-agent": build_user_agent(reg_persona, env_overrides)},
                overrides=env_overrides,
                query_params=build_common_params(reg_persona, env_overrides),
            )
            identity = reg_client.register(proxy_url=proxy_url)
            persona.update(identity)
            state.persona.update(identity)
            print(f"새 기기 등록 완료: deviceId={identity['deviceId']}")

        signup = SignupClient(
            session,
            base_url=PASSPORT_BASE_URL,
            common_headers={"user-agent": build_user_agent(persona, env_overrides)},
            query_params=build_common_params(persona, env_overrides),
            proxy_url=proxy_url,
        )
        try:
            signup.start_mobile(args.mobile, code_type=args.code_type, allow_existing=True)
            otp = args.code or input("SMS 인증번호 6자리를 입력하세요: ").strip()
            if not otp:
                print("인증번호가 비어 있어 중단합니다.", file=sys.stderr)
                return 1
            signup.complete_mobile_login_only(
                otp,
                birthday=args.birthday,
                carrier_region=args.carrier_region or "KR",
            )
            signup.token_beat(scene="boot", first_beat=True)
        except SignupError as error:
            print(f"로그인 실패 ({error.next_action}, code={error.code}): {error}", file=sys.stderr)
            return 1

        snapshot = signup.snapshot()
        state.session_token = str(signup.state.get("sessionToken") or "")
        state.source_token_hash = ""
        state.ticket_guard = state.ticket_guard.from_dict(signup.state)
        state.cookies = snapshot["cookies"]
        state.last_beat_at = datetime.now(timezone.utc).isoformat()
        state.save(session_path)

    print(f"로그인 성공! 세션 저장 완료: {session_path}")
    print("이제 league_rank_updater.py 실행 시 저장된 세션과 TicketGuard가 자동 사용됩니다.")
    if args.print_secret:
        print("\n--- TIKTOK_TT_TOKEN ---")
        print(state.session_token)
        print("\n--- TIKTOK_SESSION_JSON ---")
        print(json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTok 세션 진단 및 갱신 도우미")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="현재 토큰/세션 생존 여부 및 랭킹 API 테스트")
    check.add_argument("--session-file", type=Path, default=None)
    check.add_argument("--token", default="", help="직접 테스트할 X-Tt-Token")
    check.add_argument("--proxy", default="", help="테스트용 프록시 URL")
    check.add_argument("--anchor-id", default="6996265859489891330", help="랭킹 API 프로브용 anchor_id")

    argus_cmd = sub.add_parser("from-argus", help="캡처한 X-Argus(+URL)에서 기기 페르소나·동적 필드 추출")
    argus_cmd.add_argument("--argus", required=True, help="캡처한 X-Argus 헤더 값")
    argus_cmd.add_argument("--url", default="", help="해당 요청의 전체 URL (f26 동적 필드 검증 시 필요)")
    argus_cmd.add_argument("--token", default="", help="함께 저장할 X-Tt-Token")
    argus_cmd.add_argument("--session-file", type=Path, default=None)
    argus_cmd.add_argument("--print-secret", action="store_true", help="GitHub Secret 복사용 JSON 출력")

    reg = sub.add_parser("register", help="새 device_id / install_id 등록 시도")
    reg.add_argument("--proxy", default="", help="프록시 URL")
    reg.add_argument("--session-file", type=Path, default=None)

    sms = sub.add_parser("sms-login", help="모바일 SMS OTP 로그인으로 새 세션 발급")
    sms.add_argument("--mobile", required=True, help="국가코드 포함 번호 (예: +821012345678)")
    sms.add_argument("--code", default="", help="미입력 시 터미널에서 OTP 입력")
    sms.add_argument("--code-type", type=int, default=24)
    sms.add_argument("--birthday", default=None)
    sms.add_argument("--carrier-region", default="KR")
    sms.add_argument("--proxy", default="", help="프록시 URL")
    sms.add_argument("--register-device", action="store_true", help="로그인 전 새 device_id 등록 시도")
    sms.add_argument("--session-file", type=Path, default=None)
    sms.add_argument("--print-secret", action="store_true", help="GitHub Secret 복사용 토큰/JSON 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    load_dotenv(SCRIPT_DIR / ".env")
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "from-argus":
        return cmd_from_argus(args)
    if args.command == "register":
        return cmd_register(args)
    if args.command == "sms-login":
        return cmd_sms_login(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
