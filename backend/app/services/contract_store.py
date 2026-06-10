"""Pluggable blob storage for contract documents.

The contract repository keeps the actual PDF bytes behind this interface so the
*where* is swappable without touching the API or service layer. Today the only
backend is a local filesystem directory (a mounted Railway volume in prod); a
future client's SAP / S3 / other document store is a drop-in implementation of
the same ``ContractStore`` Protocol, selected by ``settings.contract_storage_backend``
— the same flag-gated seam pattern as the forecast engine.

Design notes:
  - keys are SERVER-generated (``<org_id>/<uuid>.pdf``), never the user filename,
    so there is no user-controlled path. ``_resolve`` additionally asserts the
    resolved path stays under the base dir (defence in depth against traversal).
  - ``LocalVolumeStore`` does NOT create or stat its dir at construction, so app
    boot never fails when the volume is absent; the directory is created on first
    ``put``. On prod with an ephemeral filesystem this means uploads are lost on
    redeploy — ``config.announce_startup`` logs a loud warning for that case, and
    provisioning a persistent volume is a gated deploy step.
  - ``delete`` is idempotent (a missing object is not an error).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import settings


class ContractStoreError(RuntimeError):
    """A blob the store was asked for does not exist (or could not be read)."""


class ContractStore(Protocol):
    """The storage seam. Implement these four to add a backend (S3, SAP, …)."""

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalVolumeStore:
    """Filesystem-backed store rooted at ``base_dir`` (a mounted volume in prod)."""

    def __init__(self, base_dir: str) -> None:
        self._dir = Path(base_dir)

    def _resolve(self, key: str) -> Path:
        # Join under the base dir and confirm we never escape it. Keys are
        # server-generated, but this guards against any future caller mistake.
        base = self._dir.resolve()
        target = (base / key).resolve()
        if base != target and base not in target.parents:
            raise ValueError(f"unsafe storage key {key!r}")
        return target

    def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)  # create-on-demand
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ContractStoreError(f"contract blob {key!r} not found") from exc

    def delete(self, key: str) -> None:
        # Idempotent: a missing blob is fine (covers "bytes already gone").
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


def get_contract_store() -> ContractStore:
    """Return the configured store. The seam where future backends plug in."""
    backend = settings.contract_storage_backend
    if backend == "local":
        return LocalVolumeStore(settings.contract_storage_dir)
    raise ValueError(
        f"unknown contract_storage_backend {backend!r}; expected 'local' "
        f"(future: 's3' / 'sap')"
    )
