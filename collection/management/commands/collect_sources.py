from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from collection.extract import extract_page
from collection.fetch import FetchError, PublicFetcher, validate_url
from collection.services import record_failure, save_observation
from racing.models import Race


URLS = {
    "hkjc-racecard": "https://racing.hkjc.com/racing/information/Chinese/racing/RaceCard.aspx",
    "hkjc-results": "https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx",
    "oncc": "https://racing.on.cc/",
    "horseracinghk": "https://horseracinghk.com/",
    "threads": "https://www.threads.net/",
}


def _failure_code(error):
    text = str(error).lower()
    if "robots" in text or "proxy" in text or "non-public" in text:
        return "access_denied"
    if "status" in text:
        return "http_error"
    if "size limit" in text:
        return "too_large"
    if "timed out" in text:
        return "timeout"
    if "dns" in text or "network" in text or "tls" in text:
        return "network_error"
    return "invalid_response"


class Command(BaseCommand):
    help = ("Collect one explicitly authorized public source as unverified observations. "
            "Robots access is not a licence; review source terms before opting in. "
            "Do not overlap collector processes; rate limiting is process-local. "
            "Use --due-results for scheduled polling after a recorded finish. "
            "Direct --url or race-date results requests are manual one-offs and bypass the finish gate; do not schedule them.")

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, choices=URLS)
        parser.add_argument("--terms-reviewed", action="store_true",
                            help="Confirm authorization and review of this source's terms before requesting it.")
        parser.add_argument("--url", help="Manual one-off exact supported URL, without secrets; results have no finish-time gate.")
        parser.add_argument("--race-date", help="Manual one-off HKJC date YYYY-MM-DD; results have no finish-time gate.")
        parser.add_argument("--venue", choices=("ST", "HV"), help="HKJC racecourse code.")
        parser.add_argument("--race-number", type=int, help="HKJC race number (1–14).")
        parser.add_argument("--race-id", type=int,
                            help="For results, poll this tracked race only after its recorded finish + 30 minutes.")
        parser.add_argument("--due-results", action="store_true",
                            help="Poll tracked races finished at least 30 minutes ago, within the lookback.")
        parser.add_argument("--lookback-hours", type=int, default=24,
                            help="Finite finish-time lookback for --due-results (1–168 hours; default 24).")

    def handle(self, *args, **options):
        source = options["source"]
        base = URLS[source]
        kind = f"{source}-race-{options['race_id']}" if options["race_id"] is not None else source
        now = timezone.now()
        try:
            if not options["terms_reviewed"] or source == "threads":
                raise _CollectionFailure("not_authorized", "Source not authorized; Threads requires an approved official API or manual review.")
            if options["due_results"]:
                if (source != "hkjc-results" or any(options[key] for key in ("url", "race_id", "race_date", "venue", "race_number"))
                        or not 1 <= options["lookback_hours"] <= 168):
                    raise _CollectionFailure("invalid_response", "Due-results requires HKJC results, no race selection, and a 1–168 hour lookback.")
                self.fetcher = PublicFetcher()
                errors = 0
                races = Race.objects.filter(Q(predictions__isnull=False) | Q(actual_bets__isnull=False)).distinct()
                for race in races.iterator():
                    latest = race.results.order_by("-version").first()
                    finish = latest.actual_finished_at if latest else None
                    if (not finish or timezone.is_naive(finish) or finish < race.starts_at
                            or not now - timedelta(hours=options["lookback_hours"]) <= finish <= now - timedelta(minutes=30)):
                        continue
                    item_options = {**options, "race_id": race.pk, "due_results": False}
                    try:
                        self.handle(*args, **item_options)
                    except CommandError:
                        errors += 1
                if errors:
                    raise CommandError(f"{errors} tracked race collection(s) failed; see pending observations.")
                return
            if options["race_id"] is not None:
                if source != "hkjc-results" or any(options[key] for key in ("url", "race_date", "venue", "race_number")):
                    raise _CollectionFailure("invalid_response", "Race ID is only supported for tracked results without other URL parameters.")
                race = Race.objects.select_related("meeting").filter(pk=options["race_id"]).first()
                if not race:
                    raise _CollectionFailure("invalid_response", "Tracked race does not exist.")
                result = race.results.order_by("-version").first()
                finish = result.actual_finished_at if result else None
                if (not finish or timezone.is_naive(finish) or finish < race.starts_at
                        or finish > now - timedelta(minutes=30)):
                    raise _CollectionFailure("not_authorized", "A recorded finish at least 30 minutes ago is required.")
                venue = {"st": "ST", "sha tin": "ST", "沙田": "ST",
                         "hv": "HV", "happy valley": "HV", "跑馬地": "HV"}.get(
                    race.meeting.venue.strip().casefold())
                if not venue:
                    raise _CollectionFailure("invalid_response", "Tracked race venue is not ST or HV.")
                race_date, number = race.meeting.date, race.number
            else:
                race_date, venue, number = options["race_date"], options["venue"], options["race_number"]
                if race_date:
                    try:
                        race_date = date.fromisoformat(race_date)
                    except ValueError:
                        raise _CollectionFailure("invalid_response", "Race date must be YYYY-MM-DD.") from None
            if options["url"] and any((race_date, venue, number)):
                raise _CollectionFailure("invalid_response", "Use a URL or race parameters, not both.")
            if any((race_date, venue, number)):
                if source not in {"hkjc-racecard", "hkjc-results"} or not all((race_date, venue, number)) or not 1 <= number <= 14:
                    raise _CollectionFailure("invalid_response", "Race date, ST/HV venue and race number 1–14 are required together.")
                query = urlencode({"RaceDate": race_date.strftime("%Y/%m/%d"), "Racecourse": venue, "RaceNo": number})
                url = base + "?" + query
            else:
                url = options["url"] or base
            parts = validate_url(url, source)
            safe_url = urlunsplit(("https", parts.hostname, parts.path or "/", "", ""))
            if not hasattr(self, "fetcher"):
                self.fetcher = PublicFetcher()
            html = self.fetcher.fetch(source, url)
            try:
                payload = extract_page(html, base_url=safe_url, tables=source.startswith("hkjc-"))
            except Exception:
                raise _CollectionFailure("parse_error", "Could not extract the page for review.") from None
            if (source.startswith("hkjc-") and not payload["tables"]
                    or not source.startswith("hkjc-") and not any(
                        (payload["title"], payload["description"], payload["headlines"]))):
                raise _CollectionFailure("parse_error", "The page contained no usable review content.")
            payload["request_parameters"] = dict(parse_qsl(parts.query))
            if options["race_id"] is not None:
                payload["tracked_race_id"] = options["race_id"]
            payload["review_required"] = True
            save_observation(safe_url, source, payload, timezone.now())
            self.stdout.write("Unverified observation saved for manual review.")
        except _CollectionFailure as exc:
            record_failure(base, kind, exc.code, timezone.now())
            raise CommandError(str(exc)) from None
        except FetchError as exc:
            record_failure(base, kind, _failure_code(exc), timezone.now())
            raise CommandError(f"Collection failed: {_failure_code(exc)}.") from None


class _CollectionFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
