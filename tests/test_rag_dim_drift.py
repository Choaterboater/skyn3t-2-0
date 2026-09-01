# tests/test_rag_dim_drift.py
"""Embedder backend drift (sentence-transformers 384-dim vs hashing-fallback
256-dim) silently made a persisted chroma store unreachable: the collection is
pinned at first add, every mismatched upsert/query raised, writes were
diverted to process-local memory (lost on restart) and recall became [] with
nothing but a generic warning. VectorStore must discover the store's pinned
dim (sidecar marker, then peek), route drifted dims to a dim-suffixed sibling
collection so writes persist and queries answer, and log the drift loudly —
while transient chroma failures keep their per-call memory fallback."""
from __future__ import annotations

import json
from pathlib import Path

import skyn3t.rag.vector_store as vector_store_module
from skyn3t.rag.vector_store import VectorStore


class _FakeCollection:
    """Chroma double that enforces dimensionality pinning like chroma."""

    def __init__(self, name: str = "skyn3t_rag") -> None:
        self.name = name
        self.dim: int | None = None  # pinned at first add, like chroma
        self.docs: dict[str, tuple[str, list[float]]] = {}

    def _check(self, vec) -> None:
        if self.dim is not None and len(vec) != self.dim:
            raise RuntimeError(
                f"Embedding dimension {len(vec)} does not match "
                f"collection dimensionality {self.dim}"
            )

    def upsert(self, ids, documents, embeddings, metadatas):
        for e in embeddings:
            self._check(e)
            if self.dim is None:
                self.dim = len(e)
        for i, _id in enumerate(ids):
            self.docs[_id] = (documents[i], list(embeddings[i]))

    def query(self, query_embeddings, n_results):
        self._check(query_embeddings[0])
        ids = list(self.docs.keys())[:n_results]
        return {
            "ids": [ids],
            "documents": [[self.docs[i][0] for i in ids]],
            "metadatas": [[{} for _ in ids]],
            "distances": [[0.0 for _ in ids]],
        }

    def count(self):
        return len(self.docs)

    def peek(self, n=1):
        embs = [emb for (_text, emb) in list(self.docs.values())[:n]]
        return {"embeddings": embs}


class _NoPeekCollection(_FakeCollection):
    """Older-chroma shape: pin exists but cannot be discovered up front."""

    def count(self):
        raise RuntimeError("count not supported")

    def peek(self, n=1):
        raise RuntimeError("peek not supported")


class _FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, name, metadata=None):
        col = self.collections.get(name)
        if col is None:
            col = _FakeCollection(name=name)
            self.collections[name] = col
        return col

    def delete_collection(self, name):
        self.collections.pop(name, None)


class _FakeChromadb:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def PersistentClient(self, path):
        return self._client

    def Client(self):
        return self._client


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[str] = []

    def warning(self, event, **kwargs):
        self.events.append(event)

    def debug(self, event, **kwargs):
        self.events.append(event)


