import io
import json
import sys
import types
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .commands_handler import ReceiverError, _get_updates, _store_updates, run_receiver
from .models import Notification, TelegramCommand, TelegramPollLock, TelegramPollState


ENV = {"TELEGRAM_BOT_TOKEN": "123:secret_token", "TELEGRAM_CHAT_ID": "1234", "TELEGRAM_USER_ID": "5678"}


def update(number, text="/racedata", *, chat=1234, user=5678, kind="private", is_bot=False):
    return {"update_id": number, "message": {
        "chat": {"id": chat, "type": kind}, "from": {"id": user, "is_bot": is_bot}, "text": text,
    }}


@patch.dict("os.environ", ENV)
class ReceiverTests(TestCase):
    def run_with(self, updates, handler):
        module = types.ModuleType("notifications.bot_commands")
        module.handle_command = handler
        with patch.dict(sys.modules, {"notifications.bot_commands": module}), \
                patch("notifications.commands_handler._get_updates", return_value=updates), \
                patch("notifications.services._send", return_value=("sent", "", 1)):
            run_receiver(once=True)

    def test_authorization_and_unknown_commands(self):
        handler = MagicMock(return_value="answer")
        updates = [
            update(1), update(2, "/result x"), update(3, "/predit"), update(4, "/unknown"),
            update(5, chat=9), update(6, user=9), update(7, kind="group"),
            update(8, is_bot=True), update(9, text="hello"),
            update(10, user=True), {"update_id": 11, "edited_message": update(11)["message"]},
        ]
        self.run_with(updates, handler)
        self.assertEqual([c.args[0] for c in handler.call_args_list], ["/racedata", "/result x", "/predit", "/unknown"])
        self.assertEqual(TelegramCommand.objects.count(), 4)
        self.assertEqual(TelegramPollState.objects.get().next_offset, 12)
        self.assertEqual(Notification.objects.filter(status="sent").count(), 8)
        self.assertEqual(Notification.objects.filter(status="sent", text="answer").count(), 4)

    def test_progress_is_sent_before_handler_and_duplicate_never_runs(self):
        def handler(text):
            self.assertEqual(Notification.objects.get(idempotency_key="telegram-command:42:progress").status, "sent")
            return "finished"

        mock = MagicMock(side_effect=handler)
        self.run_with([update(42)], mock)
        self.run_with([update(42)], mock)
        mock.assert_called_once_with("/racedata")
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(TelegramCommand.objects.get().status, "succeeded")

    def test_failed_handler_is_not_replayed_or_logged(self):
        handler = MagicMock(side_effect=RuntimeError("secret_token"))
        self.run_with([update(7)], handler)
        self.run_with([update(7)], handler)
        handler.assert_called_once()
        self.assertEqual(TelegramCommand.objects.get().status, "failed")
        self.assertNotIn("secret_token", " ".join(Notification.objects.values_list("text", flat=True)))

    def test_crashed_worker_requires_manual_lock_removal_then_marks_uncertain(self):
        TelegramPollState.objects.create(chat_id=1234, user_id=5678, next_offset=8, last_update_at=timezone.now())
        TelegramCommand.objects.create(update_id=7, text="/racedata", status="running")
        TelegramPollLock.objects.create()
        with self.assertRaises(ReceiverError):
            run_receiver(once=True)
        TelegramPollLock.objects.all().delete()
        handler = MagicMock(return_value="wrong")
        self.run_with([update(7)], handler)
        handler.assert_not_called()
        self.assertEqual(TelegramCommand.objects.get().status, "uncertain")
        self.assertIn("uncertain", Notification.objects.get().text)

    def test_crash_after_start_is_not_reexecuted(self):
        handler = MagicMock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_with([update(1)], handler)
        self.assertEqual(TelegramCommand.objects.get().status, "running")
        self.run_with([update(1)], handler)
        self.assertEqual(TelegramCommand.objects.get().status, "uncertain")
        handler.assert_called_once()

    def test_lock_blocks_overlapping_worker(self):
        def handler(text):
            with self.assertRaises(ReceiverError):
                run_receiver(once=True)
            return "done"
        self.run_with([update(1)], handler)
        self.assertFalse(TelegramPollLock.objects.exists())

    def test_config_rejects_non_numeric_and_identity_changes(self):
        for key, value in [("TELEGRAM_CHAT_ID", "@name"), ("TELEGRAM_CHAT_ID", "-100"), ("TELEGRAM_USER_ID", "0")]:
            with self.subTest(key=key), patch.dict("os.environ", {key: value}), self.assertRaises((ReceiverError, ValueError)):
                run_receiver(once=True)
        TelegramPollState.objects.create(chat_id=1, user_id=5678)
        with self.assertRaises(ReceiverError), patch("notifications.commands_handler._get_updates") as poll:
            run_receiver(once=True)
        poll.assert_not_called()

    def test_chat_can_come_from_credentials_file(self):
        with patch("notifications.credentials.read_credentials_file", return_value={"TELEGRAM_CHAT_ID": "1234"}):
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ENV["TELEGRAM_BOT_TOKEN"],
                                           "TELEGRAM_USER_ID": "5678"}, clear=True):
                self.run_with([update(1)], MagicMock(return_value="done"))
        self.assertEqual(TelegramCommand.objects.get().status, "succeeded")
        self.assertEqual(Notification.objects.filter(status="sent").count(), 2)

    def test_completion_outbox_retry_is_durable(self):
        module = types.ModuleType("notifications.bot_commands")
        module.handle_command = MagicMock(return_value="done")
        with patch.dict(sys.modules, {"notifications.bot_commands": module}), \
                patch("notifications.commands_handler._get_updates", return_value=[update(1)]), \
                patch("notifications.services._send", side_effect=[
                    ("sent", "", 1), ("retry", "telegram_rejected_retryable", None),
                ]):
            run_receiver(once=True)
        self.assertEqual(TelegramCommand.objects.get().status, "succeeded")
        self.assertEqual(Notification.objects.get(text="done").status, "pending")
        module.handle_command.assert_called_once()
        Notification.objects.filter(text="done").update(next_attempt_at=timezone.now())
        self.run_with([update(1)], module.handle_command)
        self.assertEqual(Notification.objects.get(text="done").status, "sent")
        module.handle_command.assert_called_once()

    def test_batch_and_offset_are_atomic(self):
        TelegramPollState.objects.create(chat_id=1234, user_id=5678)
        with patch("notifications.commands_handler.TelegramCommand.objects.get_or_create", side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                _store_updates([update(1)], 1234, 5678)
        self.assertEqual(TelegramPollState.objects.get().next_offset, 0)
        self.assertFalse(TelegramCommand.objects.exists())

    def test_idle_cursor_stops_before_poll_and_never_acks_lower_new_ids(self):
        state = TelegramPollState.objects.create(
            chat_id=1234, user_id=5678, next_offset=100,
            last_update_at=timezone.now() - timedelta(days=5),
        )
        with patch("notifications.commands_handler._get_updates", return_value=[update(1)]) as poll:
            with self.assertRaisesMessage(ReceiverError, "cursor is too old"):
                run_receiver(once=True)
        poll.assert_not_called()
        state.refresh_from_db()
        self.assertEqual(state.next_offset, 100)
        self.assertFalse(TelegramCommand.objects.exists())
        self.assertFalse(TelegramPollLock.objects.exists())

    def test_empty_polls_do_not_extend_idle_and_timestamp_survives_restart(self):
        now = timezone.now()
        self.run_with([update(1)], MagicMock(return_value="done"))
        state = TelegramPollState.objects.get()
        self.assertGreaterEqual(state.last_update_at, now)
        last_update_at = state.last_update_at
        self.run_with([], MagicMock(return_value="done"))
        state.refresh_from_db()
        self.assertEqual(state.last_update_at, last_update_at)
        self.assertEqual(state.next_offset, 2)

    def test_unrecognized_update_still_advances_idle_timestamp(self):
        state = TelegramPollState.objects.create(
            chat_id=1234, user_id=5678, next_offset=5,
            last_update_at=timezone.now() - timedelta(days=4, hours=23),
        )
        self.run_with([update(5, user=9)], MagicMock(return_value="unused"))
        state.refresh_from_db()
        self.assertEqual(state.next_offset, 6)
        self.assertLess(timezone.now() - state.last_update_at, timedelta(minutes=1))
        self.assertFalse(TelegramCommand.objects.exists())

    def test_work_delaying_poll_across_idle_limit_is_rechecked(self):
        state = TelegramPollState.objects.create(
            chat_id=1234, user_id=5678, next_offset=5, last_update_at=timezone.now(),
        )

        def delay():
            TelegramPollState.objects.filter(pk=state.pk).update(
                last_update_at=timezone.now() - timedelta(days=5)
            )

        with patch("notifications.commands_handler._execute_pending", side_effect=delay), \
                patch("notifications.commands_handler._get_updates") as poll:
            with self.assertRaises(ReceiverError):
                run_receiver(once=True)
        poll.assert_not_called()

    def test_legacy_cursor_and_future_timestamp_fail_closed(self):
        state = TelegramPollState.objects.create(chat_id=1234, user_id=5678, next_offset=2)
        for value in [None, timezone.now() + timedelta(days=1)]:
            state.last_update_at = value
            state.save(update_fields=["last_update_at"])
            with patch("notifications.commands_handler._get_updates") as poll:
                with self.assertRaises(ReceiverError):
                    run_receiver(once=True)
            poll.assert_not_called()

    def test_cursor_reset_and_update_id_collision_fail_closed(self):
        state = TelegramPollState.objects.create(chat_id=1234, user_id=5678)
        TelegramCommand.objects.create(update_id=10, text="/racedata")
        with patch("notifications.commands_handler._get_updates") as poll:
            with self.assertRaisesMessage(ReceiverError, "Inbox exists without a cursor"):
                run_receiver(once=True)
        poll.assert_not_called()
        state.next_offset = 5
        state.last_update_at = timezone.now()
        state.save()
        with self.assertRaisesMessage(ReceiverError, "Update ID collision"):
            _store_updates([update(10, "/result")], 1234, 5678)
        state.refresh_from_db()
        self.assertEqual(state.next_offset, 5)
        self.assertEqual(TelegramCommand.objects.get().text, "/racedata")

    def test_command_error_never_exposes_unexpected_exception(self):
        with patch("notifications.management.commands.run_telegram_commands.run_receiver",
                   side_effect=RuntimeError("secret_token")):
            with self.assertRaises(CommandError) as error:
                call_command("run_telegram_commands", once=True, stdout=io.StringIO())
        self.assertNotIn("secret_token", str(error.exception))


class PollTests(TestCase):
    @patch("notifications.commands_handler.build_opener")
    def test_fixed_endpoint_and_safe_failures(self, build):
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({"ok": True, "result": [update(5)]}).encode()
        build.return_value.open.return_value.__enter__.return_value = response
        self.assertEqual(_get_updates("123:secret_token", 5, 25), [update(5)])
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123:secret_token/getUpdates")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        build.return_value.open.side_effect = URLError("secret_token")
        with self.assertRaises(ReceiverError) as error:
            _get_updates("123:secret_token", 5, 25)
        self.assertNotIn("secret_token", str(error.exception))

    @patch("notifications.commands_handler.build_opener")
    def test_invalid_batch_is_rejected(self, build):
        response = build.return_value.open.return_value.__enter__.return_value
        response.status = 200
        for body in [{"ok": True, "result": [{"update_id": True}]}, {"ok": False}, {"ok": True, "result": "bad"}]:
            response.read.return_value = json.dumps(body).encode()
            with self.subTest(body=body), self.assertRaises(ReceiverError):
                _get_updates("token", 0, 1)


class TelegramCommandAdminTests(TestCase):
    def setUp(self):
        self.command = TelegramCommand.objects.create(update_id=42, text="/racedata", status="uncertain")
        self.client.force_login(get_user_model().objects.create_superuser(username="admin"))

    def test_read_only_including_for_superuser(self):
        list_url = reverse("admin:notifications_telegramcommand_changelist")
        change_url = reverse("admin:notifications_telegramcommand_change", args=[self.command.pk])
        self.assertContains(self.client.get(list_url), "Execution uncertain")
        response = self.client.get(change_url)
        self.assertContains(response, "/racedata")
        self.assertContains(response, "Execution uncertain")
        self.assertNotContains(response, 'name="_save"')
        self.assertEqual(self.client.get(reverse("admin:notifications_telegramcommand_add")).status_code, 403)
        self.assertEqual(self.client.post(change_url, {"status": "pending", "text": "/result"}).status_code, 403)
        self.assertEqual(self.client.post(
            reverse("admin:notifications_telegramcommand_delete", args=[self.command.pk]), {"post": "yes"}
        ).status_code, 403)
        self.client.post(list_url, {"action": "delete_selected", "_selected_action": [self.command.pk]})
        self.command.refresh_from_db()
        self.assertEqual((self.command.text, self.command.status), ("/racedata", "uncertain"))

    def test_view_permission_allows_list_and_detail(self):
        viewer = get_user_model().objects.create_user(username="viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="notifications", codename="view_telegramcommand"
        ))
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("admin:notifications_telegramcommand_changelist")).status_code, 200)
        self.assertEqual(self.client.get(
            reverse("admin:notifications_telegramcommand_change", args=[self.command.pk])
        ).status_code, 200)
