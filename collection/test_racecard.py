import io
import json
from unittest.mock import patch
import zlib

from django.test import SimpleTestCase, TestCase
from pypdf import PdfReader, PdfWriter

from collection.fetch import FetchError, MAX_PAGE, MAX_ROBOTS
from collection.models import Observation
from collection.racecard import (
    BASE_URL, HOST, _extract_pdf, _fetch_pdf, _validate_url, collect_racecard,
)


URL = BASE_URL + "20261104_starter_all_chi.pdf"


def fixture_pdf():
    """Make a small real, self-contained PDF with a text layer."""
    stream = b"BT /F1 12 Tf 72 720 Td (RACE 1 - TEST HORSE) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return data + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % start


def bilingual_pdf(texts):
    """Build compressed pages using an explicit Unicode character map."""
    chars = sorted(set("".join(texts)))
    cmap = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n"
            b"/CMapName /Test def\n/CMapType 2 def\n1 begincodespacerange\n"
            b"<0000> <FFFF>\nendcodespacerange\n%d beginbfchar\n" % len(chars))
    for char in chars:
        code = char.encode("utf-16-be").hex().upper().encode()
        cmap += b"<" + code + b"> <" + code + b">\n"
    cmap += b"endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend end"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % (6 + i * 2) for i in range(len(texts))), len(texts)),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Test /Encoding /Identity-H "
        b"/DescendantFonts [4 0 R] /ToUnicode 5 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Test "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
    ]
    for i, text in enumerate(texts):
        compressed = zlib.compress(b"BT /F1 12 Tf 72 720 Td <" +
                                   text.encode("utf-16-be").hex().encode() + b"> Tj ET")
        objects.extend([
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (7 + i * 2),
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" %
            (len(compressed), compressed),
        ])
    data = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    start = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return data + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, start)


class RacecardUrlTests(SimpleTestCase):
    def test_exact_whitelist(self):
        self.assertEqual(_validate_url(URL), "/racing/content/PDF/RaceCard/20261104_starter_all_chi.pdf")
        self.assertEqual(_validate_url(BASE_URL + "20261104_starter_all.pdf"),
                         "/racing/content/PDF/RaceCard/20261104_starter_all.pdf")
        for url in [
            URL + "?token=secret", URL + "#section", URL + "?", URL.replace("https:", "http:"),
            URL.replace(HOST, HOST + ".evil.example"), URL.replace(HOST, "user@" + HOST),
            URL.replace(HOST, HOST + ":443"), URL.replace("20261104", "20260230"),
            URL.replace("20261104", "%32" + "0261104"), BASE_URL + "../private.pdf",
            BASE_URL + "RaceForm.PDF", URL + "/",
        ]:
            with self.subTest(url=url), self.assertRaises(FetchError):
                _validate_url(url)

    def test_fetch_respects_robots_and_limits(self):
        class FakeFetcher:
            interval = 2
            calls = []

            def _request(self, host, path, limit):
                self.calls.append((host, path, limit))
                if path == "/robots.txt":
                    return "text/plain; charset=utf-8", b"User-agent: *\nCrawl-delay: 5\nDisallow: /private\n"
                return "application/pdf", fixture_pdf()

        fake = FakeFetcher()
        self.assertEqual(_fetch_pdf(fake, _validate_url(URL)), fixture_pdf())
        self.assertEqual(fake.interval, 5)
        self.assertEqual(fake.calls, [(HOST, "/robots.txt", MAX_ROBOTS),
                                      (HOST, _validate_url(URL), MAX_PAGE)])
        fake.calls = []
        with patch.object(fake, "_request", return_value=("text/plain", b"User-agent: *\nDisallow: /\n")):
            with self.assertRaises(FetchError):
                _fetch_pdf(fake, _validate_url(URL))

    def test_bad_content_type_and_magic(self):
        for response in [("text/html", b"%PDF-1.4"), ("application/pdf", b"not a pdf")]:
            with self.subTest(response=response):
                fake = type("Fetcher", (), {"interval": 2})()
                fake._request = lambda host, path, limit: (
                    ("text/plain", b"User-agent: *\nDisallow:\n")
                    if path == "/robots.txt" else response
                )
                with self.assertRaises(FetchError):
                    _fetch_pdf(fake, _validate_url(URL))

    def test_real_pdf_text_extraction(self):
        result = _extract_pdf(fixture_pdf())
        self.assertEqual(result["total_pages"], 1)
        self.assertIn("RACE 1 - TEST HORSE", result["pages"][0]["text"])
        self.assertFalse(result["text_truncated"])

    def test_compressed_bilingual_multipage_pdf(self):
        texts = ["第1場 TEST HORSE 馬匹一", "第2場 第二匹 HORSE"]
        result = _extract_pdf(bilingual_pdf(texts))
        self.assertEqual(result["total_pages"], 2)
        self.assertEqual([page["text"] for page in result["pages"]], texts)
        self.assertFalse(result["text_truncated"])

    def test_text_is_truncated_and_json_safe(self):
        result = _extract_pdf(bilingual_pdf(["馬" * 15000]))
        self.assertTrue(result["text_truncated"])
        self.assertLessEqual(len(json.dumps(result["pages"][0]["text"],
                                           ensure_ascii=False).encode("utf-8")) - 2, 30000)
        self.assertEqual(result["pages"][0]["text"], "馬" * 10000)

    def test_page_limit(self):
        result = _extract_pdf(bilingual_pdf(["RACE"] * 41))
        self.assertEqual(result["total_pages"], 41)
        self.assertEqual(len(result["pages"]), 40)

    def test_corrupt_and_encrypted_pdfs_fail(self):
        with self.assertRaises(ValueError):
            _extract_pdf(b"%PDF-1.4\ncorrupt")
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(fixture_pdf())))
        writer.encrypt("password")
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(ValueError):
            _extract_pdf(output.getvalue())


