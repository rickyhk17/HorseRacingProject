from datetime import datetime, timedelta, timezone
from io import StringIO
import http.client
import socket
import threading
import time
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from collection.extract import extract_page
from collection.fetch import FetchError, PublicFetcher, _robots_allowed, _robots_delay, validate_url
from collection.models import Observation
from racing.models import ActualBet, Meeting, Race, RaceResult


class FetchTests(SimpleTestCase):
    def test_strict_urls_and_query_parameters(self):
        source = "hkjc-results"
        base = "https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx"
        validate_url(base + "?RaceDate=2026%2F11%2F20&Racecourse=ST&RaceNo=3", source)
        for suffix in ("?RaceDate=2026%2F11%2F20&Racecourse=ST&RaceNo=3&token=secret",
                       "?RaceDate=2026%2F11%2F20&Racecourse=ST&RaceNo=3&RaceNo=4",
                       "?RaceDate=2026%2F99%2F20&Racecourse=ST&RaceNo=3", "#x"):
            with self.subTest(suffix=suffix), self.assertRaises(FetchError):
                validate_url(base + suffix, source)
        for url in ("http://racing.on.cc/", "https://racing.on.cc.evil.example/",
                    "https://user@racing.on.cc/", "https://racing.on.cc:443/",
                    "https://racing.on.cc/private", "https://racing.on.cc/?token=x"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                validate_url(url, "oncc")

    def test_robots_conservatively_blocks_matching_disallow(self):
        self.assertFalse(_robots_allowed("User-agent: *\nAllow: /\nDisallow: /private*\n", "/private/page"))
        self.assertFalse(_robots_allowed("User-agent: HorseRacingProjectCollector\nDisallow: /foo$\n", "/foo"))
        self.assertTrue(_robots_allowed("User-agent: *\nDisallow: /foo$\n", "/foobar"))
        with self.assertRaises(FetchError):
            _robots_allowed("", "/")
        self.assertEqual(_robots_delay("User-agent: *\nCrawl-delay: 5\n"), 5)
        with self.assertRaises(FetchError):
            _robots_delay("User-agent: *\nRequest-rate: 1/30\n")

    def test_robots_normalizes_percent_encodings_and_rejects_malformed(self):
        prefix = "/racing/information/Chinese/Racing/LocalResults.aspx"
        robots = f"User-agent: *\nDisallow: {prefix}?RaceDate=2026%2F11%2F20\n"
        self.assertFalse(_robots_allowed(robots, prefix + "?%52aceDate=2026%2f11%2f20&RaceNo=1"))
        self.assertFalse(_robots_allowed("User-agent: *\nDisallow: /%7euser\n", "/~user"))
        for rule, path in (("/%G1", "/x"), ("/x", "/%"), ("/x", "/%1")):
            with self.subTest(rule=rule, path=path), self.assertRaises(FetchError):
                _robots_allowed(f"User-agent: *\nDisallow: {rule}\n", path)
        url = ("https://racing.hkjc.com" + prefix
               + "?RaceDate=2026%2F11%2F20&Racecourse=ST&RaceNo=%3")
        with self.assertRaises(FetchError):
            validate_url(url, "hkjc-results")

    def test_response_deadline_interrupts_closed_connection_body(self):
        client, server = socket.socketpair()
        client.settimeout(2)
        server.settimeout(2)
        ready = threading.Event()

        def serve():
            try:
                server.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                               b"Content-Length: 100\r\nConnection: close\r\n\r\nx")
                ready.set()
                server.recv(1)
            finally:
                server.close()

        class Connection:
            sock = None
            def __init__(self, *args):
                pass
            def connect(self):
                self.sock = client
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                response = http.client.HTTPResponse(self.sock)
                response.begin()
                self.sock = None
                return response
            def close(self):
                if self.sock:
                    self.sock.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(1))
        try:
            with patch.dict("os.environ", {}, clear=True), patch("collection.fetch._resolve", return_value={"8.8.8.8"}), \
                    patch("collection.fetch._PinnedHTTPSConnection", Connection), patch("collection.fetch.TIMEOUT", 0.1):
                start = time.monotonic()
                with self.assertRaises(FetchError):
                    PublicFetcher()._request("racing.on.cc", "/", 100)
                self.assertLess(time.monotonic() - start, 1)
        finally:
            client.close()
            thread.join(2)

    def test_request_spacing_starts_after_previous_completion(self):
        clock = [0.0]
        starts = []
        class Response:
            status = 200
            def getheader(self, _name, default=""):
                return "text/plain"
            def read(self, _count):
                clock[0] += 7
                return b"ok"
        class Connection:
            sock = None
            def __init__(self, *args):
                pass
            def connect(self):
                pass
            def request(self, *args, **kwargs):
                starts.append(clock[0])
            def getresponse(self):
                return Response()
            def close(self):
                pass
        class Timer:
            def __init__(self, *args):
                pass
            def start(self):
                pass
            def cancel(self):
                pass
        def resolve(_host):
            clock[0] += 1
            return {"8.8.8.8"}
        def sleep(seconds):
            clock[0] += seconds
        with patch.dict("os.environ", {}, clear=True), patch("collection.fetch._resolve", side_effect=resolve), \
                patch("collection.fetch._PinnedHTTPSConnection", Connection), \
                patch("collection.fetch.threading.Timer", Timer), \
                patch("collection.fetch.time.monotonic", side_effect=lambda: clock[0]), \
                patch("collection.fetch.time.sleep", side_effect=sleep):
            fetcher = PublicFetcher()
            fetcher._request("racing.on.cc", "/", 100)
            fetcher._request("racing.on.cc", "/", 100)
            self.assertEqual(starts, [1, 10])
            self.assertEqual(fetcher.last_request, 17)

    def test_robots_must_succeed_before_page_request(self):
        fetcher = PublicFetcher()
        with patch.object(fetcher, "_request", return_value=("text/plain", b"User-agent: *\nDisallow: /\n")) as request:
            with self.assertRaises(FetchError):
                fetcher.fetch("oncc", "https://racing.on.cc/")
            self.assertEqual(request.call_count, 1)
        with patch.object(fetcher, "_request", side_effect=FetchError("HTTP status 403")) as request:
            with self.assertRaises(FetchError):
                fetcher.fetch("oncc", "https://racing.on.cc/")
            self.assertEqual(request.call_count, 1)

    def test_nonpublic_dns_and_proxy_are_rejected(self):
        with patch.dict("os.environ", {}, clear=True), patch("collection.fetch._resolve", return_value={"127.0.0.1"}):
            with self.assertRaises(FetchError):
                PublicFetcher()._request("racing.on.cc", "/", 100)
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://localhost:3128"}, clear=True):
            with patch("collection.fetch._resolve") as resolve:
                with self.assertRaises(FetchError):
                    PublicFetcher()._request("racing.on.cc", "/", 100)
                resolve.assert_not_called()

    def test_request_rejects_redirects_and_oversize_responses(self):
        class Response:
                status = 302
                def getheader(self, _name, default=""):
                    return "text/html"
                def read(self, count):
                    return b"x" * count
        class Connection:
                sock = None
                def __init__(self, *args):
                    pass
                def connect(self):
                    pass
                def request(self, *args, **kwargs):
                    pass
                def getresponse(self):
                    return Response()
                def close(self):
                    pass
        with patch.dict("os.environ", {}, clear=True), patch("collection.fetch._resolve", return_value={"8.8.8.8"}), \
                    patch("collection.fetch._PinnedHTTPSConnection", Connection):
                with self.assertRaisesRegex(FetchError, "302"):
                    PublicFetcher()._request("racing.on.cc", "/", 100)
                Response.status = 200
                with self.assertRaisesRegex(FetchError, "size limit"):
                    PublicFetcher()._request("racing.on.cc", "/", 100)

    def test_small_dividend_table_is_not_lost_among_long_tables(self):
        long_tables = "".join("<table>" + "".join(
            "<tr><td>馬號</td><td>horse</td></tr>" for _ in range(25)) + "</table>" for _ in range(7))
        html = (long_tables + "<table><tr><th>派彩</th><th>金額</th></tr>"
                "<tr><td>三重彩</td><td>1000</td></tr><tr><td>單T</td><td>200</td></tr></table>")
        result = extract_page(html, tables=True)
        self.assertTrue(any("三重彩" in str(table) and "單T" in str(table) for table in result["tables"]))
        self.assertTrue(result["tables_truncated"])

    def test_commentary_headlines_are_bounded_and_do_not_keep_query(self):
        html = ('<title>Tip</title><meta name="description" content="short excerpt">'
                '<h2><a href="/news/1?token=secret">Headline</a></h2>'
                '<h2><a href="https://evil.example/x">External</a></h2><p>Long article text</p>')
        result = extract_page(html, base_url="https://racing.on.cc/")
        self.assertEqual(result["headlines"], [{"title": "Headline", "url": "https://racing.on.cc/news/1"}])
        self.assertNotIn("Long article text", str(result))
        self.assertNotIn("secret", str(result))


class CollectionCommandTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
        meeting = Meeting.objects.create(date=self.now.date(), venue="Sha Tin")
        self.race = Race.objects.create(meeting=meeting, number=3, name="Test",
                                        starts_at=self.now - timedelta(hours=2))

    def command(self, *args):
        with patch("collection.management.commands.collect_sources.timezone.now", return_value=self.now):
            call_command("collect_sources", *args, stdout=StringIO())

    def test_finish_is_required_then_repeated_poll_is_unverified_and_identifiable(self):
        args = ("--source", "hkjc-results", "--terms-reviewed", "--race-id", str(self.race.pk))
        with patch("collection.management.commands.collect_sources.PublicFetcher.fetch",
                   return_value="<table><tr><td>名次</td><td>馬號</td></tr><tr><td>1</td><td>7</td></tr></table>") as fetch:
            with self.assertRaises(CommandError):
                self.command(*args)
            self.assertEqual(fetch.call_count, 0)
            RaceResult.objects.create(race=self.race, version=1, recorded_at=self.now,
                                      actual_finished_at=self.now - timedelta(minutes=29), inputs={"status": "pending"})
            with self.assertRaises(CommandError):
                self.command(*args)
            RaceResult.objects.create(race=self.race, version=2, recorded_at=self.now,
                                      actual_finished_at=self.now - timedelta(minutes=30), inputs={"status": "pending"})
            self.command(*args)
            self.now += timedelta(minutes=1)
            self.command(*args)
        successes = Observation.objects.filter(status=Observation.Status.SUCCESS).order_by("retrieved_at")
        self.assertEqual(successes.count(), 2)
        observation = successes.first()
        self.assertNotIn("?", observation.source_url)
        self.assertEqual(observation.payload["request_parameters"],
                         {"RaceDate": "2026/11/20", "Racecourse": "ST", "RaceNo": "3"})
        self.assertTrue(observation.payload["review_required"])
        self.assertFalse(self.race.official_verified)
        self.assertEqual(RaceResult.objects.count(), 2)

    def test_opt_in_and_threads_fail_without_network_and_record_failure(self):
        with patch("collection.management.commands.collect_sources.PublicFetcher.fetch") as fetch:
            for args in (("--source", "oncc"), ("--source", "threads", "--terms-reviewed")):
                with self.assertRaises(CommandError):
                    self.command(*args)
            fetch.assert_not_called()
        self.assertEqual(Observation.objects.filter(status="failure", error="not_authorized").count(), 2)

    def test_empty_pages_record_parse_failure(self):
        with patch("collection.management.commands.collect_sources.PublicFetcher.fetch",
                   return_value="<html><body>Consent required</body></html>"):
            for source in ("hkjc-results", "oncc"):
                with self.subTest(source=source), self.assertRaises(CommandError):
                    self.command("--source", source, "--terms-reviewed")
        self.assertEqual(Observation.objects.filter(status="failure", error="parse_error").count(), 2)
        self.assertFalse(Observation.objects.filter(status="success").exists())

    def test_sanitized_failure_and_robots_denial(self):
        with patch("collection.management.commands.collect_sources.PublicFetcher.fetch",
                   side_effect=FetchError("robots.txt disallows this URL.")):
            with self.assertRaises(CommandError):
                self.command("--source", "hkjc-results", "--terms-reviewed",
                             "--race-date", "2026-11-20", "--venue", "HV", "--race-number", "1")
        failure = Observation.objects.get()
        self.assertEqual(failure.error, "access_denied")
        self.assertNotIn("?", failure.source_url)

    def test_due_results_skip_missing_finish_and_continue_after_failure(self):
        self.race.meeting.venue = "沙田"
        self.race.meeting.save()
        def bet(race):
            ActualBet.objects.create(race=race, placed_at=self.now - timedelta(hours=2),
                                     description="tracked", stake="10")
        def result(race, finish):
            RaceResult.objects.create(race=race, version=1, recorded_at=self.now,
                                      actual_finished_at=finish, inputs={"status": "pending"})
        bet(self.race)
        result(self.race, self.now - timedelta(minutes=45))
        meeting = Meeting.objects.create(date=self.now.date(), venue="跑馬地")
        failing = Race.objects.create(meeting=meeting, number=4, name="Fail",
                                      starts_at=self.now - timedelta(hours=2))
        missing = Race.objects.create(meeting=meeting, number=5, name="Missing",
                                      starts_at=self.now - timedelta(hours=2))
        for race in (failing, missing):
            bet(race)
        result(failing, self.now - timedelta(minutes=45))
        result(missing, None)

        def fetch(source, url):
            if "Racecourse=ST" in url:
                raise FetchError("robots.txt disallows this URL.")
            return "<table><tr><td>名次</td><td>馬號</td></tr><tr><td>1</td><td>7</td></tr></table>"

        with patch("collection.management.commands.collect_sources.PublicFetcher.fetch", side_effect=fetch) as mocked:
            with self.assertRaises(CommandError):
                self.command("--source", "hkjc-results", "--terms-reviewed", "--due-results")
        self.assertEqual(mocked.call_count, 2)
        observation = Observation.objects.get(status="success")
        self.assertEqual(observation.payload["tracked_race_id"], failing.pk)
        self.assertEqual(observation.payload["request_parameters"]["Racecourse"], "HV")
        failure = Observation.objects.get(status="failure")
        self.assertEqual(failure.kind, f"hkjc-results-race-{self.race.pk}")
        self.assertEqual(failure.error, "access_denied")
