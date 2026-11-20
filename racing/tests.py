from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActualBet, Meeting, Prediction, Publication, Race, RaceResult, Recommendation,
    Runner, SourcePost, VerificationEvent,
)
from .services import (
    approve_prediction, eligible_recommendations, publication_cutoff, publish_prediction,
    race_eligibility, rank_candidates, record_result, simulate_settlement,
)


class RacingTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.meeting = Meeting.objects.create(date=self.now.date(), venue="Sha Tin", official_verified=True)
        self.race = Race.objects.create(meeting=self.meeting, number=1, name="Test",
                                        starts_at=self.now + timedelta(hours=4), official_verified=True,
                                        approval_rationale="Race has a verified field; selected runners are supported.")
        self.runners = [
            Runner.objects.create(race=self.race, number=i, name=f"Horse {i}", official_verified=True,
                                  first_time=i == 1)
            for i in range(1, 10)
        ]

    def source(self, suffix="a", **kwargs):
        values = {
            "url": f"https://www.threads.net/@tipster/post/{suffix}", "author": "Tipster",
            "summary": "Four runners", "followers": 1001, "verifier": "Operator",
            "published_at": self.now - timedelta(hours=1),
            "retrieved_at": self.now - timedelta(minutes=30), "manually_verified": True,
        }
        values.update(kwargs)
        return SourcePost.objects.create(**values)

    def recommend(self, runner=None, **kwargs):
        post = self.source(**kwargs)
        return Recommendation.objects.create(post=post, race=self.race, runner=runner or self.runners[0],
                                             official_match_verified=True, verifier="Matcher")

    def test_source_pending_and_canonical_duplicate(self):
        pending = self.source("pending", manually_verified=False, followers=None, summary="", verifier="")
        self.assertIsNone(pending.verified_at)
        pending.manually_verified = True
        with self.assertRaises(ValidationError):
            pending.save()
        source = self.source()
        with self.assertRaises(ValidationError):
            self.source(url="https://threads.com/@TIPSTER/post/a/?tracking=1")
        for count in (None, 1000):
            with self.assertRaises(ValidationError):
                self.source(f"bad{count}", followers=count)
        other = self.source("other", source_type="oncc", url="https://on.cc/sport/story",
                            followers=None)
        self.assertTrue(other.manually_verified)
        source.author = "Changed"
        with self.assertRaises(ValidationError):
            source.save()
        source.refresh_from_db()
        source.manually_verified = False
        source.save()
        self.assertEqual(list(VerificationEvent.objects.filter(subject="source", subject_id=source.pk)
                              .values_list("verified", flat=True)), [True, False])

    def test_eligibility_matching_cutoff_and_ranking(self):
        self.recommend(syndication_key="group")
        self.recommend(self.runners[1], suffix="b", syndication_key="GROUP")
        self.recommend(self.runners[1], suffix="c")
        self.assertEqual(rank_candidates()[0]["score"], 2)
        self.assertEqual(rank_candidates()[0]["runners"][0]["runner"], self.runners[1])
        self.assertEqual(len(rank_candidates()), 1)
        self.assertEqual(rank_candidates(limit=0), [])
        with self.assertRaises(ValidationError):
            rank_candidates(limit=6)
        self.race.is_griffin = True
        self.race.save()
        self.assertTrue(race_eligibility(self.race))
        self.assertEqual(rank_candidates(), [])
        self.race.is_griffin = False
        self.race.save()
        for runner in self.runners[:2]:
            runner.active = False
            runner.save()
        self.assertTrue(race_eligibility(self.race))

    def test_field_size_requires_nine_verified_active_runners(self):
        self.runners[8].official_verified = False
        self.runners[8].save()
        self.assertIn("At least nine active runners must be officially verified.",
                      race_eligibility(self.race))
        self.recommend()
        with self.assertRaises(ValidationError):
            approve_prediction(self.race, self.runners[:4])

    def test_publication_rejects_revoked_or_changed_evidence(self):
        rec = self.recommend()
        prediction = approve_prediction(self.race, self.runners[:4])
        cutoff = publication_cutoff(self.race)
        rec.official_match_verified = False
        rec.save()
        with patch("racing.services.timezone.now", return_value=cutoff):
            with self.assertRaises(ValidationError):
                publish_prediction(prediction)
        rec.official_match_verified = True
        rec.save()
        rec.post.manually_verified = False
        rec.post.save()
        rec.post.summary = "Changed evidence"
        rec.post.save()
        rec.post.manually_verified = True
        rec.post.save()
        with patch("racing.services.timezone.now", return_value=cutoff):
            with self.assertRaises(ValidationError):
                publish_prediction(prediction)
        self.assertEqual(prediction.inputs["recommendations"][0]["summary"], "Four runners")

    def test_source_time_window_and_meeting_cutoff(self):
        later = Race.objects.create(meeting=self.meeting, number=2, name="Later",
                                    starts_at=self.now + timedelta(hours=6), official_verified=True)
        later_runner = Runner.objects.create(race=later, number=1, name="Later horse", official_verified=True)
        post = self.source(published_at=later.starts_at - timedelta(hours=24, seconds=1),
                           retrieved_at=self.now - timedelta(minutes=30))
        rec = Recommendation.objects.create(post=post, race=later, runner=later_runner,
                                            official_match_verified=True, verifier="Matcher")
        self.assertEqual(publication_cutoff(later), self.race.starts_at - timedelta(hours=1))
        self.assertEqual(eligible_recommendations(later), [])
        with self.assertRaises(ValidationError):
            Recommendation.objects.create(post=post, race=self.race, runner=later_runner)
        post2 = self.source("future")
        with patch("racing.models.timezone.now", return_value=publication_cutoff(later)):
            rec2 = Recommendation.objects.create(post=post2, race=later, runner=later_runner,
                                                 official_match_verified=True, verifier="Matcher")
        self.assertNotIn(rec2, eligible_recommendations(later))
        self.assertNotIn(rec, eligible_recommendations(later))

    def test_approval_versions_and_publication(self):
        self.recommend()
        first = approve_prediction(self.race, self.runners[:4])
        self.assertTrue(first.inputs["first_timer_risk"])
        self.assertIn("verified field", first.inputs["approval_rationale"])
        self.assertIsNone(Publication.objects.filter(prediction=first).first())
        self.runners[0].name = "Edited"
        self.runners[0].save()
        self.assertEqual(first.inputs["selections"][0]["name"], "Horse 1")
        with self.assertRaises(ValidationError):
            first.save()
        with self.assertRaises(ValidationError):
            first.delete()
        with self.assertRaises(ValidationError):
            approve_prediction(self.race, [self.runners[0]] * 4)
        cutoff = publication_cutoff(self.race)
        with self.assertRaises(ValidationError):
            publish_prediction(first)
        with patch("racing.services.timezone.now", return_value=cutoff):
            with self.assertRaises(ValidationError):
                publish_prediction(first)
        revision = approve_prediction(self.race, self.runners[:4])
        self.assertEqual(revision.inputs["selections"][0]["name"], "Edited")
        with patch("racing.services.timezone.now", return_value=cutoff):
            published = publish_prediction(revision)
            self.assertEqual(publish_prediction(revision), published)
        with patch("racing.services.timezone.now", return_value=cutoff + timedelta(seconds=1)):
            with self.assertRaises(ValidationError):
                approve_prediction(self.race, self.runners[:4])
            late_revision = approve_prediction(self.race, self.runners[:4], allow_late_revision=True)
        self.assertEqual(late_revision.version, 3)
        self.assertEqual(first.inputs["selections"][0]["name"], "Horse 1")
        self.assertEqual(revision.inputs["selections"][0]["name"], "Edited")

    def test_result_versions_and_settlement(self):
        self.recommend()
        prediction = approve_prediction(self.race, self.runners[:4])
        with self.assertRaises(ValidationError):
            record_result(self.race, "final", [1, 2, 3], 10, 1000, 200, True)
        cancelled = record_result(self.race, "refund", official_verified=True)
        self.assertEqual(simulate_settlement(prediction, cancelled)["outcomes"]["tierce"]["payout"], 120)
        with self.assertRaises(ValidationError):
            record_result(self.race, "pending", actual_finished_at=self.now)
        self.enterContext(patch("racing.services.timezone.now",
                                return_value=self.race.starts_at + timedelta(minutes=5)))
        with self.assertRaises(ValidationError):
            record_result(self.race, "pending", actual_finished_at=self.race.starts_at + timedelta(minutes=6))
        pending = record_result(self.race, "pending", actual_finished_at=self.race.starts_at)
        self.assertIsNone(simulate_settlement(prediction, pending)["outcomes"]["tierce"]["payout"])
        result = record_result(self.race, "final", [1, 2, 3], dividend_base=10,
                               tierce_dividend=1000, trio_dividend=200, official_verified=True,
                               actual_finished_at=self.race.starts_at)
        outcome = simulate_settlement(prediction, result)["outcomes"]
        self.assertEqual((len(outcome["tierce"]["tickets"]), outcome["tierce"]["stake"],
                          outcome["tierce"]["payout"], outcome["tierce"]["roi"]),
                         (24, Decimal(120), Decimal(500), Decimal(380) / Decimal(120)))
        self.assertEqual((outcome["trio"]["stake"], outcome["trio"]["payout"]), (40, 200))
        self.assertEqual((outcome["trio_same_budget"]["stake"], outcome["trio_same_budget"]["payout"]), (120, 600))
        loss = record_result(self.race, "final", [9, 8, 7], dividend_base=10, tierce_dividend=1000,
                             trio_dividend=200, official_verified=True)
        self.assertEqual(simulate_settlement(prediction, loss)["outcomes"]["tierce"]["roi"], -1)
        refund = record_result(self.race, "refund", official_verified=True)
        self.assertEqual(simulate_settlement(prediction, refund)["outcomes"]["trio"]["roi"], 0)
        manual = record_result(self.race, "manual", notes="Withdrawal requires manual settlement")
        self.assertIsNone(simulate_settlement(prediction, manual)["outcomes"]["trio"]["roi"])
        with self.assertRaises(ValidationError):
            record_result(self.race, "final", [1, 1, 3], 10, 100, 100, True)
        with self.assertRaises(ValidationError):
            record_result(self.race, "refund")
        with self.assertRaises(ValidationError):
            record_result(self.race, "final", [1, 2, 3], 0, 100, 100, True)
        self.assertEqual(result.version, 3)
        self.assertEqual(RaceResult.objects.get(pk=result.pk).inputs["finishing_numbers"], [1, 2, 3])

    def test_actual_bet_audit(self):
        bet = ActualBet.objects.create(race=self.race, placed_at=self.now, description="Ticket", stake=10)
        bet.refund_amount = Decimal("10")
        bet.save()
        self.assertEqual(list(bet.events.values_list("inputs", flat=True))[1]["refund_amount"], "10")

    def test_admin_approval_and_snapshot_read_only(self):
        self.recommend()
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password")
        self.client.force_login(user)
        ranking = self.client.post(reverse("admin:racing_race_changelist"), {
            "action": "show_candidate_ranking", "_selected_action": [self.race.pk],
        }, follow=True)
        self.assertContains(ranking, "1 independent sources")
        response = self.client.post(reverse("admin:racing_runner_changelist"), {
            "action": "approve_selected", "_selected_action": [r.pk for r in self.runners[:4]],
        })
        self.assertEqual(response.status_code, 302)
        prediction = Prediction.objects.get()
        self.assertEqual(self.client.get(reverse("admin:racing_prediction_add")).status_code, 403)
        response = self.client.post(reverse("admin:racing_prediction_change", args=[prediction.pk]),
                                    {"version": 999, "inputs": '{"selections": []}'})
        prediction.refresh_from_db()
        self.assertEqual(prediction.version, 1)
        self.assertEqual(len(prediction.inputs["selections"]), 4)
        self.assertEqual(response.status_code, 403)
        with patch("racing.services.timezone.now", return_value=publication_cutoff(self.race)):
            response = self.client.post(reverse("admin:racing_prediction_changelist"), {
                "action": "publish_selected", "_selected_action": [prediction.pk],
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Publication.objects.filter(prediction=prediction).exists())
        with patch("racing.services.timezone.now", return_value=self.race.starts_at + timedelta(minutes=1)):
            response = self.client.post(reverse("admin:racing_raceresult_add"), {
                "race": self.race.pk, "status": "final", "first": 1, "second": 2, "third": 3,
                "dividend_base": "10", "tierce_dividend": "1000", "trio_dividend": "200",
                "official_verified": "on", "notes": "",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(simulate_settlement(prediction, RaceResult.objects.get())["outcomes"]["tierce"]["payout"], 500)

    def test_withdrawal_and_schedule_change_fail_closed(self):
        self.recommend()
        prediction = approve_prediction(self.race, self.runners[:4])
        cutoff = publication_cutoff(self.race)
        with patch("racing.services.timezone.now", return_value=self.race.starts_at + timedelta(minutes=1)):
            result = record_result(self.race, "final", [5, 6, 7], 10, 1000, 200, True)
        self.runners[0].active = False
        self.runners[0].save()
        with patch("racing.services.timezone.now", return_value=cutoff):
            with self.assertRaises(ValidationError):
                publish_prediction(prediction)
        settlement = simulate_settlement(prediction, result)
        self.assertEqual(settlement["status"], "manual")
        self.assertIsNone(settlement["outcomes"]["tierce"]["payout"])
        with patch("racing.services.timezone.now", return_value=self.race.starts_at + timedelta(minutes=1)):
            refund = record_result(self.race, "refund", official_verified=True)
        self.assertIsNone(simulate_settlement(prediction, refund)["outcomes"]["trio"]["payout"])
        self.runners[0].active = True
        self.runners[0].save()
        self.assertEqual(simulate_settlement(prediction, result)["status"], "manual")
        prediction = approve_prediction(self.race, self.runners[:4])
        self.race.starts_at += timedelta(minutes=10)
        self.race.save()
        with patch("racing.services.timezone.now", return_value=cutoff + timedelta(minutes=10)):
            with self.assertRaises(ValidationError):
                publish_prediction(prediction)

    def test_publication_rejects_changed_race_and_meeting_identity(self):
        self.recommend()
        prediction = approve_prediction(self.race, self.runners[:4])
        cutoff = publication_cutoff(self.race)
        for obj, field, changed in (
            (self.race, "name", "Renamed race"),
            (self.meeting, "venue", "Happy Valley"),
            (self.meeting, "date", self.meeting.date + timedelta(days=1)),
        ):
            original = getattr(obj, field)
            setattr(obj, field, changed)
            obj.save()
            with patch("racing.services.timezone.now", return_value=cutoff):
                with self.assertRaises(ValidationError):
                    publish_prediction(prediction)
            setattr(obj, field, original)
            obj.save()

    def test_meeting_limit_five_distinct_races(self):
        for i in range(1, 7):
            race = self.race if i == 1 else Race.objects.create(
                meeting=self.meeting, number=i, name=f"Race {i}",
                starts_at=self.now + timedelta(hours=4, minutes=i), official_verified=True,
                approval_rationale="Race and runners reviewed.")
            runners = self.runners if i == 1 else [
                Runner.objects.create(race=race, number=n, name=f"Horse {n}", official_verified=True)
                for n in range(1, 10)
            ]
            post = self.source(f"limit{i}")
            Recommendation.objects.create(post=post, race=race, runner=runners[0],
                                          official_match_verified=True, verifier="Matcher")
            if i <= 5:
                approve_prediction(race, runners[:4])
            else:
                with self.assertRaises(ValidationError):
                    approve_prediction(race, runners[:4])
