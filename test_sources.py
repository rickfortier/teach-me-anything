"""Sources: reading websites and files (Markdown, Word, PDF), and writing the course from them."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import course_builder as cb
import sources

FILLER = " Our data covers U.S. equity options with full Greeks for every contract." * 4
SITE = {
    "https://shop.example.com/robots.txt": "User-agent: *\nDisallow: /secret/\n",
    "https://shop.example.com/": f"""<html><head><title>Example Data &amp; Co</title><script>var x = 1;</script></head>
        <body><nav><a href="/pricing/">Pricing</a> <a href="/secret/plan/">Secret</a> <a href="/cart/">Cart</a></nav>
        <h1>Options data</h1><p>Since 2002.{FILLER}</p>
        <a href="https://other.com/page">elsewhere</a> <a href="/data.csv">sample</a> <a href="/search?q=x">search</a>
        <a href="/pricing/#plans">plans</a><footer>Copyright</footer></body></html>""",
    "https://shop.example.com/pricing/": f"<title>Pricing</title><p>Level 2 costs $615 for five years.{FILLER}</p>",
}


def fake_session():
    session = Mock(headers={})
    asked = []

    def get(url, timeout=None):
        asked.append(url)
        resp = Mock(url=url, text=SITE.get(url, ""), status_code=200 if url in SITE else 404,
                    headers={"content-type": "text/plain" if url.endswith(".txt") else "text/html; charset=utf-8"})
        return resp
    session.get = get
    return session, asked


class ReadingTests(unittest.TestCase):
    def test_pages_on_the_same_site_are_read_once(self):
        session, asked = fake_session()
        pages = sources.collect(["shop.example.com"], session=session)
        self.assertEqual([p["url"] for p in pages], ["https://shop.example.com/", "https://shop.example.com/pricing/"])
        self.assertEqual(pages[0]["title"], "Example Data & Co")
        self.assertIn("Since 2002.", pages[0]["text"])
        for left_out in ("var x", "Copyright", "Pricing Secret"):          # scripts, footer, menus
            self.assertNotIn(left_out, pages[0]["text"])
        visited = set(asked)
        for skipped in ("https://shop.example.com/secret/plan/", "https://shop.example.com/cart/",
                        "https://other.com/page", "https://shop.example.com/data.csv",
                        "https://shop.example.com/search?q=x"):
            self.assertNotIn(skipped, visited)                               # robots.txt, cart, off-site, files, queries
        self.assertEqual(asked.count("https://shop.example.com/pricing/"), 1)

    def test_bad_addresses_and_unreadable_sites(self):
        self.assertEqual(sources.check_urls([" example.com/a#top ", "", "https://example.com/a"]),
                         ["https://example.com/a"])
        for bad in (["ftp://example.com"], ["notaweb"], ["a.com", "b.com", "c.com", "d.com", "e.com", "f.com"]):
            with self.assertRaises(ValueError):
                sources.check_urls(bad)
        session, _ = fake_session()
        with self.assertRaisesRegex(sources.SourceError, "could be read.*HTTP 404"):
            sources.collect(["https://missing.example.org/"], session=session)

    def test_material_is_cut_to_the_limit(self):
        pages = [{"url": f"https://x.com/{i}", "title": f"P{i}", "text": "y" * 1000} for i in range(10)]
        text = sources.material(pages, limit=2500)
        self.assertTrue(text.startswith(sources.SOURCE_RULE))
        self.assertIn("URL: https://x.com/1", text)
        self.assertNotIn("URL: https://x.com/3", text)


class CourseFromSourcesTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        quiet = patch("course_builder.emit")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_prompts_carry_the_pages_and_a_retry_does_not_read_again(self):
        from test_course_builder import FakeLLM, FakeYT
        llm = FakeLLM()
        pages = [{"url": "https://shop.example.com/pricing/", "title": "Pricing", "text": "Level 2 costs $615."}]
        with patch("sources.collect", return_value=pages) as collect:
            builder = cb.CourseBuilder("Support for Example Data", "beyond_basics", "mix", {"workers": 1}, llm, FakeYT,
                                       base=self.base, now=datetime(2026, 10, 16, 9),
                                       sources=["shop.example.com"])
            folder = builder.build()
            course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
            self.assertEqual(course["sources"], {"urls": ["https://shop.example.com/"], "files": [],
                                                 "file": "sources.json", "pages": 1, "skipped": []})
            for label in ("course plan", "day 1 lesson", "day 1 quiz", "day 1 walkthrough"):
                self.assertIn("Level 2 costs $615.", llm.prompts[label], label)
                self.assertIn("must come from these sources", llm.prompts[label], label)
            self.assertNotIn("$615", llm.prompts.get("day 1 videos", ""))   # video picking doesn't need them
            cb.CourseBuilder.resume(folder.name, {"workers": 1}, llm, FakeYT, base=self.base).build()
        self.assertEqual(collect.call_count, 1)

    def test_course_without_sources_is_unchanged(self):
        from test_course_builder import FakeLLM
        llm = FakeLLM()
        builder = cb.CourseBuilder("Wash dishes", "pure_beginner", "docs", {"workers": 1}, llm, base=self.base,
                                   now=datetime(2026, 10, 16, 9))
        folder = builder.build()
        self.assertIsNone(json.loads((folder / "course.json").read_text(encoding="utf-8"))["sources"])
        self.assertNotIn("must come from these sources", llm.prompts["day 1 lesson"])


def make_pdf(path: Path, pages: list[str]) -> None:
    """A minimal PDF with one line of text per page (or none, like a scan)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def make_docx(path: Path) -> None:
    import zipfile
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (f'<w:document {w}><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Refund policy</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Refunds are given within 14 days </w:t></w:r><w:r><w:t>of purchase.</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Level 2</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>$615</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_markdown_word_and_pdf_are_read(self):
        (self.dir / "faq.md").write_text("# FAQ\n\nQ: Which format?   A: CSV files, one per day.\n" * 3, encoding="utf-8")
        make_docx(self.dir / "policy.docx")
        make_pdf(self.dir / "guide.pdf", ["Install the data loader first and check every file.",
                                           "Level 3 adds the implied volatility surface for each day."])
        md = sources.read_file(self.dir / "faq.md")
        self.assertIn("A: CSV files, one per day.", md["text"])
        doc = sources.read_file(self.dir / "policy.docx")
        self.assertEqual(doc["text"], "# Refund policy\nRefunds are given within 14 days of purchase.\nLevel 2 | $615")
        pdf = sources.read_file(self.dir / "guide.pdf")
        self.assertEqual((pdf["url"], pdf["file"]), (None, "guide.pdf"))
        self.assertIn("[Page 2]\nLevel 3 adds the implied volatility surface", pdf["text"])
        chunks = sources._chunks([pdf])
        self.assertEqual([c["where"] for c in chunks], ["guide.pdf, page 1", "guide.pdf, page 2"])
        self.assertIn("File: guide.pdf, page 2", sources.material([pdf]))

    def test_unreadable_files_say_why(self):
        make_pdf(self.dir / "scan.pdf", ["", ""])
        with self.assertRaisesRegex(sources.SourceError, "scan.pdf has no readable text.*scanned"):
            sources.read_file(self.dir / "scan.pdf")
        (self.dir / "broken.docx").write_bytes(b"not a zip")
        with self.assertRaisesRegex(sources.SourceError, "broken.docx couldn't be read"):
            sources.read_file(self.dir / "broken.docx")
        (self.dir / "sheet.xlsx").write_bytes(b"x")
        for bad in ([str(self.dir / "sheet.xlsx")], [str(self.dir / "missing.pdf")]):
            with self.assertRaises(ValueError):
                sources.check_files(bad)

    def test_long_sources_send_the_parts_that_match_the_day(self):
        manual = {"url": None, "file": "manual.txt", "title": "manual.txt", "text": "\n".join(
            [f"Section {i}: general notes about accounts and billing. " * 20 for i in range(30)]
            + ["Refunds: ask within 14 days; refunds go back to the original card. " * 10]
            + [f"Section {i}: shipping and storage notes. " * 25 for i in range(30, 60)])}
        site = {"url": "https://x.com/", "title": "Home", "text": "Welcome to X Data. " * 40}
        text = sources.material([site, manual], limit=6_000, query="Handling refund requests from customers")
        self.assertIn("Refunds: ask within 14 days", text)
        self.assertIn("Welcome to X Data.", text)                       # every source's opening is kept
        self.assertNotIn("Section 45:", text)
        self.assertLess(len(text), 6_000 + len(sources.SOURCE_RULE) + 500)
        everything = sources.material([site], limit=60_000, query="anything")
        self.assertEqual(everything.count("Welcome to X Data."), 40)    # short sources go whole


class CourseFromFilesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        quiet = patch("course_builder.emit")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_files_are_copied_read_and_bad_ones_skipped(self):
        from test_course_builder import FakeLLM
        (self.dir / "faq.md").write_text("Support hours are 9 to 5 Eastern, Monday to Friday. " * 3, encoding="utf-8")
        make_pdf(self.dir / "scan.pdf", [""])
        llm = FakeLLM()
        builder = cb.CourseBuilder("Support training", "beyond_basics", "docs", {"workers": 1}, llm,
                                   base=self.dir / "courses", now=datetime(2026, 10, 16, 9),
                                   files=[str(self.dir / "faq.md"), str(self.dir / "scan.pdf")])
        (self.dir / "faq.md").unlink()                                   # the course keeps its own copy
        folder = builder.build()
        info = json.loads((folder / "course.json").read_text(encoding="utf-8"))["sources"]
        self.assertEqual((info["files"], info["pages"]), (["faq.md", "scan.pdf"], 1))
        self.assertIn("scan.pdf has no readable text", info["skipped"][0])
        self.assertTrue((folder / "sources" / "faq.md").is_file())
        self.assertIn("Support hours are 9 to 5 Eastern", llm.prompts["day 1 lesson"])
        self.assertIn("File: faq.md", llm.prompts["day 1 lesson"])


if __name__ == "__main__":
    unittest.main()
