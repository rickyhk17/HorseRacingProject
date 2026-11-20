from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from collection.resultsall import CollectionError

from .bot_commands import handle_command


class CommandDispatchTests(SimpleTestCase):
    def test_collection_requires_authorization(self):
        with patch.dict("os.environ", {}, clear=True), patch("notifications.bot_commands.collect_results") as collect:
            self.assertIn("未確認", handle_command("/result https://racing.hkjc.com/"))
            collect.assert_not_called()

    def test_results_normalizes_html_ampersand(self):
        with patch.dict("os.environ", {"HKJC_TERMS_REVIEWED": "yes"}, clear=True), patch(
            "notifications.bot_commands.collect_results", return_value=[object()]
        ) as collect:
            response = handle_command("/result https://racing.hkjc.com/x?a=1&amp;b=2")
        collect.assert_called_once_with("https://racing.hkjc.com/x?a=1&b=2", terms_reviewed=True)
        self.assertIn("1 場", response)
        self.assertIn("不會自動結算", response)

    def test_racecard_failure_is_not_reported_as_success(self):
        failure = SimpleNamespace(status="failure", error="parse_error")
        with patch.dict("os.environ", {"HKJC_TERMS_REVIEWED": "yes"}, clear=True), patch(
            "notifications.bot_commands.collect_racecard", return_value=(failure, True)
        ):
            self.assertIn("未能採集", handle_command("/racedata https://racing.hkjc.com/x"))

    def test_results_error_is_safe(self):
        with patch.dict("os.environ", {"HKJC_TERMS_REVIEWED": "yes"}, clear=True), patch(
            "notifications.bot_commands.collect_results", side_effect=CollectionError("parse_error")
        ):
            self.assertIn("parse_error", handle_command("/result https://racing.hkjc.com/x"))

    def test_prediction_is_nonbinding(self):
        with patch("notifications.bot_commands.candidate_summary", return_value="候選預測（未批准）"):
            self.assertIn("未批准", handle_command("/predit"))
        self.assertIn("用法", handle_command("/predit extra"))
