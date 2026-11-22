"""SQLite 로컬 캐시 및 증분/일일 전체 재동기화 테스트."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from kkultong_dash import KST
from kkultong_dash.cache import LocalCache
from kkultong_dash.notion_client import HTTPResponse, NotionClient


def _make_page(page_id: str, title: str, edited: str, *, in_trash: bool = False) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "created_time": edited,
        "last_edited_time": edited,
        "in_trash": in_trash,
        "is_archived": False,
        "properties": {
            "캠페인명": {"type": "title", "title": [{"plain_text": title}]},
            "상태": {"type": "select", "select": {"name": "당첨"}},
        },
    }


class TestLocalCache(unittest.TestCase):
    def test_first_full_then_incremental_then_next_day_full_mismatch_warning(self) -> None:
        recorded_bodies: list[dict] = []
        batches = [
            # 1) Day 1 첫 실행: 전체 조회 2건
            [_make_page("p1", "캠페인1", "2026-11-20T01:00:00.000Z"), _make_page("p2", "캠페인2", "2026-11-20T01:10:00.000Z")],
            # 2) Day 1 두 번째 실행: 증분 조회 (p2 수정 + p3 추가)
            [_make_page("p2", "캠페인2-수정", "2026-11-20T03:00:00.000Z"), _make_page("p3", "캠페인3", "2026-11-20T03:05:00.000Z")],
            # 3) Day 2 첫 실행: 일일 전체 재동기화 -> p1이 노션에서 삭제되어 2건만 반환 (캐시 3건 vs 전체 2건 -> 경고!)
            [_make_page("p2", "캠페인2-수정", "2026-11-20T03:00:00.000Z"), _make_page("p3", "캠페인3", "2026-11-20T03:05:00.000Z")],
        ]

        def fake_transport(method, url, headers, body, timeout):
            recorded_bodies.append(json.loads(body.decode("utf-8")) if body else {})
            results = batches.pop(0)
            return HTTPResponse(
                status=200,
                headers={},
                body=json.dumps({"object": "list", "results": results, "has_more": False}),
            )

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=fake_transport)
        with LocalCache(":memory:") as cache:
            # Run 1 (Day 1 오전) -> full
            r1 = cache.sync_data_source(
                client,
                source_key="experience",
                source_label="체험단 DB",
                data_source_id="ds-exp",
                now_kst=datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST),
            )
            self.assertTrue(r1.ok)
            self.assertEqual(r1.sync_mode, "full")
            self.assertEqual(r1.row_count, 2)
            self.assertEqual(r1.warnings, [])
            self.assertNotIn("filter", recorded_bodies[0])

            # Run 2 (Day 1 오후) -> incremental with last_edited_time filter
            r2 = cache.sync_data_source(
                client,
                source_key="experience",
                source_label="체험단 DB",
                data_source_id="ds-exp",
                now_kst=datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST),
            )
            self.assertTrue(r2.ok)
            self.assertEqual(r2.sync_mode, "incremental")
            self.assertEqual(r2.fetched_count, 2)
            self.assertEqual(r2.row_count, 3)
            self.assertIn("filter", recorded_bodies[1])
            self.assertEqual(recorded_bodies[1]["filter"]["timestamp"], "last_edited_time")
            # 01:10:00Z - 1분 겹침 = 01:09:00Z
            self.assertEqual(
                recorded_bodies[1]["filter"]["last_edited_time"]["on_or_after"],
                "2026-11-20T01:09:00.000Z",
            )

            # Run 3 (Day 2 오전) -> 일일 전체 재동기화 + 행 수 불일치 경고
            r3 = cache.sync_data_source(
                client,
                source_key="experience",
                source_label="체험단 DB",
                data_source_id="ds-exp",
                now_kst=datetime(2026, 11, 21, 11, 20, 0, tzinfo=KST),
            )
            self.assertTrue(r3.ok)
            self.assertEqual(r3.sync_mode, "full")
            self.assertEqual(r3.row_count, 2)
            self.assertEqual(len(r3.warnings), 1)
            self.assertIn("불일치", r3.warnings[0])
            self.assertIn("3건 → 전체 조회 2건 (-1건)", r3.warnings[0])
            self.assertEqual(cache.count_cached_rows("experience"), 2)

    def test_empty_full_sync_returns_failure_not_stale_cache(self) -> None:
        responses = [
            # 첫 성공
            HTTPResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {
                        "object": "list",
                        "results": [_make_page("p1", "캠페인1", "2026-11-20T01:00:00.000Z")],
                        "has_more": False,
                    }
                ),
            ),
            # 다음 날 전체 조회가 0건(빈 응답) 반환
            HTTPResponse(
                status=200,
                headers={},
                body=json.dumps({"object": "list", "results": [], "has_more": False}),
            ),
        ]
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=lambda *a: responses.pop(0))
        with LocalCache(":memory:") as cache:
            r1 = cache.sync_data_source(
                client,
                source_key="experience",
                source_label="체험단 DB",
                data_source_id="ds-exp",
                now_kst=datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST),
            )
            self.assertTrue(r1.ok)

            r2 = cache.sync_data_source(
                client,
                source_key="experience",
                source_label="체험단 DB",
                data_source_id="ds-exp",
                now_kst=datetime(2026, 11, 21, 11, 20, 0, tzinfo=KST),
            )
            self.assertFalse(r2.ok)
            self.assertEqual(r2.rows_raw, [])
            self.assertIn("빈 응답", r2.error_message or "")

    def test_fact_source_doc_state_and_grade_history_snapshots(self) -> None:
        with LocalCache(":memory:") as cache:
            self.assertIsNone(cache.get_fact_source_updated_at("p1"))
            cache.save_fact_source_updated_at(
                "p1",
                source_url="https://help.medium.com/hc/en-us/articles/39121627791639",
                article_updated_at="2026-11-11T16:29:41Z",
                checked_at_kst=datetime(2026, 11, 21, 5, 0, 0, tzinfo=KST),
            )
            self.assertEqual(cache.get_fact_source_updated_at("p1"), "2026-11-11T16:29:41Z")

            cache.save_fact_source_updated_at(
                "p1",
                source_url="https://help.medium.com/hc/en-us/articles/39121627791639",
                article_updated_at="2026-11-22T01:00:00Z",
                checked_at_kst=datetime(2026, 11, 22, 5, 0, 0, tzinfo=KST),
            )
            self.assertEqual(cache.get_fact_source_updated_at("p1"), "2026-11-22T01:00:00Z")


if __name__ == "__main__":
    unittest.main()
