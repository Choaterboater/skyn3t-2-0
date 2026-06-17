"""High-level RAG API: ingest -> chunk -> embed -> query -> answer.

Ties together the document processor, embedder, vector store, hybrid
retriever, and repo map. Answering uses the LLM client when configured and
falls back to a deterministic extractive answer (top chunks) otherwise, so
``answer`` ALWAYS returns something useful offline (design rule #6).

Import has zero side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from skyn3t.rag.document_processor import DocumentProcessor
from skyn3t.rag.embeddings import Embedder
from skyn3t.rag.repo_map import RepoMapIndex, build_repo_map, get_repo_map
from skyn3t.rag.retrieval import HybridRetriever
from skyn3t.rag.vector_store import SearchHit, VectorStore


@dataclass
class RagAnswer:
    query: str
    answer: str
    sources: List[SearchHit] = field(default_factory=list)
    backend: str = "extractive"  # "llm" | "extractive"
    model: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "query": self.query,
            "answer": self.answer,
            "backend": self.backend,
            "model": self.model,
            "sources": [
                {"id": s.id, "score": round(s.score, 5), "text": s.text[:240]}
                for s in self.sources
            ],
        }


class RagEngine:
    """End-to-end retrieval-augmented generation engine.

    Parameters
    ----------
    persist_path:
        Optional directory for the vector store (used only with chromadb).
    alpha:
        Hybrid weight (1.0 semantic-only, 0.0 lexical-only).
    llm_client:
        Optional object exposing ``async complete(prompt, ...) -> result``
        with a ``.text`` and ``.model`` attribute (e.g. skyn3t LLMClient).
    """

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        collection: str = "skyn3t_rag",
        max_tokens_per_chunk: int = 400,
        alpha: float = 0.5,
        embedder: Optional[Embedder] = None,
        llm_client: Optional[object] = None,
        prefer_chroma: bool = True,
    ) -> None:
        self.processor = DocumentProcessor(max_tokens=max_tokens_per_chunk)
        self.embedder = embedder or Embedder()
        self.store = VectorStore(
            collection=collection, persist_path=persist_path, prefer_chroma=prefer_chroma
        )
        self.retriever = HybridRetriever(
            embedder=self.embedder, vector_store=self.store, alpha=alpha
        )
        self.llm_client = llm_client
        self._ingested = 0

    # -- ingestion ---------------------------------------------------------
    def ingest_text(
        self,
        text: str,
        source: str = "",
        kind: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> int:
        chunks = self.processor.process(text, source=source, kind=kind)
        if not chunks:
            return 0
        ids = [c.id for c in chunks]
        texts = [c.text for c in chunks]
        metas = []
        base = metadata or {}
        for c in chunks:
            m = dict(base)
            m.update(
                {
                    "source": c.source,
                    "kind": c.kind,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "chunk_index": c.index,
                }
            )
            metas.append(m)
        n = self.retriever.add_documents(ids, texts, metas)
        self._ingested += n
        return n

    def ingest_file(self, path: str) -> int:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return 0
        return self.ingest_text(text, source=str(p))

    def ingest_directory(
        self,
        directory: str,
        extensions: Optional[Sequence[str]] = None,
        max_files: int = 1000,
    ) -> int:
        root = Path(directory)
        if not root.exists():
            return 0
        exts = set(e.lower() for e in extensions) if extensions else None
        total = 0
        count = 0
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in {".git", "__pycache__", "node_modules", ".venv"} for part in p.parts):
                continue
            if exts is not None and p.suffix.lower() not in exts:
                continue
            total += self.ingest_file(str(p))
            count += 1
            if count >= max_files:
                break
        return total

    # -- retrieval ---------------------------------------------------------
    def query(self, question: str, top_k: int = 5) -> List[SearchHit]:
        return self.retriever.search(question, top_k=top_k)

    # -- answering ---------------------------------------------------------
    async def answer(
        self,
        question: str,
        top_k: int = 5,
        max_tokens: int = 512,
    ) -> RagAnswer:
        hits = self.query(question, top_k=top_k)
        context = self._format_context(hits)
        if self.llm_client is not None and hits:
            llm_answer = await self._llm_answer(question, context, max_tokens)
            if llm_answer is not None:
                return llm_answer
        return RagAnswer(
            query=question,
            answer=self._extractive_answer(question, hits),
            sources=hits,
            backend="extractive",
        )

    def _format_context(self, hits: Sequence[SearchHit]) -> str:
        blocks = []
        for i, h in enumerate(hits, start=1):
            src = h.metadata.get("source", "") if h.metadata else ""
            head = f"[{i}] {src}".strip()
            blocks.append(f"{head}\n{h.text}")
        return "\n\n".join(blocks)

    def _extractive_answer(self, question: str, hits: Sequence[SearchHit]) -> str:
        if not hits:
            return "No relevant context was found for this query."
        top = hits[0]
        lines = [
            "Based on the retrieved context, the most relevant passage is:",
            "",
            top.text.strip(),
        ]
        if len(hits) > 1:
            lines.append("")
            lines.append(f"({len(hits)} passages retrieved; showing the top match.)")
        return "\n".join(lines)

    async def _llm_answer(
        self, question: str, context: str, max_tokens: int
    ) -> Optional[RagAnswer]:
        prompt = (
            "Answer the question using ONLY the context below. If the context "
            "is insufficient, say so explicitly.\n\n"
            f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
        )
        try:
            result = await self.llm_client.complete(  # type: ignore[attr-defined]
                prompt, max_tokens=max_tokens
            )
        except Exception:
            return None
        text = getattr(result, "text", None)
        if not text:
            return None
        return RagAnswer(
            query=question,
            answer=text,
            sources=list(self.query(question)),
            backend="llm",
            model=getattr(result, "model", ""),
        )

    # -- repo map convenience ---------------------------------------------
    def repo_map(self, directory: str, max_tokens: int = 2000) -> str:
        return get_repo_map(directory, max_tokens=max_tokens)

    def build_repo_index(self, directory: str) -> RepoMapIndex:
        idx = RepoMapIndex(directory)
        idx.scan()
        return idx

    @property
    def document_count(self) -> int:
        return self.retriever.count()

    def info(self) -> Dict[str, object]:
        return {
            "embedder_backend": self.embedder.backend,
            "vector_backend": self.store.backend,
            "bm25_backend": self.retriever.bm25_backend,
            "documents": self.document_count,
            "ingested": self._ingested,
            "llm": self.llm_client is not None,
        }


__all__ = ["RagEngine", "RagAnswer", "build_repo_map", "get_repo_map"]
