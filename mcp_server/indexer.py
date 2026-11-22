"""Chunking + BM25 full-text index for the tuition policy document.

Indexing happens ONCE (at image build time via `python indexer.py`, or at server start if the index file
is missing) -- never per question.

Chunking strategy
-----------------
The policy is a numbered Markdown document. Every `### N.M Title` subsection becomes one chunk, so each
search hit maps to a citable section reference such as "Section 3.2 - Installment Structure". A `## N.`
section that has no subsections becomes a single chunk. The parent section title is kept in the chunk
metadata and is indexed with extra weight (title boost).

Ranking
-------
Okapi BM25 (k1=1.5, b=0.75) over lower-cased, stop-word-filtered, lightly stemmed tokens, with a small
domain synonym map ("instalment" -> "installment", "due" -> "deadline", ...).
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parent
DOC_PATH = BASE / "data" / "University_Tuition_Policies.md"
INDEX_PATH = BASE / "data" / "index.json"
SOURCE_NAME = "University_Tuition_Policies.md"

STOPWORDS = set("""a an and are as at be by can do does for from has have how i if in into is it its may
me my of on or our so than that the their them then there these they this to was we what when where which
who will with you your should would could about any all also am been being per
rule rules policy policies university""".split())

SYNONYMS = {
    "instalment": "installment", "instalments": "installment", "installments": "installment",
    "due": "deadline", "deadlines": "deadline", "last": "deadline",
    "penalty": "late", "penalties": "late", "fine": "late", "overdue": "late",
    "reimbursement": "refund", "refunds": "refund", "withdraw": "withdrawal",
    "iban": "iban", "transfer": "transfer", "wire": "transfer", "eft": "transfer", "swift": "transfer",
    "discount": "discount", "grant": "scholarship", "scholarships": "scholarship",
    "tuition": "tuition", "fees": "fee", "pay": "payment", "paying": "payment", "paid": "payment",
    "payments": "payment",
}


def _stem(tok: str) -> str:
    for suf in ("ings", "ing", "ies", "es", "ed", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if t in STOPWORDS:
            continue
        t = SYNONYMS.get(t, t)
        out.append(_stem(t))
    return out


@dataclass
class Chunk:
    id: str             # e.g. "3.2"
    title: str          # e.g. "Installment Structure"
    parent: str         # e.g. "3. Installment Plans"
    text: str           # markdown body of the subsection

    @property
    def reference(self) -> str:
        return f"Section {self.id} - {self.title}"


def chunk_markdown(md: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    parent, cur_id, cur_title, buf = "", None, None, []
    parent_id, parent_title, parent_buf = None, None, []

    def flush_sub():
        nonlocal buf
        if cur_id and "".join(buf).strip():
            chunks.append(Chunk(cur_id, cur_title, parent, "\n".join(buf).strip()))
        buf = []

    def flush_parent_if_no_subs():
        if parent_id and not any(c.id.startswith(parent_id + ".") for c in chunks) and "".join(parent_buf).strip():
            chunks.append(Chunk(parent_id, parent_title, parent, "\n".join(parent_buf).strip()))

    for line in md.splitlines():
        m2 = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        m3 = re.match(r"^###\s+(\d+\.\d+)\s+(.*)$", line)
        if m2:
            flush_sub()
            flush_parent_if_no_subs()
            parent_id, parent_title, parent_buf = m2.group(1), m2.group(2).strip(), []
            parent = f"{parent_id}. {parent_title}"
            cur_id = cur_title = None
        elif m3:
            flush_sub()
            cur_id, cur_title = m3.group(1), m3.group(2).strip()
        elif cur_id:
            buf.append(line)
        elif parent_id:
            parent_buf.append(line)
    flush_sub()
    flush_parent_if_no_subs()
    return chunks


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75, title_boost: int = 2):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.document_id = SOURCE_NAME
        # headings carry most of the meaning in short policy sections -> repeat them (heading boost)
        self.docs = [tokenize(f"{(c.title + ' ') * title_boost}{(c.parent + ' ') * title_boost}{c.text}")
                     for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        n = len(self.docs)
        df = Counter(t for d in self.docs for t in set(d))
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scores = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if s > 0:
                scores.append((self.chunks[i], s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    # --- persistence: the index is built once and stored as JSON ---
    def to_json(self) -> dict:
        return {"source": SOURCE_NAME, "document_id": self.document_id, "chunk_count": len(self.chunks), "avgdl": self.avgdl,
                "chunks": [asdict(c) for c in self.chunks]}

    @classmethod
    def from_json(cls, data: dict) -> "BM25Index":
        idx = cls([Chunk(**c) for c in data["chunks"]])
        idx.document_id = data.get("document_id", SOURCE_NAME)
        return idx


def document_id(md: str) -> str:
    m = re.search(r"\*\*Document ID:\*\*\s*([A-Za-z0-9._-]+)", md)
    return m.group(1) if m else SOURCE_NAME


def build_index(doc_path: Path = DOC_PATH) -> BM25Index:
    md = doc_path.read_text(encoding="utf-8")
    idx = BM25Index(chunk_markdown(md))
    idx.document_id = document_id(md)
    return idx


def load_or_build(index_path: Path = INDEX_PATH, doc_path: Path = DOC_PATH) -> BM25Index:
    if index_path.exists() and index_path.stat().st_mtime >= doc_path.stat().st_mtime:
        return BM25Index.from_json(json.loads(index_path.read_text(encoding="utf-8")))
    idx = build_index(doc_path)
    index_path.write_text(json.dumps(idx.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    return idx


if __name__ == "__main__":
    idx = build_index()
    INDEX_PATH.write_text(json.dumps(idx.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Indexed {len(idx.chunks)} chunks from {SOURCE_NAME} -> {INDEX_PATH}")
    for c in idx.chunks:
        print(f"  {c.reference}  ({len(c.text)} chars)")
    if len(sys.argv) > 1:
        for chunk, score in idx.search(" ".join(sys.argv[1:])):
            print(f"{score:6.2f}  {chunk.reference}")
