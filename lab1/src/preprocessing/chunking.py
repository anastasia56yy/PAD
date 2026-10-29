"""Chunking strategies compared in the experiments.

fixed      - windows of `chunk_size` characters, no overlap
overlap    - the same windows, each starting `chunk_size - chunk_overlap` after the previous one
paragraph  - whole paragraphs packed greedily up to `chunk_size`
heading    - split by "#" headings first, long sections then packed by paragraph;
             every chunk is prefixed with its document title and section path
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,4}) (.+)$", re.MULTILINE)


@dataclass
class Chunk:
    text: str
    index: int
    section: str = ""
    metadata: dict = field(default_factory=dict)


def _cut_at_boundary(text: str, size: int) -> int:
    """Index to cut `text` near `size`, preferring a sentence or word boundary."""
    if len(text) <= size:
        return len(text)
    window = text[:size]
    for sep in ("\n", ". ", "; ", " "):
        pos = window.rfind(sep)
        if pos > size * 0.6:
            return pos + len(sep)
    return size


def fixed_chunks(text: str, size: int, overlap: int = 0) -> list[str]:
    if overlap >= size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    chunks, start = [], 0
    while start < len(text):
        end = start + _cut_at_boundary(text[start:], size)
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def paragraph_chunks(text: str, size: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n(?=- )|\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(para) > size:  # a single huge paragraph still has to be split
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(fixed_chunks(para, size))
        elif len(current) + len(para) + 1 <= size:
            current = f"{current}\n{para}" if current else para
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(section_path, body)] where section_path is e.g. "Admitere > Acte necesare"."""
    sections: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    last_end, current_path = 0, ""
    for match in HEADING_RE.finditer(text):
        body = text[last_end:match.start()].strip()
        if body:
            sections.append((current_path, body))
        level, title = len(match.group(1)), match.group(2).strip()
        path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
        current_path = " > ".join(t for _, t in path)
        last_end = match.end()
    tail = text[last_end:].strip()
    if tail:
        sections.append((current_path, tail))
    return sections


def heading_chunks(text: str, size: int, title: str = "") -> list[tuple[str, str]]:
    out = []
    for section, body in split_sections(text):
        prefix = " | ".join(p for p in (title, section) if p)
        budget = max(size - len(prefix) - 2, size // 2)
        for piece in paragraph_chunks(body, budget):
            out.append((section, f"{prefix}\n{piece}" if prefix else piece))
    return out


def chunk_document(text: str, title: str, strategy: str, chunk_size: int, chunk_overlap: int,
                   min_chunk_chars: int = 0) -> list[Chunk]:
    if strategy == "fixed":
        pieces = [("", t) for t in fixed_chunks(text, chunk_size)]
    elif strategy == "overlap":
        pieces = [("", t) for t in fixed_chunks(text, chunk_size, chunk_overlap)]
    elif strategy == "paragraph":
        pieces = [("", t) for t in paragraph_chunks(text, chunk_size)]
    elif strategy == "heading":
        pieces = heading_chunks(text, chunk_size, title)
    else:
        raise ValueError(f"Unknown chunking strategy: {strategy}")

    # strip the markdown heading markers from what goes to the embedding model
    cleaned = [(sec, re.sub(r"^#{1,4} ", "", t, flags=re.MULTILINE)) for sec, t in pieces]
    kept = [(sec, t) for sec, t in cleaned if len(t) >= min_chunk_chars]
    return [Chunk(text=t, index=i, section=sec) for i, (sec, t) in enumerate(kept)]
