"""LocalVolumeStore: round-trip, idempotent delete, traversal guard, lazy dir."""
from __future__ import annotations

import pytest

from app.services.contract_store import (
    ContractStoreError,
    LocalVolumeStore,
    get_contract_store,
)


def test_put_get_exists_roundtrip(tmp_path):
    store = LocalVolumeStore(str(tmp_path / "contracts"))
    key = "org-1/abc.pdf"
    assert store.exists(key) is False
    store.put(key, b"%PDF-1.4 hello")
    assert store.exists(key) is True
    assert store.get(key) == b"%PDF-1.4 hello"


def test_does_not_create_dir_until_first_put(tmp_path):
    base = tmp_path / "contracts"
    LocalVolumeStore(str(base))           # construction must not touch the fs
    assert not base.exists()              # boot-safe when the volume is absent


def test_delete_is_idempotent(tmp_path):
    store = LocalVolumeStore(str(tmp_path))
    store.put("k.pdf", b"x")
    store.delete("k.pdf")
    store.delete("k.pdf")                 # second delete must not raise
    assert store.exists("k.pdf") is False


def test_get_missing_raises_store_error(tmp_path):
    store = LocalVolumeStore(str(tmp_path))
    with pytest.raises(ContractStoreError):
        store.get("nope.pdf")


def test_traversal_key_rejected(tmp_path):
    store = LocalVolumeStore(str(tmp_path / "contracts"))
    with pytest.raises(ValueError):
        store.put("../escape.pdf", b"x")


def test_factory_returns_local_by_default(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "contract_storage_backend", "local")
    assert isinstance(get_contract_store(), LocalVolumeStore)


def test_factory_unknown_backend_raises(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "contract_storage_backend", "quantum")
    with pytest.raises(ValueError):
        get_contract_store()
