"""Portable OpenSSH public-key loading and validation."""

import base64
import binascii
import struct
from pathlib import Path

from vmctl.errors import VmctlError


def read_public_key(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VmctlError(f"Cannot read SSH public key {path}: {exc}") from exc
    return validate_public_key(text)


def validate_public_key(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or "PRIVATE KEY" in text:
        raise VmctlError("An OpenSSH public key is required; private keys are never accepted")
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or fields[0] not in {
            "ssh-ed25519",
            "ssh-rsa",
            "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384",
            "ecdsa-sha2-nistp521",
            "sk-ssh-ed25519@openssh.com",
            "sk-ecdsa-sha2-nistp256@openssh.com",
        }:
            raise VmctlError("SSH key file must contain plain OpenSSH public keys")
        try:
            blob = base64.b64decode(fields[1], validate=True)
            length = struct.unpack(">I", blob[:4])[0]
            if blob[4 : 4 + length].decode() != fields[0] or len(blob) <= 4 + length:
                raise ValueError("Invalid key data")
        except (ValueError, binascii.Error, struct.error, UnicodeError) as exc:
            raise VmctlError("Invalid SSH public key encoding") from exc
    return "\n".join(lines) + "\n"
