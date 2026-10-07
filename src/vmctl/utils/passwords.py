"""Hash interactive credentials in process without exposing them to host commands."""

import re

from passlib.hash import sha512_crypt
from pydantic import SecretStr

from vmctl.errors import VmctlError


def hash_desktop_password(password: str) -> SecretStr:
    if not password or "\x00" in password or len(password.encode("utf-8")) > 4096:
        raise VmctlError("Desktop password must be nonempty, contain no NUL, and be <=4096 bytes")
    # Passlib's stubs omit the signature of its documented using() factory.
    hasher = sha512_crypt.using(rounds=500_000)  # type: ignore[no-untyped-call]
    return SecretStr(hasher.hash(password))


def validate_password_hash(value: SecretStr) -> str:
    hashed = value.get_secret_value()
    if not re.fullmatch(r"\$6\$rounds=[0-9]+\$[./a-zA-Z0-9]{1,16}\$[./a-zA-Z0-9]{86}", hashed):
        raise VmctlError(
            "Desktop password must be a SHA-512 crypt hash, created by the password helper"
        )
    return hashed
