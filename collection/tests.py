from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from racing.models import Meeting, Prediction, Race, RaceResult, Recommendation, Runner, SourcePost

from .models import Observation
from .services import record_failure, save_observation


class ObservationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.url = "https://example.com/race/1"

    def test_duplicates_are_idempotent_and_retrievals_are_separate(self):
        first, created = save_observation(self.url, "race", {"b": [2], "a": 1}, self.now)
        self.assertTrue(created)
        duplicate, created = save_observation(
            self.url, "race", {"a": 1, "b": [2]}, self.now
        )
        self.assertFalse(created)
        self.assertEqual(duplicate.pk, first.pk)
        self.assertEqual(duplicate.retrieved_at, self.now)
        later, created = save_observation(self.url, "race", {"a": 1, "b": [2]}, self.now + timedelta(minutes=5))
        self.assertTrue(created)
        self.assertNotEqual(first.pk, later.pk)
        changed, created = save_observation(self.url, "race", {"a": 2}, self.now)
        self.assertTrue(created)
        self.assertNotEqual(first.pk, changed.pk)
        self.assertEqual(Observation.objects.count(), 3)

    def test_failure_is_observable_bounded_and_deduplicated(self):
        failed, created = record_failure(self.url, "results", "timeout", self.now)
        self.assertTrue(created)
        self.assertEqual(failed.status, Observation.Status.FAILURE)
        self.assertIsNone(failed.payload)
        self.assertEqual(failed.error, "timeout")
        again, created = record_failure(self.url, "results", "timeout", self.now)
        self.assertFalse(created)
        self.assertEqual(again.pk, failed.pk)
        retry, created = record_failure(self.url, "results", "timeout", self.now + timedelta(seconds=1))
        self.assertTrue(created)
        self.assertNotEqual(retry.pk, failed.pk)
        success, created = save_observation(self.url, "results", {}, self.now)
        self.assertTrue(created)
        self.assertNotEqual(success.pk, failed.pk)
        with self.assertRaises(ValidationError):
            record_failure(self.url, "results", "x" * 513, self.now)
        with self.assertRaises(ValidationError):
            record_failure(self.url, "results", "", self.now)
        with self.assertRaises(ValidationError):
            record_failure(self.url, "results", "secret token", self.now)

    def test_invalid_inputs_are_rejected_without_writing(self):
        bad_inputs = [
            ("file:///etc/passwd", "race", {}, self.now),
            ("******example.com/", "race", {}, self.now),
            ("http://[invalid", "race", {}, self.now),
            ("******example.com/race", "race", {}, self.now),
            ("https://example.com/race?token=secret", "race", {}, self.now),
            ("https://example.com/race#token", "race", {}, self.now),
            (self.url, "", {}, self.now),
            (self.url, " race ", {}, self.now),
            (self.url, "x" * 65, {}, self.now),
            (self.url, "race", "raw text", self.now),
            (self.url, "race", {"raw": "x" * 65536}, self.now),
            (self.url, "race", {"bad": float("nan")}, self.now),
            (self.url, "race", {1: "not a JSON key"}, self.now),
            (self.url, "race", {"bad": (1, 2)}, self.now),
            (self.url, "race", {}, self.now.replace(tzinfo=None)),
        ]
        for args in bad_inputs:
            with self.subTest(args=args[:2]), self.assertRaises(ValidationError):
                save_observation(*args)
        self.assertEqual(Observation.objects.count(), 0)

    def test_database_enforces_fingerprint_uniqueness(self):
        save_observation(self.url, "race", {"a": 1}, self.now)
        duplicate = Observation(
            source_url=self.url, kind="race", payload={"a": 1},
            status=Observation.Status.SUCCESS, retrieved_at=self.now,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()

    def test_existing_verified_racing_data_is_not_changed(self):
        meeting = Meeting.objects.create(date=self.now.date(), venue="Sha Tin", official_verified=True, verified_by="A")
        race = Race.objects.create(meeting=meeting, number=1, name="Original", starts_at=self.now,
                                   official_verified=True, verified_by="A")
        runner = Runner.objects.create(race=race, number=1, name="Original", official_verified=True, verified_by="A")
        post = SourcePost.objects.create(
            source_type=SourcePost.SourceType.OTHER, url=self.url, author="Author",
            summary="Original", published_at=self.now - timedelta(hours=1),
            retrieved_at=self.now, manually_verified=True, verifier="A",
        )
        rec = Recommendation.objects.create(post=post, race=race, runner=runner,
                                            official_match_verified=True, verifier="A")
        prediction = Prediction.objects.create(race=race, version=1, approved_at=self.now, inputs={"original": True})
        result = RaceResult.objects.create(race=race, version=1, recorded_at=self.now, inputs={"original": True})
        save_observation(self.url, "source_post", {"author": "Altered", "verified": True}, self.now)
        record_failure(self.url, "result", "network_error", self.now)
        meeting.refresh_from_db()
        race.refresh_from_db()
        runner.refresh_from_db()
        post.refresh_from_db()
        rec.refresh_from_db()
        prediction.refresh_from_db()
        result.refresh_from_db()
        self.assertTrue(meeting.official_verified and race.official_verified and runner.official_verified)
        self.assertEqual((race.name, runner.name, post.author, post.summary), ("Original", "Original", "Author", "Original"))
        self.assertTrue(post.manually_verified and rec.official_match_verified)
        self.assertEqual(prediction.inputs, {"original": True})
        self.assertEqual(result.inputs, {"original": True})
        self.assertEqual(RaceResult.objects.count(), 1)
        self.assertEqual(Prediction.objects.count(), 1)
        self.assertEqual(SourcePost.objects.count(), 1)
        self.assertEqual(Recommendation.objects.count(), 1)

    def test_admin_is_read_only_and_marks_data_unverified(self):
        obs, _ = save_observation(self.url, "race", {"runner": "Candidate"}, self.now)
        self.client.force_login(get_user_model().objects.create_superuser("admin", "a@example.com", "password"))
        response = self.client.get(reverse("admin:collection_observation_change", args=[obs.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unverified")
        self.assertContains(response, "Candidate")
        self.assertEqual(self.client.post(
            reverse("admin:collection_observation_change", args=[obs.pk]), {"kind": "altered"}
        ).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:collection_observation_add")).status_code, 403)
        self.assertEqual(self.client.post(
            reverse("admin:collection_observation_delete", args=[obs.pk]), {"post": "yes"}
        ).status_code, 403)
        obs.refresh_from_db()
        self.assertEqual(obs.kind, "race")
