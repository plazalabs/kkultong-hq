"""NotionClient 단위 테스트 (429 Retry-After, 529, 503 쓰기 비재시도, 빈 응답, 읽기 전용 가드)."""

from __future__ import annotations

import json
import unittest

from kkultong_dash import NOTION_API_VERSION
from kkultong_dash.notion_client import (
    HTTPResponse,
    NotionAPIError,
    NotionClient,
    NotionEmptyResponseError,
    ReadOnlyGuardError,
)


class TestNotionClient(unittest.TestCase):
    def test_429_respects_retry_after_and_succeeds(self) -> None:
        calls: list[tuple[str, str, dict[str, str]]] = []
        sleeps: list[float] = []
        responses = [
            HTTPResponse(
                status=429,
                headers={"retry-after": "5"},
                body=json.dumps(
                    {
                        "object": "error",
                        "status": 429,
                        "code": "rate_limited",
                        "message": "rate limited",
                        "additional_data": {"rate_limit_reason": "public_api_request_rate_limit"},
                    }
                ),
            ),
            HTTPResponse(
                status=200,
                headers={},
                body=json.dumps({"object": "list", "results": [], "has_more": False}),
            ),
        ]

        def fake_transport(method, url, headers, body, timeout):
            calls.append((method, url, headers))
            return responses.pop(0)

        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            transport=fake_transport,
            sleep_fn=sleeps.append,
            jitter_fn=lambda: 0.1,
        )
        pages, incomplete = client.query_data_source("ds-1")
        self.assertEqual(pages, [])
        self.assertFalse(incomplete)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]["Notion-Version"], NOTION_API_VERSION)
        self.assertEqual(sleeps, [5.1])

    def test_429_public_api_request_blocked_does_not_retry(self) -> None:
        call_count = 0

        def fake_transport(method, url, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            return HTTPResponse(
                status=429,
                headers={"retry-after": "1"},
                body=json.dumps(
                    {
                        "object": "error",
                        "status": 429,
                        "code": "rate_limited",
                        "message": "blocked",
                        "additional_data": {"rate_limit_reason": "public_api_request_blocked"},
                    }
                ),
            )

        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            transport=fake_transport,
            sleep_fn=lambda _: None,
        )
        with self.assertRaises(NotionAPIError) as ctx:
            client.query_data_source("ds-1")
        self.assertEqual(call_count, 1)
        self.assertIn("public_api_request_blocked", str(ctx.exception))

    def test_529_retries_with_additional_data_retry_after(self) -> None:
        sleeps: list[float] = []
        responses = [
            HTTPResponse(
                status=529,
                headers={},
                body=json.dumps(
                    {
                        "object": "error",
                        "status": 529,
                        "code": "service_overload",
                        "message": "overloaded",
                        "additional_data": {"retry_after": "3"},
                    }
                ),
            ),
            HTTPResponse(
                status=200,
                headers={},
                body=json.dumps({"object": "page", "id": "p-1"}),
            ),
        ]

        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            transport=lambda *a: responses.pop(0),
            sleep_fn=sleeps.append,
            jitter_fn=lambda: 0.0,
        )
        page = client.retrieve_page("p-1")
        self.assertEqual(page["id"], "p-1")
        self.assertEqual(sleeps, [3.0])

    def test_write_503_does_not_retry_and_surfaces_committed_id(self) -> None:
        call_count = 0

        def fake_transport(method, url, headers, body, timeout):
            nonlocal call_count
            call_count += 1
            return HTTPResponse(
                status=503,
                headers={},
                body=json.dumps(
                    {
                        "object": "error",
                        "status": 503,
                        "code": "service_unavailable",
                        "message": "saved but response timed out",
                        "additional_data": {"committed_resource_id": "new-page-999"},
                    }
                ),
            )

        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            transport=fake_transport,
            sleep_fn=lambda _: None,
        )
        with self.assertRaises(NotionAPIError) as ctx:
            client.create_dashboard_page("parent-1")
        self.assertEqual(call_count, 1)
        self.assertIn("new-page-999", str(ctx.exception))

    def test_empty_response_body_raises_empty_response_error(self) -> None:
        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            transport=lambda *a: HTTPResponse(status=200, headers={}, body=""),
        )
        with self.assertRaises(NotionEmptyResponseError):
            client.query_data_source("ds-1")

    def test_403_does_not_retry_and_has_clear_hint(self) -> None:
        calls = 0

        def fake_transport(*a):
            nonlocal calls
            calls += 1
            return HTTPResponse(
                status=403,
                headers={},
                body=json.dumps({"code": "restricted_resource", "message": "No access"}),
            )

        client = NotionClient("secret_test_token", min_interval_sec=0.0, transport=fake_transport)
        with self.assertRaises(NotionAPIError) as ctx:
            client.query_data_source("ds-1")
        self.assertEqual(calls, 1)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("권한 없음", str(ctx.exception))

    def test_read_only_guards_block_protected_ids_and_invalid_page_title(self) -> None:
        client = NotionClient(
            "secret_test_token",
            min_interval_sec=0.0,
            protected_ids=["41a89944-f230-8091-9e54-000b1d8dea3c", "parent-page-id"],
            transport=lambda *a: HTTPResponse(status=200, headers={}, body="{}"),
        )
        with self.assertRaises(ReadOnlyGuardError):
            client.append_block_children("41a89944f23080919e54000b1d8dea3c", [{"type": "divider", "divider": {}}])
        with self.assertRaises(ReadOnlyGuardError):
            client.delete_block("parent-page-id")
        with self.assertRaises(ReadOnlyGuardError):
            client.create_dashboard_page("parent-page-id", title="다른 페이지")


if __name__ == "__main__":
    unittest.main()
