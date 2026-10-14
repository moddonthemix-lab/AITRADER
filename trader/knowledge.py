"""The Strat documents Trader trades by and refers to.

Two places, read together:
    docs/strat/                         the playbook that ships with Trader (forked from AMO's Strat rules)
    ~/Documents/Trader Strat Docs/      (TRADER_STRAT_DOCS_DIR) the boss's own Strat files: PDFs, Word docs,
                                        notes, screenshots' text… drop them in and Trader reads them.

Searched by keywords (no extra AI model needed). The best pieces go into Trader's prompts when he writes
a thesis, reviews a trade or answers a question — so his explanations come from the documents.
The trading decisions themselves are the rules in strat.py / risk.py, which follow the same playbook.
"""

from __future__ import annotations

import math
import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".rtf"}
DOC_EXT = TEXT_EXT | {".pdf", ".docx"}
CHUNK, OVERLAP = 900, 120
MAX_FILE_BYTES = 20_000_000
BUILTIN = PROJECT_ROOT / "docs" / "strat"
WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
STOP = {"the", "and", "for", "that", "with", "this", "are", "you", "your", "from", "when", "what", "how", "its",
        "was", "but", "not", "have", "has", "will", "can", "into", "than", "then", "they", "them", "there"}


def folders() -> list[Path]:
    return [BUILTIN, Path(settings.strat_docs_dir).expanduser()]


def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        try:
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages[:300])
        except Exception:  # noqa: BLE001 — a broken / encrypted PDF
            return ""
    if ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            return ""
        return unescape(re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml)))
    raw = path.read_text(errors="replace")
    if ext in (".html", ".htm"):
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = unescape(re.sub(r"<[^>]+>", " ", raw))
    if ext == ".rtf":
        raw = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", raw)
    return raw


def chunks(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + CHUNK)
        if end < len(text):  # end on a paragraph / sentence break if there's one nearby
            cut = max(text.rfind("\n\n", i + CHUNK // 2, end), text.rfind(". ", i + CHUNK // 2, end))
            end = cut + 1 if cut > 0 else end
        out.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - OVERLAP, i + 1)
    return [c for c in out if len(c) > 20]


def _words(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if len(w) > 1 and w not in STOP]


class Library:
    """All the Strat documents, split into pieces, with a small keyword index (re-read when files change)."""

    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = dirs or folders()
        self.pieces: list[dict[str, Any]] = []
        self._stamp: tuple = ()

    def files(self) -> list[tuple[Path, Path]]:
        out = []
        for root in self.dirs:
            if root.is_dir():
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix.lower() in DOC_EXT and not p.name.startswith((".", "~$")) \
                            and p.stat().st_size <= MAX_FILE_BYTES:
                        out.append((root, p))
        return out

    def refresh(self) -> int:
        files = self.files()
        stamp = tuple((str(p), p.stat().st_mtime) for _, p in files)
        if stamp == self._stamp:
            return len(self.pieces)
        self.pieces = []
        for root, p in files:
            for i, text in enumerate(chunks(read_text(p))):
                words = _words(text)
                self.pieces.append({"file": p.relative_to(root).as_posix(), "i": i, "text": text,
                                    "words": words, "set": set(words)})
        self._stamp = stamp
        return len(self.pieces)

    def search(self, query: str, k: int = 3) -> list[dict[str, Any]]:
        self.refresh()
        q = set(_words(query))
        if not q or not self.pieces:
            return []
        n = len(self.pieces)
        df = {w: sum(1 for p in self.pieces if w in p["set"]) for w in q}
        scored = []
        for p in self.pieces:
            s = sum(math.log(1 + n / df[w]) * (1 + math.log(p["words"].count(w))) for w in q if w in p["set"])
            if s > 0:
                scored.append((s / math.sqrt(len(p["words"]) + 20), p))
        scored.sort(key=lambda x: -x[0])
        return [{"file": p["file"], "text": p["text"], "score": round(s, 3)} for s, p in scored[:k]]

    def context(self, query: str, k: int = 3, limit: int = 2400) -> str:
        """The best-matching pieces, labeled with their file, for a prompt."""
        out, used = [], 0
        for h in self.search(query, k):
            piece = f"[{h['file']}] {h['text']}"
            if used + len(piece) > limit:
                piece = piece[:max(0, limit - used)]
            out.append(piece)
            used += len(piece)
            if used >= limit:
                break
        return "\n---\n".join(out)

    def summary(self) -> dict[str, Any]:
        self.refresh()
        files = sorted({p["file"] for p in self.pieces})
        return {"files": files, "pieces": len(self.pieces), "folders": [str(d) for d in self.dirs]}


_library: Library | None = None


def library() -> Library:
    global _library
    if _library is None:
        _library = Library()
    return _library
