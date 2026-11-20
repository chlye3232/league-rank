"""Build TikTok request profiles and default KR persona/environment contracts."""
from __future__ import annotations

import time
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

PACKAGE = "com.zhiliaoapp.musically"
TTNET_VERSION = "093e42e6 2026-09-28"
QUIC_VERSION = "ff43781e 2026-09-28"

DEFAULT_DEVICE_ID = "7642361429727249936"
DEFAULT_INSTALL_ID = "7642608726025438992"

_COMMON_ENVIRONMENT_FIELDS = (
    "app_language",
    "carrier_region",
    "carrier_region_v2",
    "current_region",
    "language",
    "last_install_time",
    "locale",
    "mcc_mnc",
    "op_region",
    "os_api",
    "region",
    "residence",
    "sys_region",
    "timezone_name",
    "timezone_offset",
)


def default_kr_persona(
    *,
    device_id: str = DEFAULT_DEVICE_ID,
    install_id: str = DEFAULT_INSTALL_ID,
) -> dict[str, Any]:
    """Return the default 47.7.2 Android persona used for KR TikTok requests."""
    return {
        "deviceId": device_id,
        "installId": install_id,
        "appId": "1233",
        "licenseId": 2_142_840_551,
        "sdkVersion": "v05.03.04-ov-android",
        "sdkVersionCode": 0x0503_0420,
        "appVersion": "47.7.2",
        "versionCode": "2024707020",
        "model": "Pixel 7",
        "deviceBrand": "google",
        "device": "panther",
        "build": "TQ3A.230805.001",
        "release": "13",
        "securityPatch": "2023-08-05",
        "fingerprint": "google/panther/panther:13/TQ3A.230805.001/10216780:user/release-keys",
        "channel": "googleplay",
        "resolution": "1080x2400",
        "dpi": 420,
        "hostAbi": "arm64-v8a",
    }


def default_kr_env_overrides(*, last_install_time: int = 1779433522) -> dict[str, Any]:
    """Return default KR region/locale overrides for common query params."""
    return {
        "app_language": "ko",
        "carrier_region": "KR",
        "carrier_region_v2": "450",
        "current_region": "KR",
        "language": "ko",
        "last_install_time": last_install_time,
        "locale": "ko_KR",
        "mcc_mnc": "45002",
        "op_region": "KR",
        "os_api": 33,
        "region": "KR",
        "residence": "KR",
        "sys_region": "KR",
        "timezone_name": "Asia/Seoul",
        "timezone_offset": 32400,
    }


def _required(values: Mapping[str, Any], key: str) -> Any:
    if key not in values or values[key] is None or values[key] == "":
        raise ValueError(f"persona field {key!r} is required")
    return values[key]


def _environment(overrides: Mapping[str, Any], key: str) -> Any:
    if key not in overrides or overrides[key] is None:
        raise ValueError(f"request override {key!r} is required")
    return overrides[key]


