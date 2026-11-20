import io
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import JobRun, Notification
from .services import (
    ConfigurationError,
    IdempotencyConflict,
    ProcessingError,
    _send,
    enqueue_notification,
    process_notifications,
)


CREDS = {"TELEGRAM_BOT_TOKEN": "123456:secret_token", "TELEGRAM_CHAT_ID": "-100123"}


class EnqueueTests(TestCase):
    def test_key_is_idempotent_and_conflict_is_rejected(self):
        first = enqueue_notification("race:1", "Hello")
        self.assertEqual(enqueue_notification("race:1", "Hello").pk, first.pk)
        with self.assertRaises(IdempotencyConflict):
            enqueue_notification("race:1", "Different")
        self.assertEqual(Notification.objects.count(), 1)

    def test_invalid_inputs(self):
        for key, text in [("", "hello"), ("a" * 256, "hello"), ("id", ""), ("id", "a" * 4097)]:
            with self.subTest(key=key[:10], text=text[:10]), self.assertRaises(ValueError):
                enqueue_notification(key, text)


@patch.dict("os.environ", CREDS)
class ProcessTests(TestCase):
    def test_success_and_repeated_process_do_not_resend(self):
        item = enqueue_notification("one", "hello")
        with patch("notifications.services._send", return_value=("sent", "", 42)) as send:
            run = process_notifications()
            again = process_notifications()
        item.refresh_from_db()
        self.assertEqual((item.status, item.attempts, item.telegram_message_id), ("sent", 1, 42))
        self.assertIsNotNone(item.sent_at)
        self.assertEqual((run.status, run.attempted, run.sent), ("succeeded", 1, 1))
        self.assertEqual(again.attempted, 0)
        send.assert_called_once()

    def test_overlapping_worker_cannot_claim_inflight_item(self):
        enqueue_notification("one", "hello")

        def send(*args):
            other = process_notifications()
            self.assertEqual(other.attempted, 0)
            return "sent", "", 42

        with patch("notifications.services._send", side_effect=send) as mocked:
            run = process_notifications()
        self.assertEqual(run.sent, 1)
        mocked.assert_called_once()

    def test_retry_only_after_due_and_exhaustion_fails(self):
        item = enqueue_notification("one", "hello")
        with patch("notifications.services._send", return_value=("retry", "telegram_rejected_retryable", None)) as send:
            first = process_notifications(max_attempts=2, retry_delay=60)
            self.assertEqual(first.retried, 1)
            self.assertEqual(first.status, "failed")
            self.assertEqual(process_notifications().attempted, 0)
            Notification.objects.filter(pk=item.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            second = process_notifications(max_attempts=2)
        item.refresh_from_db()
        self.assertEqual((item.status, item.attempts), ("failed", 2))
        self.assertEqual((second.failed, second.status), (1, "failed"))
        self.assertEqual(send.call_count, 2)

    def test_ambiguous_and_expired_sends_are_never_retried(self):
        item = enqueue_notification("one", "hello")
        stale = enqueue_notification("two", "hello")
        Notification.objects.filter(pk=stale.pk).update(
            status="sending", claim_id=uuid.uuid4(),
            claimed_at=timezone.now() - timedelta(hours=1),
        )
        with patch("notifications.services._send", return_value=("uncertain", "transport_error", None)) as send:
            run = process_notifications(lease_seconds=10)
            process_notifications()
        item.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual((item.status, stale.status, stale.last_error), ("uncertain", "uncertain", "expired_claim"))
        self.assertEqual(run.uncertain, 2)
        send.assert_called_once()

    def test_permanent_rejection_and_limit(self):
        enqueue_notification("one", "hello")
        enqueue_notification("two", "hello")
        with patch("notifications.services._send", return_value=("failed", "telegram_rejected", None)):
            run = process_notifications(limit=1)
        self.assertEqual((run.failed, run.status), (1, "failed"))
        self.assertEqual(Notification.objects.filter(status="pending").count(), 1)

    def test_missing_credentials_record_failed_run_without_claiming(self):
        enqueue_notification("one", "hello")
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(ConfigurationError):
                process_notifications()
        run = JobRun.objects.get()
        self.assertEqual((run.status, run.error), ("failed", "invalid_credentials"))
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(Notification.objects.get().status, "pending")

    def test_unexpected_error_is_recorded_without_secret(self):
        enqueue_notification("one", "hello")
        with patch("notifications.services._send", side_effect=RuntimeError("secret_token")):
            with self.assertRaises(ProcessingError) as exc:
                process_notifications()
        self.assertNotIn("secret_token", str(exc.exception))
        run = JobRun.objects.get()
        self.assertEqual((run.status, run.error), ("failed", "processing_error"))
        self.assertNotIn("secret_token", run.error)
        self.assertEqual(Notification.objects.get().status, "sending")

    def test_command_exit_status_reports_failure_and_hides_exceptions(self):
        enqueue_notification("one", "hello")
        with patch("notifications.services._send", return_value=("failed", "telegram_rejected", None)):
            with self.assertRaisesMessage(CommandError, "failed=1"):
                call_command("process_notifications")
        with patch("notifications.management.commands.process_notifications.process_notifications",
                   side_effect=RuntimeError("secret_token")):
            with self.assertRaises(CommandError) as exc:
                call_command("process_notifications")
        self.assertNotIn("secret_token", str(exc.exception))

    def test_command_success(self):
        out = io.StringIO()
        call_command("process_notifications", stdout=out)
        self.assertIn("attempted=0", out.getvalue())


class TelegramTests(TestCase):
    def response(self, status, body):
        response = MagicMock()
        response.status = status
        response.read.return_value = json.dumps(body).encode()
        response.__enter__.return_value = response
        return response

    @patch("notifications.services.build_opener")
    def test_success_uses_fixed_https_endpoint_no_proxy_or_redirect(self, build):
        build.return_value.open.return_value = self.response(200, {"ok": True, "result": {"message_id": 123}})
        self.assertEqual(_send(CREDS["TELEGRAM_BOT_TOKEN"], CREDS["TELEGRAM_CHAT_ID"], "hello", 3), ("sent", "", 123))
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123456:secret_token/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 3)

    @patch("notifications.services.build_opener")
    def test_rejections_and_ambiguous_responses(self, build):
        opener = build.return_value
        for status, body, outcome in [
            (429, {"ok": False}, "retry"),
            (503, {"ok": False}, "retry"),
            (400, {"ok": False}, "failed"),
            (200, {"ok": True, "result": {}}, "uncertain"),
            (200, {"ok": False}, "failed"),
            (302, {}, "uncertain"),
        ]:
            with self.subTest(status=status, body=body):
                opener.open.return_value = self.response(status, body)
                self.assertEqual(_send("token", "chat", "text", 3)[0], outcome)
        opener.open.side_effect = URLError("secret_token")
        self.assertEqual(_send("token", "chat", "text", 3), ("uncertain", "transport_error", None))
        opener.open.side_effect = HTTPError("https://example.invalid", 429, "", {}, io.BytesIO(b'{"ok": false}'))
        self.assertEqual(_send("token", "chat", "text", 3)[0], "retry")


class AdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="admin")
        self.client.force_login(self.user)
        self.notification = Notification.objects.create(
            idempotency_key="uncertain-key", text="visible text",
            status=Notification.Status.UNCERTAIN, last_error="transport_error",
        )
        self.run = JobRun.objects.create(status=JobRun.Status.FAILED, error="processing_error")

    def test_lists_and_details_show_failures_and_uncertain_items(self):
        for model, instance, visible in [
            ("notification", self.notification, ["uncertain-key", "Delivery uncertain", "transport_error"]),
            ("jobrun", self.run, ["Failed", "processing_error"]),
        ]:
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:notifications_{model}_changelist"))
                self.assertEqual(response.status_code, 200)
                for value in visible:
                    self.assertContains(response, value)
                response = self.client.get(reverse(f"admin:notifications_{model}_change", args=[instance.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, visible[-1])

    def test_superuser_cannot_add_change_or_delete(self):
        for model, instance in [("notification", self.notification), ("jobrun", self.run)]:
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:notifications_{model}_add")).status_code, 403)
                self.assertEqual(self.client.post(
                    reverse(f"admin:notifications_{model}_change", args=[instance.pk]), {"status": "sent"},
                ).status_code, 403)
                self.assertEqual(self.client.post(
                    reverse(f"admin:notifications_{model}_delete", args=[instance.pk]), {"post": "yes"},
                ).status_code, 403)
        self.notification.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.notification.status, Notification.Status.UNCERTAIN)
        self.assertEqual(self.run.status, JobRun.Status.FAILED)

    def test_view_permission_allows_read_only_access(self):
        user = get_user_model().objects.create_user(username="viewer", is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="notifications", codename__in=["view_notification", "view_jobrun"]
        ))
        self.client.force_login(user)
        for model, instance in [("notification", self.notification), ("jobrun", self.run)]:
            self.assertEqual(self.client.get(reverse(f"admin:notifications_{model}_changelist")).status_code, 200)
            self.assertEqual(self.client.get(
                reverse(f"admin:notifications_{model}_change", args=[instance.pk]),
            ).status_code, 200)
