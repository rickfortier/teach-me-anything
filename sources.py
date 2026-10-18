"""Sources: the websites and files a course is written from, e.g. your own business's
site plus its manuals and FAQs.

Websites: starting from each address given, the reader follows links on the same
site and keeps the readable text of up to `source_pages` pages. It honours
robots.txt and skips images, downloads and links with query strings.
Files: Markdown, text, Word (.docx) and PDF; PDFs keep their page numbers.

The course states facts about products, prices and policies only from these
sources and cites them. When there is more than fits in a prompt, each lesson
gets the parts that match its own day.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit

import requests

AGENT = "TeachMe/1.0 (builds a training course from pages you chose)"
MAX_START_URLS = 5
# Page furniture, not content. Links inside these are still followed.
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template", "button"}
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "tr", "br", "section", "article", "td", "th",
              "dt", "dd", "table", "ul", "ol"}
# Pages that are about the visitor, not the business.
PRIVATE = re.compile(r"/(cart|basket|checkout|my-?account|account|log-?in|sign-?in|sign-?up|register|wp-admin|wp-login)"
                     r"(/|\.|$)", re.I)
ASSET = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|json|xml|pdf|zip|gz|csv|xlsx?|docx?|mp4|mp3|woff2?)$", re.I)
SOURCE_RULE = """Sources: this course is written from the web pages and files below, from the learner's own organisation.
- Every fact about its products, services, prices, plans, data, policies and procedures must come from these sources. Never invent, estimate or round one. If the sources don't say, say that plainly and tell the reader where to check.
- General background the reader needs (concepts, terms, how things work in general) may come from your own knowledge, but must never contradict the sources.
- When you state a fact from a web page, link it in Markdown, e.g. [Pricing](https://example.com/pricing). When it comes from a file, name the file (and page) in brackets, e.g. (Product Guide.pdf, page 3).
- These sources may be only the parts that match today's lesson; don't assume something is missing from them just because it isn't shown here."""


class SourceError(Exception):
    friendly = True      # message is safe and readable as-is


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.parts, self.links = "", [], []
        self._skip, self._in_title = 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def check_urls(urls: list[str]) -> list[str]:
    """The addresses cleaned up (https:// added if missing); ValueError if one isn't a web address."""
    cleaned = []
    for url in urls:
        url = (url or "").strip()
        if not url:
            continue
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or "." not in (parts.hostname or ""):
            raise ValueError(f"Not a web address: {url}")
        if not parts.path:                                  # example.com -> example.com/
            url = parts._replace(path="/").geturl()
        cleaned.append(urldefrag(url)[0])
    if len(cleaned) > MAX_START_URLS:
        raise ValueError(f"At most {MAX_START_URLS} source websites")
    return list(dict.fromkeys(cleaned))


def _site(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _allowed(robots: dict, session: requests.Session, url: str) -> bool:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in robots:
        parser = robotparser.RobotFileParser()
        try:
            resp = session.get(base + "/robots.txt", timeout=10)
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except requests.RequestException:
            parser.parse([])
        robots[base] = parser
    return robots[base].can_fetch(AGENT, url)


def collect(urls: list[str], max_pages: int = 25, page_chars: int = 8_000, progress=None,
            session: requests.Session | None = None) -> list[dict]:
    """[{url, title, text}] for up to `max_pages` pages, starting pages first, then the
    pages they link to on the same sites. SourceError if none could be read."""
    session = session or requests.Session()
    session.headers["User-Agent"] = AGENT
    queue = check_urls(urls)
    sites = {_site(urlsplit(u).hostname) for u in queue}
    seen, pages, errors, robots = set(), [], [], {}
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or _site(parts.hostname) not in sites
                or ASSET.search(parts.path) or PRIVATE.search(parts.path)):
            continue
        if not _allowed(robots, session, url):
            errors.append(f"{url}: the site's robots.txt asks not to read it")
            continue
        try:
            resp = session.get(url, timeout=20)
        except requests.RequestException as exc:
            errors.append(f"{url}: {type(exc).__name__}")
            continue
        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue
        if "html" not in resp.headers.get("content-type", "").lower():
            continue
        final = urldefrag(resp.url or url)[0]
        if _site(urlsplit(final).hostname) not in sites:      # redirected off the site
            continue
        seen.add(final)
        if "charset" not in resp.headers.get("content-type", "").lower():
            resp.encoding = "utf-8"                # requests would guess Latin-1 and garble the text
        page = _Page()
        page.feed(resp.text)
        text = page.text()
        if len(text) >= 200 and not any(p["text"] == text[:page_chars] for p in pages):
            pages.append({"url": final, "title": " ".join(page.title.split()) or final, "text": text[:page_chars]})
            if progress:
                progress(f"Read {final}")
        for href in page.links:
            link = urldefrag(urljoin(final, href))[0]
            if link not in seen and not urlsplit(link).query:
                queue.append(link)
    if not pages:
        raise SourceError("None of the source websites could be read"
                          + (": " + "; ".join(errors[:3]) if errors else "."))
    return pages


# ------------------------------------------------------------------- files

FILE_TYPES = {".md", ".markdown", ".txt", ".docx", ".pdf"}
MAX_FILES = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def check_files(paths: list[str]) -> list[Path]:
    """The files as Paths; ValueError for a missing file, an unknown type or too many."""
    files = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"File not found: {raw}")
        if path.suffix.lower() not in FILE_TYPES:
            raise ValueError(f"{path.name}: only Markdown, text, Word (.docx) and PDF files can be used")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"{path.name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        files.append(path)
    if len(files) > MAX_FILES:
        raise ValueError(f"At most {MAX_FILES} source files")
    return files


