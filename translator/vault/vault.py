"""
vault/vault.py — Encrypted local storage for the provider tree.

Crypto primitives are reused verbatim from v3.6 (PBKDF2-HMAC-SHA256 +
SHA-256 keystream XOR + HMAC-SHA256 tag) — adequate for a local personal
tool, and the user did not ask for a third-party crypto dependency.

What changed vs v3.6 is only the plaintext schema: instead of a flat
{provider_key: [keys]} dict, v4 stores the whole Provider -> Connection ->
Model tree (02_DATA_MODELS.md §4). No migration path from v3.6 — the file
path is deliberately different so v3.6's keys.enc is never touched.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os

from .. import config


class VaultError(Exception):
    """Raised for unrecoverable vault I/O problems (not for wrong passwords)."""


# ── Crypto primitives (ported as-is from v3.6) ────────────────────────────────

def _pbkdf2(password: str, salt: bytes, iterations: int = 310_000, dklen: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen)


def _ctr_crypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """AES-CTR emulation via SHA-256 keystream blocks (XOR cipher)."""
    out, counter = bytearray(), 0
    for i in range(0, len(data), 32):
        keystream = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(b ^ k for b, k in zip(data[i:i + 32], keystream))
        counter += 1
    return bytes(out)


# ── Low-level file I/O ────────────────────────────────────────────────────────

_MAGIC_LEN = len(config.VAULT_MAGIC)


def _write(data: dict, password: str, path: str) -> None:
    plaintext = json.dumps(data, ensure_ascii=False).encode()
    salt = os.urandom(16)
    nonce = os.urandom(16)
    dk = _pbkdf2(password, salt)
    ciphertext = _ctr_crypt(dk, nonce, plaintext)
    tag = _hmac.new(dk, ciphertext, hashlib.sha256).digest()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(config.VAULT_MAGIC + salt + nonce + tag + ciphertext)
    os.replace(tmp, path)


def _read(password: str, path: str) -> dict | None:
    """dict on success; None on wrong password / corrupt / missing file."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw[:_MAGIC_LEN] != config.VAULT_MAGIC:
            return None
        body = raw[_MAGIC_LEN:]
        salt, nonce, tag, ciphertext = body[:16], body[16:32], body[32:64], body[64:]
        dk = _pbkdf2(password, salt)
        if not _hmac.compare_digest(_hmac.new(dk, ciphertext, hashlib.sha256).digest(), tag):
            return None
        return json.loads(_ctr_crypt(dk, nonce, ciphertext).decode())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def empty_payload() -> dict:
    return {"version": config.VAULT_SCHEMA_VERSION, "providers": []}


# ── Public API ────────────────────────────────────────────────────────────────

class Vault:
    """
    Thin persistence layer over the encrypted file. Holds no business logic —
    the registry owns the Provider objects, this owns bytes on disk.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or config.VAULT_FILE

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self, password: str) -> dict | None:
        """
        Returns the plaintext payload dict, or None if the password is wrong
        or the file is corrupt. A missing file returns a fresh empty payload
        so first-run setup needs no special case.
        """
        if not self.exists():
            return empty_payload()
        data = _read(password, self.path)
        if data is None:
            return None
        if "providers" not in data:
            data["providers"] = []
        data.setdefault("version", config.VAULT_SCHEMA_VERSION)
        return data

    def save(self, payload: dict, password: str) -> None:
        payload.setdefault("version", config.VAULT_SCHEMA_VERSION)
        _write(payload, password, self.path)

    def verify_password(self, password: str) -> bool:
        return not self.exists() or _read(password, self.path) is not None

    def change_password(self, old_password: str, new_password: str) -> bool:
        data = self.load(old_password)
        if data is None:
            return False
        self.save(data, new_password)
        return True

    def destroy(self) -> bool:
        """
        Delete the vault file outright. The only escape from a forgotten
        password — the contents are unrecoverable either way.
        """
        if not self.exists():
            return False
        os.remove(self.path)
        return True