def _dimensions(persona: Mapping[str, Any]) -> tuple[int, int]:
    resolution = str(_required(persona, "resolution"))
    try:
        width, height = (int(part) for part in resolution.split("x"))
    except ValueError as exc:
        raise ValueError("persona resolution must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0 or resolution != f"{width}x{height}":
        raise ValueError("persona resolution must be WIDTHxHEIGHT")
    return width, height


def _short_version_code(app_version: str) -> int:
    try:
        major, minor, patch = (int(part) for part in app_version.split("."))
    except ValueError as exc:
        raise ValueError("persona appVersion must be MAJOR.MINOR.PATCH") from exc
    if min(major, minor, patch) < 0 or minor > 99 or patch > 99:
        raise ValueError("persona appVersion must be MAJOR.MINOR.PATCH")
    return (major * 100 + minor) * 100 + patch


def build_user_agent(
    persona: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> str:
    values = dict(persona)
    values.update(overrides or {})
    locale = _required(values, "locale")
    package = values.get("package", PACKAGE)
    if not package:
        raise ValueError("request override 'package' must not be empty")
    return (
        f"{package}/{_required(values, 'versionCode')} "
        f"(Linux; U; Android {_required(values, 'release')}; {locale}; "
        f"{_required(values, 'model')}; Build/{_required(values, 'build')}; "
        f"Cronet/TTNetVersion:{TTNET_VERSION} QuicVersion:{QUIC_VERSION})"
    )


def build_common_params(
    persona: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    *,
    now_ms: int | None = None,
) -> dict[str, str]:
    overrides = dict(overrides or {})
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer")

    app_version = str(_required(persona, "appVersion"))
    version_code = str(_required(persona, "versionCode"))
    width, height = _dimensions(persona)
    params: dict[str, Any] = {
        "_rticket": now_ms,
        "is_pad": 0,
        "host_abi": persona.get("hostAbi", "arm64-v8a"),
        "ts": now_ms // 1000,
        "app_version": version_code,
        "ab_version": app_version,
        "ac": "mobile",
        "ac2": "unknown",
        "aid": _required(persona, "appId"),
        "app_name": "musical_ly",
        "app_type": "normal",
        "build_number": app_version,
        "channel": _required(persona, "channel"),
        "device_brand": persona.get("deviceBrand", "google"),
        "device_platform": "android",
        "device_type": _required(persona, "model"),
        "dpi": _required(persona, "dpi"),
        "manifest_version_code": version_code,
        "update_version_code": version_code,
        "os": "android",
        "os_version": _required(persona, "release"),
        "resolution": f"{width}*{height}",
        "ssmix": "a",
        "uoo": 0,
        "version_code": _short_version_code(app_version),
        "version_name": app_version,
    }
    for key in _COMMON_ENVIRONMENT_FIELDS:
        params[key] = _environment(overrides, key)
    for persona_key, wire_key in (("deviceId", "device_id"), ("installId", "iid")):
        value = persona.get(persona_key)
        if value is not None and value != "":
            params[wire_key] = value
    params.update(overrides)
    return {key: str(value) for key, value in params.items() if value is not None and value != ""}


def with_common_params(url: str, common_params: Mapping[str, Any]) -> str:
    if not common_params:
        return url
    parts = urlsplit(url)
    encoded = urlencode({key: str(value) for key, value in common_params.items()})
    query = f"{parts.query}&{encoded}" if parts.query else encoded
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def build_registration_header(
    persona: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = dict(overrides or {})
    app_version = str(_required(persona, "appVersion"))
    version_code = str(_required(persona, "versionCode"))
    width, height = _dimensions(persona)
    dpi = _required(persona, "dpi")
    brand = persona.get("deviceBrand", "google")
    header: dict[str, Any] = {
        "channel": _required(persona, "channel"),
        "not_request_sender": 1,
        "aid": _required(persona, "appId"),
        "user_agent": build_user_agent(persona, overrides),
        "ab_version": app_version,
        "app_language": _environment(overrides, "app_language"),
        "package": overrides.get("package", PACKAGE),
        "app_version": app_version,
        "version_code": version_code,
        "update_version_code": version_code,
        "manifest_version_code": version_code,
        "device_platform": "android",
        "os": "Android",
        "os_version": _required(persona, "release"),
        "os_api": _environment(overrides, "os_api"),
        "device_model": _required(persona, "model"),
        "device_brand": brand,
        "device_manufacturer": brand,
        "cpu_abi": persona.get("hostAbi", "arm64-v8a"),
        "density_dpi": dpi,
        "display_density": {
            120: "ldpi",
            160: "mdpi",
            240: "hdpi",
            320: "xhdpi",
            480: "xxhdpi",
            640: "xxxhdpi",
        }[min(
            (120, 160, 240, 320, 480, 640),
            key=lambda bucket: abs(bucket - dpi),
        )],
        "display_density_v2": dpi,
        "resolution": f"{height}x{width}",
        "rom": _required(persona, "build"),
        "rom_version": _required(persona, "fingerprint"),
        "language": _environment(overrides, "language"),
        "region": _environment(overrides, "region"),
        "sdk_version": "2.5.14.8",
        "sdk_target_version": 30,
        "sdk_flavor": "i18nInner",
        "git_hash": "e9b5c24",
    }
    for persona_key, wire_key in (
        ("deviceId", "device_id"),
        ("installId", "install_id"),
        ("deviceToken", "device_token"),
    ):
        value = persona.get(persona_key)
        if value is not None and value != "":
            header[wire_key] = value
    header.update(overrides)
    for key in ("device_id", "install_id", "bd_did", "ssid", "device_token"):
        if key in header and (header[key] is None or header[key] == ""):
            del header[key]
    return header
