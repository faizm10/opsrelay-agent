"""Encrypts connector credentials at rest (Fernet, key in OPSRELAY_SECRET_KEY).

Both storage backends seal these fields on write and open them on read, so the rest of the API
only sees plaintext. A credential sealed with an earlier key reads as missing, so after a key rotation
the workspace reconnects instead of every page failing. Values without the prefix are pre-encryption plaintext; `python -m uptime
seal-secrets` rewrites them. The agent worker opens GitHub tokens with the same key and format.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("uptime.sealing")

SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "github": ("token",),
    "vercel": ("token",),
    "sentry": ("client_secret",),
    "notify": ("url", "secret", "routing_key"),
    "openai": ("key",),
}
PREFIX = "enc:v1:"
# Local development and tests only; deployments must set their own key (see require_key).
_DEV_KEY = "b3BzcmVsYXktZGV2LWtleS1ub3QtYS1zZWNyZXQtMzI="  # base64("opsrelay-dev-key-not-a-secret-32")


def require_key() -> None:
    """Deployments refuse to start without a key rather than fall back to the public dev key."""
    deployed = os.environ.get("VERCEL_ENV") or os.environ.get("OPSRELAY_ENV") == "production"
    if deployed and not os.environ.get("OPSRELAY_SECRET_KEY"):
        raise RuntimeError("OPSRELAY_SECRET_KEY is required to store connector credentials")


@lru_cache(maxsize=4)
def _fernet(key: str) -> Fernet:
    return Fernet(key.encode())


def _box() -> Fernet:
    return _fernet(os.environ.get("OPSRELAY_SECRET_KEY") or _DEV_KEY)


def seal(kind: str, doc: dict) -> dict:
    fields = [f for f in SECRET_FIELDS.get(kind, ()) if isinstance(doc.get(f), str) and not doc[f].startswith(PREFIX)]
    return {**doc, **{f: PREFIX + _box().encrypt(doc[f].encode()).decode() for f in fields}} if fields else doc


def unseal(kind: str, doc: dict | None) -> dict | None:
    if not doc:
        return doc
    fields = [f for f in SECRET_FIELDS.get(kind, ()) if isinstance(doc.get(f), str) and doc[f].startswith(PREFIX)]
    try:
        return {**doc, **{f: _box().decrypt(doc[f][len(PREFIX):].encode()).decode() for f in fields}} if fields else doc
    except InvalidToken:
        log.warning("%s credentials of workspace %s were sealed with another OPSRELAY_SECRET_KEY; treating them as not set",
                    kind, doc.get("workspace_id", "?"))
        return None


def reseal_all(storage) -> int:
    """Re-save every workspace's credentials so plaintext from before encryption is sealed. Idempotent."""
    saved = 0
    for ws in storage.all_docs("workspaces"):
        for kind in ("github", "vercel", "sentry", "openai"):
            if doc := storage.get_doc(kind, ws["id"]):
                storage.put_doc(kind, ws["id"], doc)
                saved += 1
        for doc in storage.find_docs("notify", "workspace_id", ws["id"]):
            storage.put_doc("notify", f"{ws['id']}:{doc['kind']}", doc)
            saved += 1
    return saved
