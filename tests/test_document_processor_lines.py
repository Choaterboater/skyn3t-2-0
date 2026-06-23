# tests/test_document_processor_lines.py
"""Chunk line numbers must be file-relative. _chunk_markdown/_chunk_code packed
each section/block from line 1, so every chunk after the first reported start_line=1."""
from __future__ import annotations

from skyn3t.rag.document_processor import DocumentProcessor


def test_markdown_chunk_line_numbers_are_file_relative():
    doc = DocumentProcessor()
    md = "# First\nLine 1\n\n# Second\nLine 2"  # section 2 begins at file line 4
    chunks = doc._chunk_markdown(md, "src.md")
    starts = sorted(c.start_line for c in chunks)
    assert starts == [1, 4], starts  # not [1, 1]


def test_code_chunk_line_numbers_are_file_relative():
    doc = DocumentProcessor()
    code = "def f1():\n    pass\n\ndef f2():\n    pass"  # f2 begins at file line 4
    chunks = doc._chunk_code(code, "src.py")
    starts = sorted(c.start_line for c in chunks)
    assert starts == [1, 4], starts
