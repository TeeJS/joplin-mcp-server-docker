"""Robustness guarantees for the Joplin API client.

Covers the defensive changes that keep a malformed backend response or a
hostile tool argument from crashing a request or altering its target:
timestamp coercion, missing pagination keys, path-segment encoding, and the
permanent-delete query parameter.
"""

import unittest
from datetime import timezone
from unittest import mock

from src.joplin.joplin_api import JoplinAPI, _ms_to_datetime


class MsToDatetimeTests(unittest.TestCase):
    def test_valid_ms_is_utc(self):
        dt = _ms_to_datetime(1_700_000_000_000)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_zero_and_negative_are_none(self):
        self.assertIsNone(_ms_to_datetime(0))
        self.assertIsNone(_ms_to_datetime(-1))

    def test_out_of_range_is_none(self):
        self.assertIsNone(_ms_to_datetime(10**18))

    def test_non_numeric_is_none(self):
        self.assertIsNone(_ms_to_datetime("nope"))
        self.assertIsNone(_ms_to_datetime(None))
        self.assertIsNone(_ms_to_datetime(True))  # bool is not a real timestamp


class RequestShapingTests(unittest.TestCase):
    def setUp(self):
        self.api = JoplinAPI(token="t" * 40, base_url="http://joplin:41184")

    def test_get_notes_tolerates_missing_pagination_keys(self):
        with mock.patch.object(self.api, "_make_request", return_value={}):
            page = self.api.get_notes()
        self.assertEqual(page.items, [])
        self.assertFalse(page.has_more)

    def test_get_note_encodes_the_id(self):
        captured = {}

        def fake(method, endpoint, params=None, json=None):
            captured["endpoint"] = endpoint
            return {"id": "x", "title": "x"}

        with mock.patch.object(self.api, "_make_request", side_effect=fake):
            self.api.get_note("../folders")
        # The traversal characters must be percent-encoded, not passed through.
        self.assertNotIn("../", captured["endpoint"])
        self.assertIn("%2F", captured["endpoint"])

    def test_delete_note_permanent_uses_param_not_string(self):
        captured = {}

        def fake(method, endpoint, params=None, json=None):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {}

        with mock.patch.object(self.api, "_make_request", side_effect=fake):
            self.api.delete_note("abc", permanent=True)
        self.assertNotIn("?", captured["endpoint"])
        self.assertEqual(captured["params"], {"permanent": "1"})


if __name__ == "__main__":
    unittest.main()
