"""Document chunking for text, markdown, and source code.

Pure-stdlib, zero side effects. Produces ``Chunk`` records suitable for
embedding and retrieval. Code is chunked along top-level symbol boundaries
when possible (so functions/classes stay intact), text/markdown along
paragraph/heading boundaries, with a token-budget cap and overlap.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


# Rough token estimate: ~4 chars/token. Cheap and deterministic.
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c",
    ".h", ".cpp", ".hpp", ".cc", ".rb", ".php", ".cs", ".kt", ".swift",
    ".scala", ".sh", ".sql",
}
_MD_EXT = {".md", ".markdown", ".rst"}


def detect_kind(path: str | None, explicit: str | None = None) -> str:
    if explicit in {"text", "markdown", "code"}:
        return explicit  # type: ignore[return-value]
    if not path:
        return "text"
    ext = Path(path).suffix.lower()
    if ext in _CODE_EXT:
        return "code"
    if ext in _MD_EXT:
        return "markdown"
    return "text"


@dataclass
class Chunk:
    text: str
    index: int
    source: str = ""
    kind: str = "text"
    start_line: int = 0
    end_line: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def id(self) -> str:
        h = hashlib.blake2b(
            f"{self.source}::{self.index}::{self.text}".encode(),
            digest_size=12,
        ).hexdigest()
        return h

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_CODE_BOUNDARY_RE = re.compile(
    r"^\s*(?:def |class |async def |function |export |public |private |"
    r"func |fn |impl |interface |type |const |var |let )",
)


class DocumentProcessor:
    """Splits documents into overlapping, token-bounded chunks."""

    def __init__(self, max_tokens: int = 400, overlap_tokens: int = 40) -> None:
        self.max_tokens = max(32, int(max_tokens))
        self.overlap_tokens = max(0, min(int(overlap_tokens), self.max_tokens // 2))

    # -- public API --------------------------------------------------------
    def process(
        self,
        text: str,
        source: str = "",
        kind: str | None = None,
    ) -> list[Chunk]:
        kind = detect_kind(source, kind)
        if not text or not text.strip():
            return []
        if kind == "code":
            return self._chunk_code(text, source)
        if kind == "markdown":
            return self._chunk_markdown(text, source)
        return self._chunk_text(text, source, kind="text")

    def process_file(self, path: str) -> list[Chunk]:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []
        return self.process(text, source=str(p))

    # -- internals ---------------------------------------------------------
    def _emit(
        self,
        lines: list[str],
        start_line: int,
        source: str,
        kind: str,
        index: int,
    ) -> Chunk:
        body = "\n".join(lines)
        return Chunk(
            text=body,
            index=index,
            source=source,
            kind=kind,
            start_line=start_line,
            end_line=start_line + len(lines) - 1,
        )

    def _pack_lines(
        self, lines: list[str], source: str, kind: str, line_offset: int = 0
    ) -> list[Chunk]:
        """Greedily pack lines into token-bounded chunks with line overlap.

        ``line_offset`` is the number of file lines preceding ``lines`` so chunk
        start/end line numbers stay file-relative when a section/block is packed
        in isolation (else every section restarts at line 1)."""
        chunks: list[Chunk] = []
        cur: list[str] = []
        cur_start = 1 + line_offset
        cur_tokens = 0
        line_no = 0
        idx = 0
        # Pre-split any single line that alone exceeds the budget into
        # word-wrapped sub-lines so no chunk can blow the token cap.
        lines = self._wrap_long_lines(lines)
        for line_no, line in enumerate(lines, start=1 + line_offset):
            lt = estimate_tokens(line) + 1
            if cur and cur_tokens + lt > self.max_tokens:
                chunks.append(self._emit(cur, cur_start, source, kind, idx))
                idx += 1
                # overlap: keep trailing lines worth ~overlap_tokens
                keep: list[str] = []
                kt = 0
                for kept_line in reversed(cur):
                    lt2 = estimate_tokens(kept_line) + 1
                    if kt + lt2 > self.overlap_tokens:
                        break
                    keep.insert(0, kept_line)
                    kt += lt2
                cur = keep[:]
                cur_start = line_no - len(keep)
                cur_tokens = kt
            cur.append(line)
            cur_tokens += lt
        if cur and any(s.strip() for s in cur):
            chunks.append(self._emit(cur, cur_start, source, kind, idx))
        return chunks

    def _wrap_long_lines(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        for line in lines:
            if estimate_tokens(line) + 1 <= self.max_tokens:
                out.append(line)
                continue
            words = line.split(" ")
            cur: list[str] = []
            cur_tok = 0
            for w in words:
                wt = estimate_tokens(w) + 1
                if cur and cur_tok + wt > self.max_tokens:
                    out.append(" ".join(cur))
                    cur = []
                    cur_tok = 0
                cur.append(w)
                cur_tok += wt
            if cur:
                out.append(" ".join(cur))
        return out

    def _chunk_text(self, text: str, source: str, kind: str) -> list[Chunk]:
        return self._pack_lines(text.splitlines(), source, kind)

    def _chunk_markdown(self, text: str, source: str) -> list[Chunk]:
        # Split on headings into sections, then pack each section.
        lines = text.splitlines()
        sections: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            if _HEADING_RE.match(line) and current:
                sections.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            sections.append(current)

        chunks: list[Chunk] = []
        idx = 0
        offset = 0
        for sec in sections:
            for c in self._pack_lines(sec, source, "markdown", line_offset=offset):
                c.index = idx
                idx += 1
                chunks.append(c)
            offset += len(sec)
        return chunks

    def _chunk_code(self, text: str, source: str) -> list[Chunk]:
        # Split on top-level symbol boundaries (def/class/function/...).
        lines = text.splitlines()
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            is_boundary = bool(_CODE_BOUNDARY_RE.match(line)) and not line.startswith(
                (" ", "\t")
            )
            if is_boundary and current:
                blocks.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append(current)

        chunks: list[Chunk] = []
        idx = 0
        offset = 0
        for blk in blocks:
            # Large blocks still get packed under the token budget.
            for c in self._pack_lines(blk, source, "code", line_offset=offset):
                c.index = idx
                idx += 1
                chunks.append(c)
            offset += len(blk)
        return chunks


__all__ = ["Chunk", "DocumentProcessor", "detect_kind", "estimate_tokens"]
