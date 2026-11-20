import threading
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from collection.fetch import FetchError, PublicFetcher, get_shared_fetcher, validate_url
from collection.models import Observation
from collection.resultsall import CollectionError, collect_results
from racing.models import RaceResult


BASE = "https://racing.hkjc.com/zh-hk/local/information/resultsall"
URL = BASE + "?racedate=2026/11/18&Racecourse=HV"

# Representative markup; live HKJC HTML was unavailable through the protected fetcher.
PAGE = """
<h2>第 1 場</h2>
<table><tr><th>名次</th><th>馬號</th><th>馬名</th></tr>
<tr><td>1</td><td>7</td><td>甲</td></tr>
<tr><td>2 DH</td><td>2</td><td>乙</td></tr>
<tr><td>2 DH</td><td>3</td><td>丙</td></tr>
<tr><td>退出</td><td>4</td><td>丁</td></tr></table>
<table><tr><th>彩池</th><th>勝出組合</th><th>派彩 (HK$)</th><th>投注額</th></tr>
<tr><td rowspan="2">位置</td><td>7</td><td>16.50</td><td>10</td></tr>
<tr><td>2</td><td>退款</td><td></td></tr></table>
<h2>第 2 場</h2><table><tr><th>名次</th><th>馬號</th></tr>
<tr><td>1</td><td>8</td></tr></table>
"""


