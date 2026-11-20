from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from notifications.models import JobRun, Notification

from .models import ActualBet, Meeting, Prediction, Publication, Race, RaceResult, Runner

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("racing.urls")),
]


@override_settings(ROOT_URLCONF="racing.test_dashboard")
class DashboardTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(username="superuser")
        self.non_superuser_staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.user = get_user_model().objects.create_user(username="user")
        meeting = Meeting.objects.create(date=timezone.localdate(), venue="Sha Tin")
        self.race = Race.objects.create(
            meeting=meeting, number=1, name="Test", starts_at=timezone.now() + timedelta(days=1)
        )

    def prediction(self, version, numbers=(1, 2, 3, 4), published=True):
        runners = {
            number: Runner.objects.get_or_create(
                race=self.race, number=number, defaults={"name": "Horse"}
            )[0]
            for number in numbers
        }
        prediction = Prediction.objects.create(
            race=self.race, version=version, approved_at=timezone.now(),
            inputs={"selections": [
                {"id": runners[number].pk, "number": number,
                 "name": "<script>alert(1)</script>" if number == 1 else "Horse"}
                for number in numbers
            ], "first_timer_risk": True},
        )
        if published:
            Publication.objects.create(prediction=prediction, published_at=timezone.now())
        return prediction

    def result(self, version, status="final", finish=(1, 2, 3)):
        return RaceResult.objects.create(
            race=self.race, version=version, recorded_at=timezone.now(),
            inputs={
                "status": status, "finishing_numbers": list(finish),
                "dividend_base": "10", "tierce_dividend": "200", "trio_dividend": "30",
            },
        )

    def test_dashboard_requires_staff(self):
        url = reverse("dashboard")
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('admin:login')}?next={url}", fetch_redirect_response=False)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.non_superuser_staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_first_published_version_and_latest_result_only(self):
        self.prediction(1)
        self.prediction(2, numbers=(5, 6, 7, 8))
        self.result(1, finish=(5, 6, 7))
        self.result(2)
        ActualBet.objects.create(
            race=self.race, placed_at=timezone.now(), description="Settled",
            stake=Decimal("100"), payout=Decimal("150"), refund_amount=Decimal("10"),
        )
        ActualBet.objects.create(
            race=self.race, placed_at=timezone.now(), description="Pending",
            stake=Decimal("1000"), refund_amount=Decimal("50"),
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(len(response.context["simulations"]), 1)
        row = response.context["simulations"][0]
        self.assertEqual(row["settlement"]["prediction_version"], 1)
        self.assertEqual(row["settlement"]["result_version"], 2)
        self.assertEqual(response.context["tierce"]["stake"], Decimal("120"))
        self.assertEqual(response.context["tierce"]["payout"], Decimal("100"))
        self.assertEqual(response.context["trio"]["stake"], Decimal("40"))
        self.assertEqual(response.context["trio"]["payout"], Decimal("30"))
        self.assertEqual(response.context["trio_same_budget"]["stake"], Decimal("120"))
        self.assertEqual(response.context["trio_same_budget"]["payout"], Decimal("90"))
        self.assertEqual(response.context["hit_count"], 1)
        self.assertEqual(response.context["hit_rate_percent"], Decimal("100"))
        self.assertEqual(response.context["tierce"]["winning_races"], 1)
        self.assertEqual(response.context["tierce"]["profitable_races"], 0)
        self.assertEqual(response.context["actual"]["roi_percent"], Decimal("60"))
        self.assertEqual(response.context["actual"]["stake"], Decimal("100"))
        self.assertEqual(response.context["pending_bets"], 1)
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;", html=False)
        self.assertNotContains(response, "<script>alert(1)</script>", html=False)

    def test_notification_status_is_visible_to_staff(self):
        Notification.objects.create(idempotency_key="one", text="test", status=Notification.Status.FAILED)
        JobRun.objects.create(status=JobRun.Status.FAILED)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Failed: 1")
        self.assertContains(response, "最近一次處理工作：")
        self.assertContains(response, "通知管理")
        self.assertContains(response, 'lang="zh-Hant"')

    def test_pending_manual_and_unpublished_are_not_settled(self):
        self.prediction(1)
        self.result(1)
        self.result(2, status="manual")
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["tierce"]["count"], 0)
        self.assertEqual(response.context["final_races"], 0)
        self.assertContains(response, "manual")
        self.assertContains(response, "賽果不足")

    def test_first_publication_may_be_later_revision(self):
        first_version = self.prediction(1, published=False)
        self.prediction(2)
        Publication.objects.create(prediction=first_version, published_at=timezone.now())
        self.result(1)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(len(response.context["simulations"]), 1)
        self.assertEqual(response.context["simulations"][0]["prediction"].version, 2)

    def test_missing_result_is_pending(self):
        self.prediction(1)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["simulations"][0]["settlement"]["status"], "pending")
        self.assertContains(response, "尚無賽果")
        self.assertEqual(response.context["final_races"], 0)

    def test_withdrawn_runner_requires_manual_settlement(self):
        self.prediction(1)
        self.result(1)
        runner = self.race.runners.get(number=1)
        runner.active = False
        runner.save()
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["final_races"], 0)
        self.assertEqual(response.context["tierce"]["winning_races"], 0)
        self.assertContains(response, "須按投注組合人手結算")

    def test_refund_result_is_settled_without_profit(self):
        self.prediction(1)
        self.result(1, status="refund")
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["tierce"]["payout"], Decimal("120"))
        self.assertEqual(response.context["tierce"]["roi_percent"], Decimal("0"))
        self.assertEqual(response.context["tierce"]["winning_races"], 0)
        self.assertEqual(response.context["tierce"]["profitable_races"], 0)
        self.assertEqual(response.context["final_races"], 0)

    def test_hit_rate_and_profit_only_count_final_races(self):
        self.prediction(1)
        self.result(1)
        self.race = Race.objects.create(
            meeting=self.race.meeting, number=2, name="Second",
            starts_at=timezone.now() + timedelta(days=1),
        )
        self.prediction(1, numbers=(5, 6, 7, 8))
        self.result(1, finish=(1, 2, 3))
        self.race = Race.objects.create(
            meeting=self.race.meeting, number=3, name="Third",
            starts_at=timezone.now() + timedelta(days=1),
        )
        self.prediction(1)
        RaceResult.objects.create(
            race=self.race, version=1, recorded_at=timezone.now(),
            inputs={"status": "final", "finishing_numbers": [1, 2, 3],
                    "dividend_base": "10", "tierce_dividend": "400", "trio_dividend": "100"},
        )
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["hit_count"], 2)
        self.assertEqual(response.context["final_races"], 3)
        self.assertEqual(response.context["tierce"]["winning_races"], 2)
        self.assertEqual(response.context["tierce"]["profitable_races"], 1)
        self.assertContains(response, "66.67%")

    @patch("racing.views.rank_candidates")
    def test_candidates_and_risk_are_displayed_safely(self, rank):
        runner = Runner(race=self.race, number=1, name="<b>Runner</b>", first_time=True)
        rank.return_value = [{"race": self.race, "score": 2, "runners": [{"runner": runner, "score": 1}]}]
        self.client.force_login(self.staff)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "2 個獨立來源")
        self.assertContains(response, "&lt;b&gt;Runner&lt;/b&gt;", html=False)
        self.assertContains(response, "初出賽馬匹風險")
