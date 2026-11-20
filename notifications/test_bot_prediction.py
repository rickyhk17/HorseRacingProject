from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from racing.models import Meeting, Race, Recommendation, Runner, SourcePost

from .bot_prediction import candidate_summary


class CandidateSummaryTests(TestCase):
    def setUp(self):
        now = timezone.now()
        meeting = Meeting.objects.create(date=timezone.localdate(), venue="ST", official_verified=True)
        self.race = Race.objects.create(
            meeting=meeting, number=1, name="Test", starts_at=now + timedelta(hours=4),
            official_verified=True,
        )
        self.runners = [
            Runner.objects.create(race=self.race, number=i, name=f"Horse {i}", official_verified=True,
                                  first_time=i == 1)
            for i in range(1, 10)
        ]
        self.source = SourcePost.objects.create(
            source_type="oncc", url="https://racing.on.cc/article",
            author="Tipster", summary="Selections", verifier="Operator",
            published_at=now - timedelta(hours=1),
            retrieved_at=now - timedelta(minutes=30), manually_verified=True,
        )

    def add_recommendations(self, count):
        for runner in self.runners[:count]:
            Recommendation.objects.create(post=self.source, race=self.race, runner=runner,
                                          official_match_verified=True, verifier="Operator")

    def test_requires_four_supported_runners(self):
        self.add_recommendations(3)
        self.assertIn("資料不足", candidate_summary())

    def test_returns_candidates_and_first_timer_warning(self):
        self.add_recommendations(4)
        text = candidate_summary()
        self.assertIn("候選預測（未批准）", text)
        self.assertIn("1 Horse 1", text)
        self.assertIn("4 Horse 4", text)
        self.assertIn("https://racing.on.cc/article", text)
        self.assertIn("首次出賽", text)
        self.assertIn("少於三場", text)

    def test_ineligible_race_is_excluded(self):
        self.add_recommendations(4)
        self.race.is_griffin = True
        self.race.save()
        self.assertIn("資料不足", candidate_summary())

    def test_filter_supported_runners_before_limit(self):
        self.add_recommendations(4)
        for number in range(2, 7):
            race = Race.objects.create(
                meeting=self.race.meeting, number=number, name="Earlier",
                starts_at=self.race.starts_at - timedelta(minutes=number), official_verified=True,
            )
            runners = [
                Runner.objects.create(race=race, number=i, name=f"Horse {i}", official_verified=True)
                for i in range(1, 10)
            ]
            Recommendation.objects.create(post=self.source, race=race, runner=runners[0],
                                          official_match_verified=True, verifier="Operator")
        self.assertIn("第 1 場", candidate_summary())