def _docx_text(path: Path) -> str:
    """Paragraphs and tables of a Word file, in order; headings marked with #."""
    import zipfile
    from xml.etree import ElementTree
    with zipfile.ZipFile(path) as z:
        body = ElementTree.fromstring(z.read("word/document.xml")).find(f"{_W}body")
    lines = []

    def para(p):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t")).strip()
        style = p.find(f"{_W}pPr/{_W}pStyle")
        level = re.match(r"Heading(\d)", style.get(f"{_W}val", "")) if style is not None else None
        return ("#" * int(level.group(1)) + " " + text) if level and text else text

    for block in body if body is not None else []:
        if block.tag == f"{_W}p":
            lines.append(para(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(para(p) for p in cell.iter(f"{_W}p")).strip() for cell in row.iter(f"{_W}tc")]
                lines.append(" | ".join(cells))
    return "\n".join(line for line in lines if line.strip())


def _pdf_text(path: Path) -> str:
    """The text of each page, marked "[Page N]" so lessons can cite the page."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise SourceError(f"{path.name} is password-protected")
    pages = []
    for n, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[Page {n}]\n{text}")
    return "\n\n".join(pages)


def read_file(path: Path, name: str | None = None) -> dict:
    """{file, title, text} for one source file. SourceError when it has no readable text
    (a scanned PDF is only pictures of pages)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text = _pdf_text(path)
        elif suffix == ".docx":
            text = _docx_text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"{path.name} couldn't be read ({type(exc).__name__})")
    text = "\n".join(" ".join(line.split()) if not line.startswith("#") else line.strip()
                     for line in text.splitlines()).strip()
    if len(text) < 50:
        why = " (a scanned PDF has no text to read)" if suffix == ".pdf" else ""
        raise SourceError(f"{path.name} has no readable text{why}")
    return {"url": None, "file": name or path.name, "title": path.name, "text": text}


# --------------------------------------------------- choosing what to send

CHUNK_CHARS = 1_500
_STOP = set("""the and for with that this from your you are was were have has had not but all any can will
would should could into about over than then them they their there what when where which while who how its
our out use using used each more most also just only one two may must per via""".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text.lower()) if w not in _STOP]


def _chunks(pages: list[dict]) -> list[dict]:
    """Every source cut into pieces of about CHUNK_CHARS at line breaks, each knowing
    where it came from (the URL, or the file and PDF page)."""
    out = []
    for i, page in enumerate(pages):
        where_base = page.get("url") or page.get("file") or page["title"]
        buf, where, pdf_page = [], where_base, None
        for line in page["text"].splitlines():
            marker = re.fullmatch(r"\[Page (\d+)\]", line)
            if marker:
                pdf_page = marker.group(1)
            if buf and (sum(len(x) for x in buf) + len(line) > CHUNK_CHARS or marker):
                out.append({"source": i, "where": where, "text": "\n".join(buf)})
                buf = []
            if not buf:
                where = f"{where_base}, page {pdf_page}" if pdf_page else where_base
            if not marker:
                buf.append(line)
        if buf:
            out.append({"source": i, "where": where, "text": "\n".join(buf)})
    return out


def select(pages: list[dict], query: str, limit: int) -> list[dict]:
    """The chunks to send: everything when it fits; otherwise the opening of each
    source (so every source is known) and then the chunks that best match `query`."""
    chunks = _chunks(pages)
    if sum(len(c["text"]) for c in chunks) <= limit:
        return chunks
    import math
    docs = [set(_words(c["text"])) for c in chunks]
    terms = set(_words(query))
    idf = {t: math.log(1 + len(chunks) / (1 + sum(t in d for d in docs))) for t in terms}

    def score(i):
        words = _words(chunks[i]["text"])
        return sum(idf[w] for w in words if w in idf) / (1 + len(words)) ** 0.5

    chosen, used = set(), 0
    openings = [next(i for i, c in enumerate(chunks) if c["source"] == s) for s in sorted({c["source"] for c in chunks})]
    for i in openings:                                     # at most a third of the room for openings
        if used + len(chunks[i]["text"]) > limit / 3:
            break
        chosen.add(i)
        used += len(chunks[i]["text"])
    for i in sorted(range(len(chunks)), key=lambda i: -score(i)):
        if i in chosen or used + len(chunks[i]["text"]) > limit:
            continue
        chosen.add(i)
        used += len(chunks[i]["text"])
    return [chunks[i] for i in sorted(chosen)]


def material(pages: list[dict], limit: int = 60_000, query: str = "") -> str:
    """The sources as one block for a prompt: all of them if they fit in `limit`
    characters, else the parts that best match `query` (today's lesson)."""
    out, last = [], None
    for chunk in select(pages, query, limit):
        page = pages[chunk["source"]]
        if chunk["where"] != last:
            source = f"URL: {chunk['where']}" if page.get("url") else f"File: {chunk['where']}"
            out.append(f"### {page['title']}\n{source}")
            last = chunk["where"]
        out.append(chunk["text"])
    return SOURCE_RULE + "\n\n## The sources\n\n" + "\n\n".join(out)
