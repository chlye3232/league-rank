"""Pure-Python P-256 ECDSA and TikTok TicketGuard header signing."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)


def _p256_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        lam = (3 * x1 * x1 + _P256_A) * pow(2 * y1, -1, _P256_P) % _P256_P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P256_P) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return x3, y3


def _p256_mul(k: int, point: tuple[int, int] = _P256_G) -> tuple[int, int]:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while k:
        if k & 1:
            result = _p256_add(result, addend)
        addend = _p256_add(addend, addend)
        k >>= 1
    assert result is not None
    return result


def _der_int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
    return b"\x02" + bytes([len(raw)]) + raw


def p256_keypair(priv_hex: str | None = None) -> tuple[str, str]:
    """Generate or derive a P-256 keypair returning (private_hex, base64_uncompressed_pubkey)."""
    if priv_hex:
        d = int(priv_hex, 16)
    else:
        d = int.from_bytes(os.urandom(32), "big") % (_P256_N - 1) + 1
        priv_hex = f"{d:064x}"
    x, y = _p256_mul(d)
    pub_uncompressed = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return priv_hex, base64.b64encode(pub_uncompressed).decode("ascii")


def p256_sign_base64(priv_hex: str, message: bytes) -> str:
    """Sign message with ECDSA-SHA256 over P-256 and return Base64 DER signature."""
    d = int(priv_hex, 16)
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = int.from_bytes(os.urandom(32), "big") % (_P256_N - 1) + 1
        x1, _ = _p256_mul(k)
        r = x1 % _P256_N
        if r == 0:
            continue
        s = pow(k, -1, _P256_N) * (z + r * d) % _P256_N
        if s != 0:
            break
    body = _der_int(r) + _der_int(s)
    der = b"\x30" + bytes([len(body)]) + body
    return base64.b64encode(der).decode("ascii")


@dataclass
class TicketGuardState:
    """Holds per-session P-256 TicketGuard keys and server-issued ticket state."""

    private_key: str = ""
    public_key: str = ""
    ticket: str = ""
    ts_sign: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> TicketGuardState:
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            private_key=str(data.get("privateKey") or data.get("ticketGuardPrivateKey") or ""),
            public_key=str(data.get("publicKey") or data.get("ticketGuardPublicKey") or ""),
            ticket=str(data.get("ticket") or data.get("ticketGuardTicket") or ""),
            ts_sign=str(data.get("tsSign") or data.get("ticketGuardTsSign") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "privateKey": self.private_key,
            "publicKey": self.public_key,
            "ticket": self.ticket,
            "tsSign": self.ts_sign,
        }

    def ensure_keypair(self) -> tuple[str, str]:
        if not self.private_key or not self.public_key:
            self.private_key, self.public_key = p256_keypair(self.private_key or None)
        return self.private_key, self.public_key

    def build_headers(
        self,
        url_or_path: str,
        *,
        has_token: bool,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        priv_hex, pub_b64 = self.ensure_keypair()
        headers = {
            "tt-ticket-guard-public-key": pub_b64,
            "tt-ticket-guard-version": "3",
            "tt-ticket-guard-iteration-version": "0",
        }
        if has_token and self.ticket:
            clean_path = urlsplit(url_or_path).path or url_or_path
            ts = int(time.time()) if timestamp is None else int(timestamp)
            to_sign = f"ticket={self.ticket}&path={clean_path}&timestamp={ts}".encode("utf-8")
            req_sign = p256_sign_base64(priv_hex, to_sign)
            client_data = {
                "req_content": "ticket,path,timestamp",
                "timestamp": ts,
                "ts_sign": self.ts_sign,
                "req_sign": req_sign,
            }
            headers["tt-ticket-guard-client-data"] = base64.b64encode(
                json.dumps(client_data, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
        return headers

    def capture_server_data(self, response_headers: Mapping[str, str]) -> bool:
        raw_server_data = ""
        for name, value in response_headers.items():
            if name.lower() == "tt-ticket-guard-server-data":
                raw_server_data = str(value or "")
                break
        if not raw_server_data:
            return False
        try:
            decoded = json.loads(base64.b64decode(raw_server_data).decode("utf-8"))
            tickets = decoded.get("tickets") if isinstance(decoded, dict) else None
            if isinstance(tickets, list) and tickets:
                item = tickets[0]
                if isinstance(item, dict) and item.get("ticket"):
                    self.ticket = str(item["ticket"])
                    raw_ts_sign = str(item.get("ts_sign") or "")
                    ts_sign_ree = raw_ts_sign
                    if raw_ts_sign.startswith("{"):
                        parsed_ts = json.loads(raw_ts_sign)
                        if isinstance(parsed_ts, dict) and parsed_ts.get("ts_sign_ree"):
                            ts_sign_ree = str(parsed_ts["ts_sign_ree"])
                    self.ts_sign = ts_sign_ree
                    return True
        except Exception:
            return False
        return False
