"""Extract dynamic task fields and persona metadata from a captured X-Argus token."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from .crypto import DynamicFieldsDict, MASK32, ProtoReader, decrypt_argus, dyn_encode


def _decode_argus_fields(argus: str) -> dict[str, Any]:
    protobuf = decrypt_argus(argus)
    reader = ProtoReader(protobuf)
    parsed: dict[str, Any] = {"entries": []}
    while True:
        item = reader.next_field()
        if item is None:
            break
        field, wire, val = item
        if field == 3 and wire == 0:
            assert isinstance(val, int)
            parsed["nonce"] = val >> 1
        elif field == 4 and wire == 2:
            assert isinstance(val, bytes)
            parsed["appId"] = val.decode("utf-8", errors="replace")
        elif field == 6 and wire == 2:
            assert isinstance(val, bytes)
            raw_lic = val.decode("utf-8", errors="replace")
            if raw_lic.isdigit():
                parsed["licenseId"] = int(raw_lic)
        elif field == 7 and wire == 2:
            assert isinstance(val, bytes)
            parsed["appVersion"] = val.decode("utf-8", errors="replace")
        elif field == 8 and wire == 2:
            assert isinstance(val, bytes)
            parsed["sdkVersion"] = val.decode("utf-8", errors="replace")
        elif field == 9 and wire == 0:
            assert isinstance(val, int)
            parsed["sdkVersionCode"] = val >> 1
        elif field == 12 and wire == 0:
            assert isinstance(val, int)
            parsed["nowSeconds"] = val >> 1
        elif field == 16 and wire == 2:
            assert isinstance(val, bytes)
            parsed["deviceToken"] = val.decode("utf-8", errors="replace")
        elif field == 24 and wire == 2:
            assert isinstance(val, bytes)
            parsed["dynSeed"] = val.decode("utf-8")
        elif field == 25 and wire == 0:
            assert isinstance(val, int)
            parsed["seedState"] = val >> 1
        elif field == 26 and wire == 2:
            assert isinstance(val, bytes)
            nested = ProtoReader(val)
            alg: int | None = None
            out_bytes: bytes | None = None
            while True:
                n_item = nested.next_field()
                if n_item is None:
                    break
                nf, nw, nv = n_item
                if nf == 1 and nw == 0:
                    assert isinstance(nv, int)
                    alg = nv >> 1
                elif nf == 2 and nw == 2:
                    assert isinstance(nv, bytes)
                    out_bytes = nv
            if alg is None or out_bytes is None:
                raise RuntimeError("dynamic entry is missing its algorithm or output")
            parsed["entries"].append((alg, out_bytes))
    return parsed


def extract_dynamic_fields(
    argus: str,
    url: str,
    *,
    body: bytes = b"",
) -> DynamicFieldsDict:
    query = urlsplit(url).query or ""
    body_stub = hashlib.md5(body).digest()
    decoded = _decode_argus_fields(argus)

    dyn_seed = decoded.get("dynSeed")
    seed_state = decoded.get("seedState")
    nonce = decoded.get("nonce")
    now_seconds = decoded.get("nowSeconds")
    entries = decoded["entries"]

    if dyn_seed is None:
        raise RuntimeError("Argus has no dynamic seed field")
    if nonce is None or not 0 <= nonce <= MASK32:
        raise RuntimeError("Argus has no valid nonce")
    if now_seconds is None:
        raise RuntimeError("Argus has no timestamp")
    if seed_state is None:
        raise RuntimeError("Argus has no dynamic seed state")

    builtin: tuple[int, int] | None = None
    for idx, (alg, output) in enumerate(entries):
        if 1 <= alg <= 6:
            expected = dyn_encode(alg, query.encode("utf-8"), body_stub, nonce)
            if output == expected:
                builtin = (idx, alg)
                break
    if builtin is None:
        raise RuntimeError("built-in dynamic entry does not match the request")
    builtin_idx, dyn_version = builtin
    return {
        "dynSeed": dyn_seed,
        "dynVersion": dyn_version,
        "seedState": seed_state,
        "nonce": nonce,
        "nowSeconds": now_seconds,
        "f26Tail": [
            {"algorithm": alg, "outputHex": out_b.hex()}
            for idx, (alg, out_b) in enumerate(entries)
            if idx != builtin_idx
        ],
    }


def extract_persona_from_argus(
    argus: str,
    url: str | None = None,
    *,
    body: bytes = b"",
) -> dict[str, Any]:
    """Extract device persona fields (and optional dynamic seed/f26Tail) from X-Argus."""
    decoded = _decode_argus_fields(argus)
    persona: dict[str, Any] = {}
    for key in ("appId", "licenseId", "appVersion", "sdkVersion", "sdkVersionCode"):
        if key in decoded:
            persona[key] = decoded[key]

    dev_token = decoded.get("deviceToken")
    if isinstance(dev_token, str) and dev_token.startswith("{"):
        try:
            dev_info = json.loads(dev_token)
        except json.JSONDecodeError:
            dev_info = {}
        if isinstance(dev_info, dict):
            mapping = {
                "device_id": "deviceId",
                "install_id": "installId",
                "channel": "channel",
                "app_version": "appVersion",
                "os_version": "release",
                "device_type": "model",
                "device_brand": "deviceBrand",
                "resolution": "resolution",
                "dpi": "dpi",
                "rom": "build",
                "host_abi": "hostAbi",
            }
            for src_key, dst_key in mapping.items():
                if dev_info.get(src_key) not in (None, ""):
                    persona[dst_key] = dev_info[src_key]

    if url and decoded.get("dynSeed") is not None:
        dyn = extract_dynamic_fields(argus, url, body=body)
        persona["dynSeed"] = dyn["dynSeed"]
        persona["dynVersion"] = dyn["dynVersion"]
        persona["seedState"] = dyn["seedState"]
        if dyn["f26Tail"]:
            persona["f26Tail"] = dyn["f26Tail"]

    return persona