class RacecardStagingTests(TestCase):
    def test_authorization_and_invalid_url_are_fail_closed(self):
        with patch("collection.racecard.get_shared_fetcher") as fetcher:
            obs, _ = collect_racecard(URL)
            self.assertEqual(obs.error, Observation.Error.NOT_AUTHORIZED)
            self.assertEqual(collect_racecard(URL, terms_reviewed=1)[0].error,
                             Observation.Error.NOT_AUTHORIZED)
            bad, _ = collect_racecard(URL + "?token=secret")
            self.assertEqual(bad.source_url, BASE_URL)
            self.assertEqual(bad.error, Observation.Error.INVALID_RESPONSE)
            fetcher.assert_not_called()

    def test_pdf_fetch_holds_shared_origin_lock(self):
        class Lock:
            held = False

            def __enter__(self):
                self.held = True

            def __exit__(self, *args):
                self.held = False

        fake = type("Fetcher", (), {"lock": Lock()})()

        def fetch_pdf(fetcher, path):
            self.assertIs(fetcher, fake)
            self.assertTrue(fetcher.lock.held)
            return fixture_pdf()

        with patch("collection.racecard.get_shared_fetcher", return_value=fake) as shared, \
                patch("collection.racecard._fetch_pdf", side_effect=fetch_pdf):
            obs, _ = collect_racecard(URL, terms_reviewed=True)
        shared.assert_called_once_with("hkjc-racecard")
        self.assertFalse(fake.lock.held)
        self.assertEqual(obs.status, Observation.Status.SUCCESS)

    def test_stages_unverified_raw_text(self):
        result = {"pages": [{"page": 1, "text": "RACE 1 - TEST HORSE"}],
                  "total_pages": 2, "text_truncated": True}
        with patch("collection.racecard.importlib.util.find_spec", return_value=True), \
                patch("collection.racecard._fetch_pdf", return_value=fixture_pdf()), \
                patch("collection.racecard._extract_pdf", return_value=result):
            obs, created = collect_racecard(URL, terms_reviewed=True)
        self.assertTrue(created)
        self.assertEqual(obs.status, Observation.Status.SUCCESS)
        self.assertEqual(obs.payload["pages"], result["pages"])
        self.assertTrue(obs.payload["review_required"])
        self.assertEqual(obs.payload["extraction_level"], "raw_text_only")
        self.assertTrue(obs.payload["pages_truncated"])
        self.assertTrue(obs.payload["text_truncated"])
        self.assertIn("Structured meeting, race and runner fields have not been parsed.",
                      obs.payload["missing_information"])

    def test_real_bilingual_pdf_staged_without_field_guessing(self):
        pdf = bilingual_pdf(["第1場 TEST HORSE 馬匹一", "第2場 第二匹 HORSE"])
        with patch("collection.racecard._fetch_pdf", return_value=pdf):
            obs, _ = collect_racecard(URL, terms_reviewed=True)
        self.assertEqual(obs.status, Observation.Status.SUCCESS)
        self.assertEqual([p["text"] for p in obs.payload["pages"]],
                         ["第1場 TEST HORSE 馬匹一", "第2場 第二匹 HORSE"])
        self.assertEqual(obs.payload["extraction_level"], "raw_text_only")
        self.assertFalse(obs.payload["pages_truncated"])
        self.assertFalse(obs.payload["text_truncated"])

    def test_missing_parser_does_not_request(self):
        with patch("collection.racecard.importlib.util.find_spec", return_value=None), \
                patch("collection.racecard._fetch_pdf") as fetch:
            obs, _ = collect_racecard(URL, terms_reviewed=True)
            self.assertEqual(obs.error, Observation.Error.PARSE_ERROR)
            fetch.assert_not_called()

    def test_network_and_empty_text_failures(self):
        with patch("collection.racecard.importlib.util.find_spec", return_value=True):
            with patch("collection.racecard._fetch_pdf", side_effect=FetchError("DNS lookup failed.")):
                self.assertEqual(collect_racecard(URL, terms_reviewed=True)[0].error,
                                 Observation.Error.NETWORK_ERROR)
            with patch("collection.racecard._fetch_pdf", return_value=fixture_pdf()), \
                    patch("collection.racecard._extract_pdf", return_value={
                        "pages": [{"page": 1, "text": ""}], "total_pages": 1,
                        "text_truncated": False,
                    }):
                self.assertEqual(collect_racecard(URL, terms_reviewed=True)[0].error,
                                 Observation.Error.PARSE_ERROR)