def _wire_chroma(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(vector_store_module, "_CHROMA_AVAILABLE", True)
    monkeypatch.setattr(vector_store_module, "chromadb", _FakeChromadb(client))


def _store(tmp_path: Path) -> VectorStore:
    return VectorStore(collection="skyn3t_rag", persist_path=tmp_path)


# -- drift routing ----------------------------------------------------------
def test_drifted_add_persists_to_suffixed_collection_not_memory(
    monkeypatch, tmp_path: Path
):
    client = _FakeClient()
    base = client.get_or_create_collection("skyn3t_rag")
    base.upsert(["old"], ["old doc"], [[0.5, 0.5, 0.5, 0.5]], [{}])  # pinned at 4
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    n = vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    assert n == 1
    # The batch persisted in the dim-suffixed sibling, not process memory.
    assert "n1" in client.collections["skyn3t_rag_d6"].docs
    assert vs._docs == {}
    assert vs.backend == "chromadb"
    assert vs.dim_drift == {
        "store_dim": 4,
        "embedder_dim": 6,
        "active_collection": "skyn3t_rag_d6",
    }


def test_drifted_query_returns_hits_instead_of_empty(monkeypatch, tmp_path: Path):
    client = _FakeClient()
    base = client.get_or_create_collection("skyn3t_rag")
    base.upsert(["old"], ["old doc"], [[0.5, 0.5, 0.5, 0.5]], [{}])
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    hits = vs.query([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert [h.id for h in hits] == ["n1"]


def test_dim_drift_is_logged(monkeypatch, tmp_path: Path):
    rec = _RecordingLog()
    client = _FakeClient()
    base = client.get_or_create_collection("skyn3t_rag")
    base.upsert(["old"], ["old doc"], [[0.5, 0.5, 0.5, 0.5]], [{}])
    _wire_chroma(monkeypatch, client)
    monkeypatch.setattr(vector_store_module, "log", rec)
    vs = _store(tmp_path)
    vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    assert "vector_store.embedder_dim_drift" in rec.events


def test_dim_mismatch_rescue_when_pin_is_undiscoverable(monkeypatch, tmp_path: Path):
    # No marker and peek/count unavailable: the mismatch is only detectable
    # from chroma's rejection message, which must route to the sibling
    # instead of being treated as a transient failure.
    client = _FakeClient()
    base = _NoPeekCollection(name="skyn3t_rag")
    base.upsert(["old"], ["old doc"], [[0.5, 0.5, 0.5, 0.5]], [{}])
    client.collections["skyn3t_rag"] = base
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    assert "n1" in client.collections["skyn3t_rag_d6"].docs
    assert vs._docs == {}
    hits = vs.query([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert [h.id for h in hits] == ["n1"]


def test_transient_failure_does_not_create_drift_collections(
    monkeypatch, tmp_path: Path
):
    class _LockedCollection(_FakeCollection):
        def upsert(self, ids, documents, embeddings, metadatas):
            raise RuntimeError("database is locked")

    client = _FakeClient()
    client.collections["skyn3t_rag"] = _LockedCollection(name="skyn3t_rag")
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    vs.add(["a"], ["alpha"], [[1.0, 0.0]])
    # Memory fallback for the batch, no sibling collection, no drift state.
    assert "a" in vs._docs
    assert list(client.collections) == ["skyn3t_rag"]
    assert vs.dim_drift is None


# -- sidecar marker ---------------------------------------------------------
def test_marker_written_after_first_persisted_add(monkeypatch, tmp_path: Path):
    _wire_chroma(monkeypatch, _FakeClient())
    vs = _store(tmp_path)
    vs.add(["a"], ["alpha"], [[1.0, 0.0, 0.0, 0.0]])
    data = json.loads((tmp_path / "embedder.json").read_text(encoding="utf-8"))
    assert data["collection"] == "skyn3t_rag"
    assert data["dim"] == 4


def test_marker_is_write_once(monkeypatch, tmp_path: Path):
    marker = tmp_path / "embedder.json"
    marker.write_text(
        json.dumps({"collection": "skyn3t_rag", "dim": 4, "sentinel": True}),
        encoding="utf-8",
    )
    _wire_chroma(monkeypatch, _FakeClient())
    vs = _store(tmp_path)
    vs.add(["a"], ["alpha"], [[1.0, 0.0, 0.0, 0.0]])
    assert json.loads(marker.read_text(encoding="utf-8"))["sentinel"] is True


def test_marker_pins_store_dim_before_any_add(monkeypatch, tmp_path: Path):
    # A restarted process must learn the pin from the marker even before
    # touching the (possibly cold) collection: the first drifted write goes
    # straight to the sibling instead of bouncing off chroma.
    (tmp_path / "embedder.json").write_text(
        json.dumps({"collection": "skyn3t_rag", "dim": 4}), encoding="utf-8"
    )
    client = _FakeClient()
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    assert "n1" in client.collections["skyn3t_rag_d6"].docs
    assert client.collections["skyn3t_rag"].docs == {}


def test_reset_clears_drift_state_and_stale_marker(monkeypatch, tmp_path: Path):
    client = _FakeClient()
    base = client.get_or_create_collection("skyn3t_rag")
    base.upsert(["old"], ["old doc"], [[0.5, 0.5, 0.5, 0.5]], [{}])
    _wire_chroma(monkeypatch, client)
    vs = _store(tmp_path)
    vs.add(["n1"], ["new doc"], [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
    vs.reset()
    assert not (tmp_path / "embedder.json").exists()
    assert vs.dim_drift is None
    assert "skyn3t_rag_d6" not in client.collections
    assert "skyn3t_rag" not in client.collections
