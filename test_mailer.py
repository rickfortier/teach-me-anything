import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mailer

SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw"}


class FakeServer:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.calls = [("connect", host, port)]

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        FakeServer.sent.append((msg, self.calls))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSmtp:
    SMTP = FakeServer
    SMTP_SSL = FakeServer


class MailerTest(unittest.TestCase):
    def setUp(self):
        FakeServer.sent = []
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "course.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sends_pdf_attachment(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False):
            mailer.send_pdf("friend@example.org", self.pdf, "Wash dishes", smtp=FakeSmtp)
        msg, calls = FakeServer.sent[0]
        self.assertEqual(msg["To"], "friend@example.org")
        self.assertEqual(msg["From"], "me@example.com")
        self.assertIn("Wash dishes", msg["Subject"])
        att = next(msg.iter_attachments())
        self.assertEqual(att.get_filename(), "course.pdf")
        self.assertEqual(att.get_content(), b"%PDF-1.4 test")
        self.assertIn(("starttls",), calls)

    def test_not_configured(self):
        env = {k: "" for k in SMTP_ENV}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(mailer.NotConfigured):
                mailer.send_pdf("friend@example.org", self.pdf, "x", smtp=FakeSmtp)
        self.assertEqual(FakeServer.sent, [])

    def test_rejects_bad_address(self):
        with mock.patch.dict(os.environ, SMTP_ENV, clear=False):
            for bad in ("", "nobody", "a@b.com, c@d.com", "x@y.com\nBcc: z@w.com"):
                with self.assertRaises(ValueError):
                    mailer.send_pdf(bad, self.pdf, "x", smtp=FakeSmtp)


if __name__ == "__main__":
    unittest.main()
