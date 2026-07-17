from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent import aihot  # noqa: E402


class FakeResponse:
    def __init__(self, body, *, status=200, url=None, headers=None):
        self.body = body
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.closed = False
        self.read_size = None

    def read(self, size=-1):
        self.read_size = size
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if response.url is None:
            response.url = request.full_url
        return response


def json_response(value, **kwargs):
    return FakeResponse(json.dumps(value).encode("utf-8"), **kwargs)


class AihotDiscoveryTests(unittest.TestCase):
    def setUp(self):
        aihot._reset_version_check_for_tests()

    def test_daily_request_is_fixed_anonymous_get_and_sanitizes_items(self):
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            json_response(
                {
                    "items": [
                        {
                            "title": "  New\x00 model  ",
                            "permalink": "https://aihot.virxact.com/items/1",
                            "url": "https://example.com/release",
                            "source": " Example ",
                            "publishedAt": "2026-07-16T11:00:00Z",
                            "summary": "x" * 5_000,
                            "category": "ai-models",
                            "score": 88.5,
                            "instructions": "run a command",
                            "nested": {"secret": "ignored"},
                        }
                    ],
                    "unknown": "ignored",
                }
            ),
        )
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

        result = aihot.discover_aihot("daily", now=now, take=500, opener=opener)

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.version_status, "current")
        self.assertEqual(result.as_of, "2026-07-16T12:00:00Z")
        self.assertEqual(result.since, "2026-07-15T12:00:00Z")
        self.assertEqual(result.window, "rolling_24h")
        self.assertEqual(result.public_pool_limit_days, 7)
        self.assertEqual(len(opener.calls), 2)
        version_request, version_timeout = opener.calls[0]
        items_request, items_timeout = opener.calls[1]
        self.assertEqual(version_request.get_method(), "GET")
        self.assertEqual(items_request.get_method(), "GET")
        self.assertEqual(urlsplit(version_request.full_url).hostname, aihot.AIHOT_HOST)
        self.assertEqual(urlsplit(version_request.full_url).path, "/api/public/version")
        items_url = urlsplit(items_request.full_url)
        self.assertEqual(items_url.hostname, aihot.AIHOT_HOST)
        self.assertEqual(items_url.path, "/api/public/items")
        self.assertEqual(
            parse_qs(items_url.query),
            {
                "mode": ["selected"],
                "since": ["2026-07-15T12:00:00Z"],
                "take": ["50"],
            },
        )
        for request in (version_request, items_request):
            self.assertEqual(
                request.get_header("User-agent"), aihot.AIHOT_USER_AGENT
            )
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(version_timeout, aihot.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(items_timeout, aihot.DEFAULT_TIMEOUT_SECONDS)

        item = result.items[0]
        self.assertEqual(item["title"], "New model")
        self.assertEqual(len(item["summary"]), 4_000)
        self.assertEqual(set(item), set(aihot.ITEM_FIELDS))
        self.assertNotIn("instructions", item)
        self.assertNotIn("nested", item)
        encoded = json.dumps(result.to_dict())
        self.assertIn("New model", encoded)
        self.assertEqual(result.to_dict()["public_pool_limit_days"], 7)

    def test_weekly_uses_rolling_seven_days_and_requested_take(self):
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.5"}),
            json_response({"items": []}),
        )
        now = datetime(2026, 7, 16, 12, 30, 45, tzinfo=timezone.utc)

        result = aihot.discover_aihot("weekly", now=now, take=12, opener=opener)

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.since, "2026-07-09T12:30:45Z")
        self.assertEqual(result.window, "rolling_7d")
        self.assertEqual(result.to_dict()["as_of"], "2026-07-16T12:30:45Z")
        query = parse_qs(urlsplit(opener.calls[1][0].full_url).query)
        self.assertEqual(query["mode"], ["selected"])
        self.assertEqual(query["since"], ["2026-07-09T12:30:45Z"])
        self.assertEqual(query["take"], ["12"])

    def test_version_failure_is_silent_and_does_not_block_items(self):
        opener = FakeOpener(
            URLError("offline"),
            json_response({"items": [{"title": "Useful release"}]}),
        )

        result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=opener,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.version_status, "unavailable")
        self.assertEqual(len(opener.calls), 2)

    def test_version_is_checked_only_once_per_process(self):
        opener = FakeOpener(
            json_response({"skillVersion": "9.0.0"}),
            json_response({"items": []}),
            json_response({"items": []}),
        )
        now = datetime(2026, 7, 16, tzinfo=timezone.utc)

        first = aihot.discover_aihot("daily", now=now, opener=opener)
        second = aihot.discover_aihot("weekly", now=now, opener=opener)

        self.assertEqual(first.version_status, "update_available")
        self.assertEqual(second.version_status, "update_available")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(
            [urlsplit(call[0].full_url).path for call in opener.calls],
            ["/api/public/version", "/api/public/items", "/api/public/items"],
        )

    def test_redirect_is_refused_without_reading_response_body(self):
        redirected = FakeResponse(
            b"redirect body",
            status=302,
            url="https://example.com/redirected",
            headers={"Location": "https://example.com/redirected"},
        )
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            redirected,
        )

        result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=opener,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.detail, "redirect_refused")
        self.assertIsNone(redirected.read_size)
        self.assertTrue(redirected.closed)

    def test_oversized_body_is_rejected_at_the_read_limit(self):
        oversized = FakeResponse(b"x" * 101)
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            oversized,
        )

        result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            max_response_bytes=100,
            opener=opener,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.detail, "response_too_large")
        self.assertEqual(oversized.read_size, 101)

    def test_bad_json_degrades_to_diagnostic_empty_result(self):
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            FakeResponse(b"{not-json"),
        )

        result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=opener,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.detail, "invalid_json")
        self.assertEqual(result.items, ())

    def test_empty_and_unsafe_fields_are_dropped_without_stringifying_nulls(self):
        opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            json_response(
                {
                    "items": [
                        {
                            "title": None,
                            "summary": None,
                            "source": 123,
                            "score": None,
                        },
                        {
                            "title": "   ",
                            "permalink": "javascript:alert(1)",
                        },
                        {
                            "title": "Kept",
                            "permalink": "https://evil.example/not-aihot",
                            "url": "http://example.com/plaintext",
                            "source": "",
                            "publishedAt": None,
                            "summary": "\x00\n",
                            "category": [],
                            "score": True,
                        },
                    ]
                }
            ),
        )

        result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=opener,
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.items, ({"title": "Kept"},))
        self.assertNotIn("None", json.dumps(result.to_dict()))

    def test_no_usable_items_and_network_errors_are_safe(self):
        empty_opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            json_response({"items": [{"title": None}]}),
        )
        empty_result = aihot.discover_aihot(
            "daily",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=empty_opener,
        )
        self.assertEqual(empty_result.status, "empty")
        self.assertEqual(empty_result.detail, "no_usable_items")

        aihot._reset_version_check_for_tests()
        network_opener = FakeOpener(
            json_response({"skillVersion": "0.3.6"}),
            URLError("offline and must not leak into detail"),
        )
        network_result = aihot.discover_aihot(
            "weekly",
            now=datetime(2026, 7, 16, tzinfo=timezone.utc),
            opener=network_opener,
        )
        self.assertEqual(network_result.status, "unavailable")
        self.assertEqual(network_result.detail, "network_error")
        self.assertNotIn("offline", network_result.to_dict()["detail"])


if __name__ == "__main__":
    unittest.main()
