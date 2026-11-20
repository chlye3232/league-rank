"""Resumable TikTok email/mobile auth, token_beat, and account verification client."""
from __future__ import annotations

import gzip
import json
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from .profiles import with_common_params
from .session_state import DEFAULT_COOKIE_ORIGINS
from .ticket_guard import TicketGuardState
from .transport import HttpResponse, SignedSession


def mix_encode(value: str) -> str:
    return bytes(byte ^ 5 for byte in value.encode("utf-8")).hex()


def mix_decode(value: str) -> str:
    return bytes(byte ^ 5 for byte in bytes.fromhex(value)).decode("utf-8")


class SignupError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, data: Any = None, next_action: str = "failed"):
        super().__init__(message)
        self.code = code
        self.data = data
        self.next_action = next_action


class SignupClient:
    def __init__(
        self,
        session: SignedSession,
        *,
        base_url: str = "https://api16-normal-c-alisg.tiktokv.com",
        state: Mapping[str, Any] | None = None,
        common_headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, Any] | None = None,
        proxy_url: str | None = None,
    ):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.state = dict(state or {"status": "new", "nextAction": "start"})
        self.origin = self.base_url
        self.common_headers = dict(common_headers or {})
        self.query_params = dict(query_params or {})
        self.proxy_url = proxy_url
        self.ticket_guard = TicketGuardState.from_dict(self.state)
        priv_hex, pub_b64 = self.ticket_guard.ensure_keypair()
        self.state["ticketGuardPrivateKey"] = priv_hex
        self.state["ticketGuardPublicKey"] = pub_b64

    def snapshot(self) -> dict[str, Any]:
        origins = list(dict.fromkeys([self.origin, *DEFAULT_COOKIE_ORIGINS]))
        return {
            "state": self.state,
            "cookies": self.session.dump_cookies(origins),
        }

    def public_state(self) -> dict[str, Any]:
        fields = (
            "status", "nextAction", "method", "email", "mobile", "hasAccount", "retryTime",
            "loginName", "uniqueId", "accountUserId", "screenName", "tokenBeatScene",
        )
        return {key: self.state[key] for key in fields if key in self.state}

    def _decode(self, response: HttpResponse) -> dict[str, Any]:
        if not response.ok:
            raise SignupError(f"HTTP {response.status}", code=response.status, next_action="failed")
        raw = response.content
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SignupError("invalid JSON response", data=error, next_action="failed") from error
        if not isinstance(result, dict):
            raise SignupError("response root must be an object", next_action="failed")

        data = result.get("data")
        data_dict = data if isinstance(data, dict) else {}
        message = result.get("message")
        code = result.get("error_code", result.get("code"))
        if code in (None, 0, "0"):
            code = data_dict.get("error_code", data_dict.get("code"))
        status_code = result.get("status_code")
        if status_code not in (None, 0, "0") and code in (None, 0, "0"):
            raise SignupError(
                str(result.get("status_msg") or result.get("message") or f"aweme error {status_code}"),
                code=status_code,
                data=result,
                next_action="failed",
            )
        if (message in {"error", "exception"} or code not in (None, 0, "0")) and not data_dict.get("sms_code_key"):
            challenge_keys = {
                "captcha",
                "recaptcha",
                "recaptcha_token",
                "shark_ticket",
                "verify_ticket",
                "verify_center_decision_conf",
            }
            next_action = "needs_challenge" if challenge_keys.intersection(data_dict) else "failed"
            description = (
                data_dict.get("description")
                or result.get("description")
                or data_dict.get("message")
                or (message if message not in {None, "error", "exception"} else None)
                or f"signup error {code}"
            )
            raise SignupError(str(description), code=code, data=data, next_action=next_action)
        return result

    def _sync_ticket_guard_state(self) -> None:
        self.state["ticketGuardPrivateKey"] = self.ticket_guard.private_key
        self.state["ticketGuardPublicKey"] = self.ticket_guard.public_key
        if self.ticket_guard.ticket:
            self.state["ticketGuardTicket"] = self.ticket_guard.ticket
            self.state["ticketGuardTsSign"] = self.ticket_guard.ts_sign

    def _request(
        self,
        path: str,
        fields: Mapping[str, Any],
        *,
        method: str = "POST",
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        clean_path = urlsplit(path).path
        is_passport = clean_path.startswith("/passport/")
        body_fields = dict(fields)
        if is_passport and method != "GET":
            body_fields.setdefault("account_sdk_source", "app")
            body_fields.setdefault("multi_login", "1")
        encoded = urlencode(list(body_fields.items()))
        query_params = dict(self.query_params)
        if is_passport:
            query_params.setdefault("passport-sdk-version", "1")
            if method == "GET":
                query_params.setdefault("account_sdk_source", "app")
                query_params.setdefault("multi_login", "1")
        url = with_common_params(self.base_url + path, query_params)

        headers = dict(self.common_headers)
        headers.setdefault("sdk-version", "2")
        headers.setdefault("x-tt-bypass-dp", "1")
        token = str(self.state.get("sessionToken") or "").strip()
        if token:
            headers.setdefault("x-tt-token", token)
        headers.update(self.ticket_guard.build_headers(clean_path, has_token=bool(token)))
        self._sync_ticket_guard_state()

        if is_passport:
            headers.setdefault("passport-sdk-version", "1")
            headers.setdefault("passport-sdk-settings", "x-tt-token")
            headers.setdefault("passport-sdk-sign", "x-tt-token")
            headers.setdefault("x-vc-bdturing-sdk-version", "2.4.4.i18n")
            cookies = self.session.get_cookies(url)
            csrf = cookies.get("passport_csrf_token") or cookies.get("passport_csrf_token_default")
            if csrf:
                headers.setdefault("x-tt-passport-csrf-token", csrf)

        effective_proxy = proxy_url if proxy_url is not None else self.proxy_url
        if method == "GET":
            response = self.session.get(
                with_common_params(url, body_fields),
                headers=headers,
                proxy_url=effective_proxy,
            )
        else:
            response = self.session.post(
                url,
                headers={**headers, "content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                body=encoded.encode("utf-8"),
                proxy_url=effective_proxy,
            )

        self._last_response = response
        if self.ticket_guard.capture_server_data(response.headers):
            self._sync_ticket_guard_state()
        new_token = response.header("x-tt-token").strip()
        if new_token and "sessionToken" in self.state:
            self.state["sessionToken"] = new_token
        return self._decode(response)

    @property
    def _token(self) -> str | None:
        resp = getattr(self, "_last_response", None)
        if resp is None:
            return None
        val = resp.header("x-tt-token").strip()
        return val or None

    @staticmethod
    def _data(result: Mapping[str, Any]) -> dict[str, Any]:
        data = result.get("data", {})
        return data if isinstance(data, dict) else {}

    def _complete_result(self, result: Mapping[str, Any]) -> None:
        token = self._token
        if not token:
            raise SignupError("login/signup response is missing x-tt-token", data=result)
        if not isinstance(result.get("data"), dict) or not result["data"]:
            raise SignupError("login/signup response is missing user data", data=result)
        self.state.update({
            "status": "completed",
            "nextAction": "completed",
            "result": result,
            "sessionToken": token,
        })

    def start_mobile(
        self,
        mobile: str,
        code_type: int = 24,
        *,
        auto_read: int = 0,
        check_register: int = 0,
        tickets: Mapping[str, str] | None = None,
        allow_existing: bool = True,
    ) -> dict[str, Any]:
        sent = self._request("/passport/mobile/send_code/v1/", {
            "mobile": mix_encode(mobile),
            "type": mix_encode(str(code_type)),
            "unbind_exist": mix_encode("0"),
            "mix_mode": "1",
            "check_register": str(check_register),
            "is6Digits": "1",
            "auto_read": str(auto_read),
            **dict(tickets or {}),
        })
        data = self._data(sent)
        if not allow_existing and data.get("has_account"):
            raise SignupError("mobile already has an account", code="already_registered", data=data)
        self.state.update({
            "status": "needs_otp",
            "nextAction": "submit_otp",
            "method": "mobile",
            "mobile": mobile,
            "codeType": code_type,
            "hasAccount": data.get("has_account"),
            "retryTime": data.get("retry_time", 30),
            **({"mobileTicket": data["mobile_ticket"]} if data.get("mobile_ticket") else {}),
        })
        return self.public_state()

    def submit_otp(self, code: str) -> dict[str, Any]:
        if self.state.get("status") != "needs_otp":
            raise SignupError("OTP is not expected in this state", next_action=self.state.get("nextAction", "failed"))
        if self.state.get("method") == "email":
            result = self._request("/passport/email/check_code/", {
                "email": mix_encode(self.state["email"]),
                "code": mix_encode(code),
                "type": mix_encode(str(self.state["codeType"])),
                "mix_mode": "1",
                "fixed_mix_mode": "1",
            })
        elif self.state.get("method") == "mobile":
            result = self._request("/passport/mobile/check_code/", {
                "mobile": mix_encode(self.state["mobile"]),
                "code": mix_encode(code),
                "type": mix_encode(str(self.state["codeType"])),
                "mix_mode": "1",
                "fixed_mix_mode": "1",
            })
        else:
            raise SignupError("auth method is not initialized")
        ticket = self._data(result).get("ticket")
        if not ticket:
            raise SignupError("check_code response is missing data.ticket", data=result)
        self.state.update({
            "status": "verified",
            "nextAction": "complete",
            "ticket": ticket,
            "otp": code,
        })
        return self.public_state()

    def complete_mobile_login_only(
        self,
        code: str | None = None,
        *,
        birthday: str | None = None,
        carrier_region: str | None = None,
        verify_reused_mobile: bool = True,
        captcha: str | None = None,
    ) -> dict[str, Any]:
        otp = code or self.state.get("otp")
        if self.state.get("method") != "mobile" or self.state.get("status") not in {"needs_otp", "verified"} or not otp:
            raise SignupError("mobile OTP verification is required first")
        self.state["otp"] = otp
        result = self._request("/passport/mobile/sms_login_only/", {
            "mobile": mix_encode(self.state["mobile"]),
            **({"captcha": captcha} if captcha else {}),
            "code": mix_encode(str(otp)),
            "mix_mode": "1",
            "enable_account_selection": "1",
            "scene": "0",
            **({"verify_reused_mobile": "true"} if verify_reused_mobile else {}),
        })
        sms_code_key = self._data(result).get("sms_code_key")
        if sms_code_key:
            self.state["smsCodeKey"] = str(sms_code_key)
            result = self._request("/passport/mobile/sms_login_continue/", {
                "mobile": mix_encode(self.state["mobile"]),
                "sms_code_key": mix_encode(str(sms_code_key)),
                "mix_mode": "1",
                **({"birthday": birthday} if birthday else {}),
                **({"carrier_region": carrier_region} if carrier_region else {}),
            })
        self._complete_result(result)
        return self.public_state()

    def token_beat(
        self,
        *,
        scene: str = "boot",
        first_beat: bool = True,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        self._request(
            "/passport/token/beat/v2/",
            {
                "scene": scene,
                "first_beat": "true" if first_beat else "false",
            },
            method="GET",
            proxy_url=proxy_url,
        )
        self.state["tokenBeatScene"] = scene
        return self.public_state()

    def get_account_info(
        self,
        *,
        scene: str = "login",
        device_migrate_time: int = 0,
        get_info_type: int | None = 1,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "scene": scene,
            "device_migrate_time": str(device_migrate_time),
        }
        if get_info_type is not None:
            fields["get_info_type"] = str(get_info_type)
        result = self._request("/passport/account/info/v2/", fields, proxy_url=proxy_url)
        data = self._data(result)
        uid = data.get("user_id_str") or (str(data["user_id"]) if data.get("user_id") else None)
        screen_name = data.get("screen_name") or data.get("username")
        self.state.update({
            **({"accountUserId": uid} if uid else {}),
            **({"screenName": str(screen_name)} if screen_name else {}),
        })
        return self.public_state()
