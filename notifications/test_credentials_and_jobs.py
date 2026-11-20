import io
import os
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from racing.models import ActualBet, Meeting, Race
from .credentials import CredentialsFileError, credential_values
from .models import JobRun, Notification
from .services import ConfigurationError, enqueue_notification, process_notifications


TOKEN = "123456:secret_token"
CHAT = "-100123"


class CredentialsTests(TestCase):
    def setUp(self):
        self.path = Path(__file__).parent / (".test-credentials-" + uuid.uuid4().hex)
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.file_patch = patch("notifications.credentials.CREDENTIALS_FILE", self.path)
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)

    def write_file(self, contents, mode=0o600):
        self.path.write_bytes(contents)
        self.path.chmod(mode)

    @patch.dict(os.environ, {}, clear=True)
    def test_literal_file_and_environment_precedence(self):
        self.write_file(f"# local\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n".encode())
        self.assertEqual(credential_values(), (TOKEN, CHAT))
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "@other_chat"}):
            self.assertEqual(credential_values(), (TOKEN, "@other_chat"))
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(ConfigurationError):
                process_notifications()

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_blank_and_unsafe_files_fail_without_claim(self):
        item = enqueue_notification("pending", "hello")
        for contents, mode in [(None, None), (b"TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", 0o600),
                               (f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n".encode(), 0o644)]:
            if contents is not None:
                self.write_file(contents, mode)
            with self.assertRaises(ConfigurationError) as exc:
                process_notifications()
            self.assertNotIn(TOKEN, str(exc.exception))
            item.refresh_from_db()
            self.assertEqual((item.status, item.attempts), ("pending", 0))
        self.assertEqual(JobRun.objects.filter(status="failed", error="invalid_credentials").count(), 3)

    @patch.dict(os.environ, {}, clear=True)
    def test_rejects_malformed_files_without_interpreting_values(self):
        for contents in [
            b"TELEGRAM_BOT_TOKEN=one\nTELEGRAM_BOT_TOKEN=two\n",
            b"export TELEGRAM_BOT_TOKEN=secret\n",
            b"OTHER=secret\n",
            b"\xff",
            b"x" * 4097,
        ]:
            with self.subTest(contents=contents[:30]):
                self.write_file(contents)
                with self.assertRaises(CredentialsFileError) as exc:
                    credential_values()
                self.assertNotIn("secret", str(exc.exception))
        self.write_file(b"TELEGRAM_BOT_TOKEN=$(touch never-created)\nTELEGRAM_CHAT_ID=1\n")
        with self.assertRaises(ConfigurationError):
            process_notifications()
        self.assertFalse(Path("never-created").exists())

    @patch.dict(os.environ, {}, clear=True)
    def test_symlink_and_fifo_rejected_and_complete_environment_bypasses_file(self):
        self.path.symlink_to("telegram.credentials.example")
        with self.assertRaises(CredentialsFileError):
            credential_values()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}):
            self.assertEqual(credential_values(), (TOKEN, CHAT))
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(CredentialsFileError):
            credential_values()

    @patch.dict(os.environ, {}, clear=True)
    def test_hardlinked_file_is_rejected(self):
        other = self.path.with_name(self.path.name + "-link")
        self.addCleanup(other.unlink, missing_ok=True)
        self.write_file(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID={CHAT}\n".encode())
        os.link(self.path, other)
        with self.assertRaises(CredentialsFileError):
            credential_values()


@patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
class IntegratedJobTests(TestCase):
    def setUp(self):
        now = timezone.now()
        meeting = Meeting.objects.create(date=now.date(), venue="Test")
        self.race = Race.objects.create(meeting=meeting, number=1, name="Test", starts_at=now - timedelta(hours=1))
        ActualBet.objects.create(race=self.race, placed_at=now - timedelta(hours=2), description="Manual", stake="10")

    def test_real_racing_queue_then_send_is_idempotent(self):
        out = io.StringIO()
        with patch("notifications.services._send", return_value=("sent", "", 123)) as send:
            call_command("run_racing_and_notifications", race_ids=[self.race.pk], stdout=out)
            call_command("run_racing_and_notifications", race_ids=[self.race.pk], stdout=out)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(status="sent").count(), 2)
        self.assertEqual(send.call_count, 2)
        self.assertIn("attempted=2 sent=2", out.getvalue())
        self.assertIn("attempted=0 sent=0", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())

    def test_racing_failure_never_sends_or_leaks_exception(self):
        enqueue_notification("pending", "hello")
        with patch("notifications.management.commands.run_racing_and_notifications.call_command",
                   side_effect=RuntimeError(TOKEN)), patch("notifications.services._send") as send:
            with self.assertRaises(CommandError) as exc:
                call_command("run_racing_and_notifications")
        self.assertNotIn(TOKEN, str(exc.exception))
        self.assertIn("notifications were not processed", str(exc.exception))
        send.assert_not_called()
        self.assertEqual(Notification.objects.get().status, "pending")
        self.assertFalse(JobRun.objects.exists())

    def test_invalid_race_stops_before_notification_processing(self):
        enqueue_notification("pending", "hello")
        with patch("notifications.services._send") as send:
            with self.assertRaisesMessage(CommandError, "notifications were not processed"):
                call_command("run_racing_and_notifications", race_ids=[-1], stdout=io.StringIO())
        send.assert_not_called()
        self.assertEqual(Notification.objects.get().status, "pending")
        self.assertFalse(JobRun.objects.exists())

    def test_missing_config_retains_queued_notifications(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaisesMessage(CommandError, "credentials are missing"):
                call_command("run_racing_and_notifications", race_ids=[self.race.pk], stdout=io.StringIO())
        self.assertTrue(Notification.objects.filter(status="pending").exists())
        self.assertEqual(JobRun.objects.get().error, "invalid_credentials")

    def test_delivery_failure_exits_nonzero_and_preserves_outbox_status(self):
        with patch("notifications.services._send", return_value=("uncertain", "transport_error", None)):
            with self.assertRaisesMessage(CommandError, "uncertain=1"):
                call_command("run_racing_and_notifications", race_ids=[self.race.pk], limit=1, stdout=io.StringIO())
        self.assertEqual(Notification.objects.filter(status="uncertain").count(), 1)
        self.assertEqual(Notification.objects.filter(status="pending").count(), 1)

    def test_bad_notification_options_fail_before_racing(self):
        with self.assertRaisesMessage(CommandError, "Invalid notification processing options"):
            call_command("run_racing_and_notifications", timeout=float("nan"))
        self.assertFalse(Notification.objects.exists())
