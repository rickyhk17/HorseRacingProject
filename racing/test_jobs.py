from datetime import datetime, timedelta, timezone as utc
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from notifications.models import Notification
from racing.management.commands.run_racing_jobs import _dashboard_path
from racing.models import (
    ActualBet, Meeting, Prediction, Publication, Race, RaceResult, Recommendation, Runner, SourcePost,
)
from racing.services import approve_prediction


class RacingJobsTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 6, 0, tzinfo=utc.utc)
        self.meeting = Meeting.objects.create(date=self.now.date(), venue="Test", official_verified=True)
        self.first = Race.objects.create(
            meeting=self.meeting, number=1, name="First", starts_at=self.now + timedelta(hours=1),
            official_verified=True,
        )
        self.race = Race.objects.create(
            meeting=self.meeting, number=2, name="Second", starts_at=self.now + timedelta(hours=2),
            official_verified=True, approval_rationale="Race form and four runner selections reviewed.",
        )
        self.runners = [
            Runner.objects.create(race=self.race, number=n, name=f"Runner {n}",
                                  official_verified=True)
            for n in range(1, 10)
        ]
        with patch("django.utils.timezone.now", return_value=self.now - timedelta(hours=1)):
            post = SourcePost.objects.create(
                source_type=SourcePost.SourceType.THREADS, url="https://threads.net/@example/post/123",
                author="Example", summary="Verified recommendation", followers=1001,
                published_at=self.now - timedelta(hours=3), retrieved_at=self.now - timedelta(hours=2),
                manually_verified=True, verifier="Operator",
            )
            Recommendation.objects.create(post=post, race=self.race, runner=self.runners[0],
                                          official_match_verified=True, verifier="Operator")

    def run_jobs(self, at=None, *options):
        with patch("django.utils.timezone.now", return_value=at or self.now):
            call_command("run_racing_jobs", *options, verbosity=0)

    def prediction(self, version=1, selections=None):
        if selections is None:
            selections = [1, 2, 3, 4]
        if not all(isinstance(n, int) for n in selections):
            return Prediction.objects.create(
                race=self.race, version=version, approved_at=self.now - timedelta(minutes=1),
                inputs={"selections": selections},
            )
        with patch("django.utils.timezone.now", return_value=self.now - timedelta(minutes=1)):
            return approve_prediction(self.race, [self.runners[n - 1] for n in selections],
                                      allow_late_revision=version > 1)

    def bet(self, race=None):
        return ActualBet.objects.create(
            race=race or self.race, placed_at=self.now - timedelta(hours=1),
            description="Manually recorded", stake="10.00",
        )

    def result(self, version=1, finish=None, status="pending", verified=False):
        return RaceResult.objects.create(
            race=self.race, version=version, recorded_at=self.now - timedelta(hours=1),
            actual_finished_at=finish,
            inputs={"status": status, "official_verified": verified, "notes": "private-secret"},
        )

    def texts(self):
        return list(Notification.objects.values_list("text", flat=True))

    def test_cutoff_publication_revisions_and_retries(self):
        prediction = self.prediction()
        self.run_jobs(self.now - timedelta(seconds=1), "--race-id", str(self.race.pk))
        self.assertFalse(Publication.objects.exists())
        self.assertFalse(Notification.objects.exists())
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertTrue(Publication.objects.filter(prediction=prediction).exists())
        self.assertIn("Selections: 1, 2, 3, 4", self.texts()[0])
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertEqual(Notification.objects.count(), 1)
        revision = self.prediction(version=2, selections=[4, 3, 2, 1])
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertTrue(Publication.objects.filter(prediction=revision).exists())
        self.assertEqual(Notification.objects.count(), 2)
        self.assertTrue(any("Prediction update v2" in text for text in self.texts()))

    def test_overdue_and_invalid_predictions_fail_closed(self):
        self.bet()
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertIn("approval is overdue", self.texts()[0])
        self.prediction(selections=[{"number": 1}] * 4)
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertFalse(Publication.objects.exists())
        self.assertTrue(any("not published" in text for text in self.texts()))

    def test_late_run_never_publishes_and_reports_missing_data(self):
        self.prediction()
        self.run_jobs(self.race.starts_at, "--race-id", str(self.race.pk))
        self.assertFalse(Publication.objects.exists())
        self.assertTrue(any("not published before" in text for text in self.texts()))
        self.assertTrue(any("No result was fetched" in text for text in self.texts()))
        self.assertFalse(any("30 minutes" in text for text in self.texts()))

    def test_result_reminder_requires_explicit_finish_and_waits_30_minutes(self):
        self.bet()
        result = self.result(finish=None)
        self.run_jobs(self.now, "--race-id", str(self.race.pk))
        self.assertFalse(any("30 minutes" in text for text in self.texts()))
        result = self.result(version=2, finish=self.now - timedelta(minutes=29))
        self.run_jobs(self.now, "--race-id", str(self.race.pk))
        self.assertFalse(any("30 minutes" in text for text in self.texts()))
        self.run_jobs(self.now + timedelta(minutes=1), "--race-id", str(self.race.pk))
        self.assertEqual(Notification.objects.filter(idempotency_key=f"racing:result-reminder:{result.pk}").count(), 1)
        self.run_jobs(self.now + timedelta(minutes=2), "--race-id", str(self.race.pk))
        self.assertEqual(Notification.objects.filter(idempotency_key=f"racing:result-reminder:{result.pk}").count(), 1)
        self.assertFalse(any("private-secret" in text for text in self.texts()))

    def test_latest_verified_final_result_needs_no_reminder(self):
        self.bet()
        self.result(finish=self.now - timedelta(hours=1))
        self.result(version=2, finish=self.now - timedelta(hours=1), status="final", verified=True)
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertFalse(any("30 minutes" in text for text in self.texts()))
        self.assertTrue(any("status final" in text for text in self.texts()))

    def test_unverified_final_fails_closed(self):
        self.bet()
        self.result(finish=self.now - timedelta(hours=1), status="final")
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertTrue(any("status unknown" in text for text in self.texts()))
        self.assertTrue(any("30 minutes" in text for text in self.texts()))

    def test_dashboard_path_strictly_relative_and_stable_across_reruns(self):
        self.prediction()
        self.bet(self.first)
        with patch.dict("os.environ", {"RACING_DASHBOARD_PATH": "/dashboard/"}, clear=False):
            self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertIn("Dashboard: /dashboard/", self.texts()[0])
        with patch.dict("os.environ", {"RACING_DASHBOARD_PATH": "//example.org/?token=secret"}, clear=False):
            self.run_jobs(None, "--race-id", str(self.race.pk))
            self.run_jobs(None, "--race-id", str(self.first.pk))
        self.assertEqual(Notification.objects.count(), 2)
        self.assertFalse(any("token" in text or "example.org" in text for text in self.texts()))

    def test_invalid_race_selection_does_not_partially_process(self):
        with self.assertRaises(CommandError):
            self.run_jobs(None, "--race-id", str(self.race.pk), "--race-id", "999999")
        self.assertFalse(Notification.objects.exists())

    def test_publication_and_enqueue_are_atomic(self):
        self.prediction()
        with patch("racing.management.commands.run_racing_jobs.enqueue_notification", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertFalse(Publication.objects.exists())

    def test_only_selected_or_bet_races_are_alerted_even_when_explicit(self):
        self.result(finish=self.now - timedelta(hours=1))
        self.run_jobs(self.race.starts_at, "--race-id", str(self.race.pk))
        self.assertFalse(Notification.objects.exists())

    def test_default_window_excludes_historical_races_and_is_configurable(self):
        self.bet()
        later = self.race.starts_at + timedelta(hours=25)
        self.run_jobs(later)
        self.assertFalse(Notification.objects.exists())
        self.run_jobs(later, "--lookback-hours", "26")
        self.assertTrue(any("approval is overdue" in text for text in self.texts()))

    def test_publication_failure_warns_and_other_races_continue(self):
        self.prediction()
        self.bet(self.first)
        runner = self.runners[0]
        runner.active = False
        runner.save()
        self.run_jobs()
        self.assertFalse(Publication.objects.exists())
        self.assertTrue(any("manual review of eligibility" in text for text in self.texts()))
        self.assertTrue(any(f"Race {self.first.pk}: prediction approval is overdue" in text
                            for text in self.texts()))

    def test_revision_publishes_without_waiting_for_new_cutoff(self):
        self.prediction()
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.first.starts_at += timedelta(hours=1)
        self.first.save()
        self.race.starts_at += timedelta(hours=1)
        self.race.save()
        revision = self.prediction(version=2)
        self.run_jobs(None, "--race-id", str(self.race.pk))
        self.assertTrue(Publication.objects.filter(prediction=revision).exists())
        self.assertTrue(any("Prediction update v2" in text for text in self.texts()))

    def test_https_dashboard_url_validation(self):
        valid = "https://dashboard.example.com:8443/dashboard/"
        with patch.dict("os.environ", {"RACING_DASHBOARD_URL": valid, "RACING_DASHBOARD_PATH": ""}):
            self.assertEqual(_dashboard_path(), valid)
            self.bet()
            self.run_jobs(None, "--race-id", str(self.race.pk))
            self.assertIn(valid, self.texts()[0])
        for invalid in (
            "http://dashboard.example.com/", "https://user@dashboard.example.com/",
            "https://@dashboard.example.com/", "https://dashboard.example.com/?token=secret",
            "https://dashboard.example.com/#fragment", "//dashboard.example.com/",
            "https://dashboard.example.com/\nsecret", "https://dashboard.example.com:99999/",
        ):
            with self.subTest(invalid=invalid):
                with patch.dict("os.environ", {"RACING_DASHBOARD_URL": invalid, "RACING_DASHBOARD_PATH": ""}):
                    self.assertEqual(_dashboard_path(), "")
