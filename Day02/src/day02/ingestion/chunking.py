"""Coordinate-preserving chunking. A table/paragraph is not split to satisfy a size target."""
from __future__ import annotations

import re
from dataclasses import dataclass

from day02.evidence import digest


@dataclass
class Chunk:
    chunk_id: str
    text: str
    start: int
    end: int


def fixed_chunks(text: str, size: int = 400, overlap: int = 80):
    if not 0 <= overlap < size:
        raise ValueError("0 <= overlap < size가 필요합니다.")
    return [Chunk(digest(f"{start}:{text[start:start+size]}")[:12], text[start:start+size],
                  start, min(start + size, len(text))) for start in range(0, len(text), size - overlap)]


def section_chunks(text: str):
    positions = sorted({0, len(text), *[m.start() for m in re.finditer(r"(?m)^#{1,3} ", text)]})
    return [Chunk(digest(f"{a}:{text[a:b]}")[:12], text[a:b], a, b)
            for a, b in zip(positions, positions[1:]) if text[a:b].strip()]


def bounded_chunks(text: str, target_chars: int = 3000):
    """Group complete sections; large atomic sections remain large and are reported downstream."""
    groups = []
    for section in section_chunks(text):
        if groups and section.end - groups[-1][0] <= target_chars:
            groups[-1] = (groups[-1][0], section.end)
        else:
            groups.append((section.start, section.end))
    return [Chunk(digest(f"{a}:{text[a:b]}")[:12], text[a:b], a, b) for a, b in groups]


def semantic_chunks(text: str, embed_documents, threshold: float = 0.72):
    import numpy as np
    paragraphs = list(re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text))
    if not paragraphs:
        return []
    vectors = np.asarray(embed_documents([p.group() for p in paragraphs]), dtype=float)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("0 벡터는 의미 경계에 사용할 수 없습니다.")
    vectors /= norms
    similarities = np.sum(vectors[:-1] * vectors[1:], axis=1)
    starts = [0, *[paragraphs[i + 1].start() for i, s in enumerate(similarities) if s < threshold], len(text)]
    return [Chunk(digest(f"{a}:{text[a:b]}")[:12], text[a:b], a, b) for a, b in zip(starts, starts[1:])]