class ResultsURLTests(SimpleTestCase):
    def test_allowed_urls_and_strict_rejections(self):
        for url in (BASE, URL, "https://racing.hkjc.com/en-us/local/information/resultsall"
                    "?RaceDate=2026%2F11%2F18&Racecourse=ST",
                    "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"):
            validate_url(url, "hkjc-resultsall")
        for url in (BASE + "?racedate=2026/02/30&Racecourse=ST",
                    BASE + "?racedate=2026/11/18&Racecourse=XX",
                    BASE + "?racedate=2026/11/18&Racecourse=ST&token=x",
                    BASE + "?racedate=2026/11/18&RaceDate=2026/11/18",
                    BASE + "?racedate=2026/11/18",
                    BASE + "?racedate=2026%2G11%2F18&Racecourse=ST",
                    BASE + "#fragment", BASE.replace("https:", "http:"),
                    BASE.replace("racing.hkjc.com", "racing.hkjc.com.evil.example"),
                    BASE.replace("racing.hkjc.com", "user@racing.hkjc.com"),
                    BASE.replace("racing.hkjc.com", "racing.hkjc.com:443"),
                    BASE + "/extra"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                validate_url(url, "hkjc-resultsall")

    def test_fetcher_checks_robots_for_exact_all_results_url(self):
        fetcher = PublicFetcher()
        with patch.object(fetcher, "_request", return_value=(
            "text/plain", b"User-agent: *\nDisallow: /zh-hk/local/information/resultsall\n"
        )) as request:
            with self.assertRaises(FetchError):
                fetcher.fetch("hkjc-resultsall", URL)
            self.assertEqual(request.call_count, 1)

    def test_same_origin_fetcher_serializes_html_and_custom_pdf_sequence(self):
        fetcher = get_shared_fetcher("hkjc-resultsall")
        self.assertIs(fetcher, get_shared_fetcher("hkjc-racecard"))
        started = threading.Event()
        release = threading.Event()
        pdf_started = threading.Event()
        pdf_requested = threading.Event()
        requests = []
        errors = []

        def request(host, path, limit):
            requests.append(path)
            if path == "/robots.txt":
                started.set()
                if not release.wait(2):
                    raise RuntimeError("test timed out")
                return "text/plain", b"User-agent: *\nDisallow:\n"
            if path == "/pdf":
                pdf_requested.set()
            return "text/html", b"<html></html>"

        def html_fetch():
            try:
                fetcher.fetch("hkjc-resultsall", URL)
            except Exception as exc:
                errors.append(exc)

        def pdf_fetch():
            try:
                pdf_started.set()
                with fetcher.lock:
                    fetcher._request("racing.hkjc.com", "/pdf", 100)
            except Exception as exc:
                errors.append(exc)

        with patch.object(fetcher, "_request", side_effect=request):
            first = threading.Thread(target=html_fetch)
            second = threading.Thread(target=pdf_fetch)
            first.start()
            try:
                self.assertTrue(started.wait(1))
                second.start()
                self.assertTrue(pdf_started.wait(1))
                self.assertFalse(pdf_requested.wait(0.1))
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(requests, ["/robots.txt", "/zh-hk/local/information/resultsall?racedate=2026/11/18&Racecourse=HV", "/pdf"])


class CollectResultsTests(TestCase):
    def test_requires_independent_explicit_authorization_before_network(self):
        with patch("collection.fetch.PublicFetcher.fetch") as fetch:
            for value in (False, None, 1):
                with self.subTest(value=value), self.assertRaises(CollectionError) as error:
                    collect_results(URL, terms_reviewed=value)
                self.assertEqual(error.exception.code, "not_authorized")
            fetch.assert_not_called()
        self.assertEqual(Observation.objects.count(), 0)

    def test_saves_one_unverified_observation_per_race(self):
        with patch("collection.fetch.PublicFetcher.fetch", return_value=PAGE) as fetch:
            observations = collect_results(URL, terms_reviewed=True)
        fetch.assert_called_once_with("hkjc-resultsall", URL)
        self.assertEqual(len(observations), 2)
        first, second = [item.payload for item in observations]
        self.assertEqual(first["results"][1]["placing"], "2 DH")
        self.assertEqual(first["results"][2]["placing"], "2 DH")
        self.assertEqual(first["results"][3]["placing"], "退出")
        self.assertEqual(first["dividends"][1],
                         {"pool": "位置", "combination": "2", "dividend": "退款"})
        self.assertEqual(first["dividends"][0]["base"], "10")
        self.assertEqual(second["race_number"], 2)
        self.assertEqual(second["dividends"], [])
        self.assertEqual(first["request_parameters"], {"racedate": "2026/11/18", "racecourse": "HV"})
        self.assertTrue(first["review_required"])
        self.assertEqual(first["review_flags"], ["possible_dead_heat", "possible_withdrawal", "possible_refund"])
        self.assertEqual(second["review_flags"], [])
        for payload in (first, second):
            self.assertEqual(payload["extraction_completeness"], "unknown")
            self.assertFalse(payload["content_identity_verified"])
            self.assertTrue(payload["extraction_limitations"])
            self.assertEqual(payload["request_label_comparison"], {"date": "unavailable", "venue": "unavailable"})
        self.assertTrue(all(o.source_url == BASE and o.status == "success" for o in observations))
        self.assertEqual(RaceResult.objects.count(), 0)

    def test_no_inference_from_unknown_or_empty_layout(self):
        with patch("collection.fetch.PublicFetcher.fetch",
                   return_value="<h2>Race 1</h2><table><tr><td>1</td><td>7</td></tr></table>"):
            with self.assertRaises(CollectionError) as error:
                collect_results(URL, terms_reviewed=True)
        self.assertEqual(error.exception.code, "parse_error")
        self.assertFalse(Observation.objects.exists())

    def test_invalid_url_and_network_errors_are_safe(self):
        with patch("collection.fetch.PublicFetcher.fetch",
                   side_effect=FetchError("robots.txt disallows this URL. secret")) as fetch:
            with self.assertRaises(CollectionError) as error:
                collect_results(BASE + "?token=secret", terms_reviewed=True)
            self.assertEqual(str(error.exception), "invalid_url")
            fetch.assert_not_called()
            with self.assertRaises(CollectionError) as error:
                collect_results(URL, terms_reviewed=True)
            self.assertEqual(str(error.exception), "access_denied")
        self.assertFalse(Observation.objects.exists())

    def test_english_headers_and_no_guessed_base(self):
        html = """<h2>Race 3</h2>
        <table><tr><th>Placing</th><th>Horse No.</th><th>Horse</th></tr>
        <tr><td>1</td><td>9</td><td>Example</td></tr></table>
        <table><tr><th>Pool</th><th>Winning Combination</th><th>Dividend (HK$)</th></tr>
        <tr><td>WIN</td><td>9</td><td>47.00</td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            payload = collect_results(URL, terms_reviewed=True)[0].payload
        self.assertEqual(payload["dividends"], [{"pool": "WIN", "combination": "9", "dividend": "47.00"}])

    def test_explicit_meeting_identity_is_checked_not_assumed(self):
        prefix = "<p>賽事日期: 18/11/2026</p><p>馬場: 跑馬地</p>"
        with patch("collection.fetch.PublicFetcher.fetch", return_value=prefix + PAGE):
            payload = collect_results(URL, terms_reviewed=True)[0].payload
        self.assertEqual(payload["displayed_date"], "2026/11/18")
        self.assertEqual(payload["displayed_venue"], "HV")
        self.assertEqual(payload["request_label_comparison"], {"date": "matched", "venue": "matched"})
        self.assertFalse(payload["content_identity_verified"])
        self.assertEqual(payload["extraction_completeness"], "unknown")
        before = Observation.objects.count()
        with patch("collection.fetch.PublicFetcher.fetch",
                   return_value=prefix.replace("18/11/2026", "19/11/2026") + PAGE):
            with self.assertRaises(CollectionError) as error:
                collect_results(URL, terms_reviewed=True)
        self.assertEqual(error.exception.code, "parse_error")
        self.assertEqual(Observation.objects.count(), before)

    def test_unexpected_network_errors_are_not_exposed(self):
        with patch("collection.fetch.PublicFetcher.fetch", side_effect=ValueError("private")):
            with self.assertRaises(CollectionError) as error:
                collect_results(URL, terms_reviewed=True)
        self.assertEqual(str(error.exception), "collection_error")

    def test_explicit_header_base_is_kept(self):
        html = """<h2>Race 1</h2><table>
        <tr><th>Pool</th><th>Winning Combination</th><th>Dividend (per HK$10)</th></tr>
        <tr><td>WIN</td><td>7</td><td>47.00</td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            payload = collect_results(URL, terms_reviewed=True)[0].payload
        self.assertEqual(payload["dividends"][0]["base"], "10")

    def test_nested_container_tables_assign_each_result_to_its_marker(self):
        html = """<table><tr><td>
        <div>Unrelated information</div><h2>Race 1</h2>
        <table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>7</td></tr></table>
        <div>Other information</div><h2>Race 2</h2>
        <table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>8</td></tr><tr><td>1</td><td>9</td></tr></table>
        </td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            results = collect_results(URL, terms_reviewed=True)
        first, second = [item.payload for item in results]
        self.assertEqual((first["race_number"], first["results"][0]["horse_no"]), (1, "7"))
        self.assertEqual((second["race_number"], [r["horse_no"] for r in second["results"]]),
                         (2, ["8", "9"]))
        self.assertEqual(second["review_flags"], ["possible_dead_heat"])
        self.assertTrue(all(p["extraction_completeness"] == "unknown" for p in (first, second)))

    def test_result_cell_text_does_not_change_race(self):
        html = """<table><tr><td>Race 1</td></tr></table>
        <table><tr><th>Placing</th><th>Horse No.</th><th>Horse</th></tr>
        <tr><td>1</td><td>7</td><td><span>Race 2</span></td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            payload = collect_results(URL, terms_reviewed=True)[0].payload
        self.assertEqual(payload["race_number"], 1)
        self.assertEqual(payload["results"][0]["horse_name"], "Race 2")

    def test_multi_cell_heading_row_precedes_nested_result_table(self):
        html = """<h2>Race 1</h2>
        <table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>7</td></tr></table>
        <table><tr><td><h2>Race 2</h2></td><td>Race details</td></tr>
        <tr><td><table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>8</td></tr></table></td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            payloads = [o.payload for o in collect_results(URL, terms_reviewed=True)]
        self.assertEqual([(p["race_number"], [r["horse_no"] for r in p["results"]])
                          for p in payloads], [(1, ["7"]), (2, ["8"])])

    def test_ambiguous_marker_row_clears_previous_race_until_safe_marker(self):
        html = """<h2>Race 1</h2><table>
        <tr><th>Placing</th><th>Horse No.</th></tr><tr><td>1</td><td>7</td></tr></table>
        <table><tr><td><h2>Race 2</h2></td><td><h2>Race 3</h2></td></tr>
        <tr><td><table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>8</td></tr></table></td></tr></table>
        <h2>Race 4</h2><table><tr><th>Placing</th><th>Horse No.</th></tr>
        <tr><td>1</td><td>9</td></tr></table>"""
        with patch("collection.fetch.PublicFetcher.fetch", return_value=html):
            payloads = [o.payload for o in collect_results(URL, terms_reviewed=True)]
        self.assertEqual([(p["race_number"], [r["horse_no"] for r in p["results"]])
                          for p in payloads], [(1, ["7"]), (4, ["9"])])
