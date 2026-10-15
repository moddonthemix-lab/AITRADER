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
                                    "words": words, "set": set(words), "builtin": root == BUILTIN})
        self._stamp = stamp
        return len(self.pieces)

    def search(self, query: str, k: int = 3, boss_only: bool = False) -> list[dict[str, Any]]:
        """boss_only: only the boss's own documents and saved pages, not the built-in playbook."""
        self.refresh()
        q = set(_words(query))
        pieces = [p for p in self.pieces if not (boss_only and p.get("builtin"))]
        if not q or not pieces:
            return []
        n = len(pieces)
        df = {w: sum(1 for p in pieces if w in p["set"]) for w in q}
        scored = []
        for p in pieces:
            s = sum(math.log(1 + n / df[w]) * (1 + math.log(p["words"].count(w))) for w in q if w in p["set"])
            if s > 0:
                scored.append((s / math.sqrt(len(p["words"]) + 20), p))
        scored.sort(key=lambda x: -x[0])
        return [{"file": p["file"], "text": p["text"], "score": round(s, 3)} for s, p in scored[:k]]

    def context(self, query: str, k: int = 3, limit: int = 2400, boss_only: bool = False) -> str:
        """The best-matching pieces, labeled with their file, for a prompt."""
        out, used = [], 0
        for h in self.search(query, k, boss_only):
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


# ------------------------------------------------------------------ saving web pages (exact words)
THESTRAT_INDEX = "https://thestrat.ai/docs/the-3-universal-truths-that-govern-price/"
TRENDSPIDER_GUIDE = ("https://trendspider.com/learning-center/"
                     "a-comprehensive-guide-to-thestrat-trading-strategy-for-the-modern-trader/")
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                         "Version/17.0 Safari/605.1.15",
           "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}
# Site furniture that isn't part of a page's teaching (newsletter boxes, footers) — removed, nothing else is.
CHROME = [re.compile(r"Reading this the whole way through\?.*?Required \.", re.S),
          re.compile(r"Get the next one by email.*\Z", re.S),
          re.compile(r"^\s*TheStrat Docs\s*$", re.M)]


def fetch_html(url: str) -> str:
    import subprocess
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=BROWSER), timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — some sites refuse Python's client; macOS always has curl
        out = subprocess.run(["curl", "-sSL", "-m", "30", "-A", BROWSER["User-Agent"], url],
                             capture_output=True, text=True, timeout=40)
        if out.returncode != 0 or len(out.stdout) < 500:
            raise RuntimeError(f"couldn't download {url}")
        return out.stdout


def page_text(html: str) -> tuple[str, str]:
    """(title, the page's own words): the article body, paragraphs kept, markup and site furniture removed."""
    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = unescape(re.sub(r"\s+", " ", t.group(1))).strip() if t else ""
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", html) or re.search(r"(?is)<main[^>]*>(.*?)</main>", html)
    s = m.group(1) if m else html
    s = re.sub(r"(?is)<(script|style|nav|header|footer|svg|form|noscript)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h[1-6]>|</tr>|</blockquote>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    text = unescape(re.sub(r"<[^>]+>", " ", s))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    for pat in CHROME:
        text = pat.sub("", text)
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


def slug(url: str) -> str:
    from urllib.parse import urlparse

    u = urlparse(url)
    tail = [p for p in u.path.split("/") if p][-1:] or ["index"]
    return re.sub(r"[^a-z0-9-]+", "-", f"{tail[0]}".lower()).strip("-")[:80] or "page"


def save_page(url: str, folder: Path, subfolder: str | None = None) -> Path:
    """Save one web page into the Strat documents, word for word, with its source link on top."""
    from datetime import date
    from urllib.parse import urlparse

    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) links")
    title, text = page_text(fetch_html(url))
    if len(text) < 200:
        raise RuntimeError(f"no readable text on {url}")
    site = subfolder or (urlparse(url).hostname or "web").removeprefix("www.")
    dest = folder / site / f"{slug(url)}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"# {title}\n\nSource: {url}\nSaved {date.today().isoformat()} — the page's own words, "
                    f"unedited.\n\n{text}\n")
    return dest


def thestrat_links(index_html: str | None = None) -> list[str]:
    html = index_html if index_html is not None else fetch_html(THESTRAT_INDEX)
    links = set(re.findall(r'href="(https://thestrat\.ai/docs/[a-z0-9-]+/)"', html))
    links |= {"https://thestrat.ai" + p for p in re.findall(r'href="(/docs/[a-z0-9-]+/)"', html)}
    links.add(THESTRAT_INDEX)
    return sorted(links)


def save_thestrat(folder: Path, refresh: bool = False, pause: float = 0.6,
                  progress: Any = None) -> dict[str, Any]:
    """Every thestrat.ai docs page plus the TrendSpider guide, word for word (skips ones already saved)."""
    import time as _time

    saved, skipped, failed = [], [], []
    for url in thestrat_links() + [TRENDSPIDER_GUIDE]:
        site = "thestrat.ai" if "thestrat.ai" in url else "trendspider.com"
        if not refresh and (folder / site / f"{slug(url)}.md").exists():
            skipped.append(url)
            continue
        try:
            save_page(url, folder, site)
            saved.append(url)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{url} ({type(e).__name__})")
        if progress:
            progress(len(saved) + len(skipped) + len(failed))
        _time.sleep(pause)
    if _library is not None:
        _library.refresh()
    return {"saved": len(saved), "already_had": len(skipped), "failed": failed}
