"""TikTok 47.7.2 request signing, TicketGuard, TTEncrypt, session state, and wreq transport."""
from __future__ import annotations

from .crypto import (
    VERSION,
    DynamicFieldsDict,
    DynTailDict,
    SignOutputDict,
    decrypt_argus,
    dyn_encode,
    encrypt_argus,
    gorgon,
    ladon,
    sign_request,
    sm3,
)
from .dynamic import extract_dynamic_fields, extract_persona_from_argus
from .profiles import (
    DEFAULT_DEVICE_ID,
    DEFAULT_INSTALL_ID,
    build_common_params,
    build_registration_header,
    build_user_agent,
    default_kr_env_overrides,
    default_kr_persona,
    with_common_params,
)
from .registration import DEFAULT_REGISTRATION_URL, DeviceRegistrationClient, RegistrationResponseError
from .session_state import (
    DEFAULT_COOKIE_ORIGINS,
    TikTokSessionState,
    dump_cookie_jar,
    restore_cookie_jar,
    token_sha256,
)
from .signer import MetaSecSigner
from .signup import SignupClient, SignupError, mix_decode, mix_encode
from .ticket_guard import TicketGuardState, p256_keypair, p256_sign_base64
from .transport import WREQ_AVAILABLE, HttpResponse, SignedSession, TransportError
from .ttencrypt import tt_decrypt, tt_decrypt_gzip, tt_encrypt, tt_encrypt_gzip

__all__ = [
    "DEFAULT_COOKIE_ORIGINS",
    "DEFAULT_DEVICE_ID",
    "DEFAULT_INSTALL_ID",
    "DEFAULT_REGISTRATION_URL",
    "DeviceRegistrationClient",
    "DynTailDict",
    "DynamicFieldsDict",
    "HttpResponse",
    "MetaSecSigner",
    "RegistrationResponseError",
    "SignOutputDict",
    "SignedSession",
    "SignupClient",
    "SignupError",
    "TicketGuardState",
    "TikTokSessionState",
    "TransportError",
    "VERSION",
    "WREQ_AVAILABLE",
    "build_common_params",
    "build_registration_header",
    "build_user_agent",
    "decrypt_argus",
    "default_kr_env_overrides",
    "default_kr_persona",
    "dump_cookie_jar",
    "dyn_encode",
    "encrypt_argus",
    "extract_dynamic_fields",
    "extract_persona_from_argus",
    "gorgon",
    "ladon",
    "mix_decode",
    "mix_encode",
    "p256_keypair",
    "p256_sign_base64",
    "restore_cookie_jar",
    "sign_request",
    "sm3",
    "token_sha256",
    "tt_decrypt",
    "tt_decrypt_gzip",
    "tt_encrypt",
    "tt_encrypt_gzip",
    "with_common_params",
]
