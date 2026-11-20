"""DashboardPublisher 단위 테스트 (페이지 식별, 읽기 전용 가드, 섹션 E 인플레이스 보존 및 복구)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from typing import Any

from kkultong_dash import (
    DASHBOARD_PAGE_TITLE,
    KST,
    MANUAL_E_END_MARKER,
    MANUAL_E_START_MARKER,
    PARENT_PAGE_TITLE,
)
from kkultong_dash.cache import LocalCache
from kkultong_dash.models import DashboardReport, SourceSyncResult
from kkultong_dash.notion_client import HTTPResponse, NotionClient, ReadOnlyGuardError
from kkultong_dash.parsers import extract_block_plain_text
from kkultong_dash.publisher import DashboardPublisher


class FakeNotionServer:
    """Notion Pages/Blocks/Search API의 인메모리 시뮬레이터 (2026-03-11 position 지원)."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[dict[str, Any]]] = {}
        self.deleted_ids: list[str] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def add_page(self, page_id: str, title: str, *, parent_page_id: str | None = None) -> None:
        parent = {"type": "page_id", "page_id": parent_page_id} if parent_page_id else {"type": "workspace", "workspace": True}
        self.pages[page_id] = {
            "object": "page",
            "id": page_id,
            "in_trash": False,
            "parent": parent,
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        }
        self.children.setdefault(page_id, [])
        if parent_page_id:
            self.children.setdefault(parent_page_id, []).append(
                {
                    "object": "block",
                    "id": page_id,
                    "type": "child_page",
                    "in_trash": False,
                    "child_page": {"title": title},
                }
            )

    def transport(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> HTTPResponse:
        payload = json.loads(body.decode("utf-8")) if body else {}
        path = url.split("/v1", 1)[1].split("?", 1)[0]

        if method == "POST" and path == "/search":
            q = payload.get("query", "")
            matches = [
                p for p in self.pages.values() if q in p["properties"]["title"]["title"][0]["plain_text"]
            ]
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": matches, "has_more": False}))

        if method == "GET" and path.startswith("/pages/"):
            pid = path.split("/pages/", 1)[1]
            if pid not in self.pages:
                return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "not found"}))
            return HTTPResponse(200, {}, json.dumps(self.pages[pid]))

        if method == "POST" and path == "/pages":
            parent_id = payload["parent"]["page_id"]
            title = payload["properties"]["title"]["title"][0]["text"]["content"]
            new_id = self._next_id("dash")
            self.add_page(new_id, title, parent_page_id=parent_id)
            return HTTPResponse(200, {}, json.dumps(self.pages[new_id]))

        if method == "GET" and path.startswith("/blocks/") and path.endswith("/children"):
            bid = path[len("/blocks/") : -len("/children")]
            items = [b for b in self.children.get(bid, []) if not b.get("in_trash")]
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": items, "has_more": False}))

        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            bid = path[len("/blocks/") : -len("/children")]
            new_blocks = []
            for raw in payload.get("children", []):
                blk = json.loads(json.dumps(raw))
                blk["id"] = self._next_id("blk")
                blk["in_trash"] = False
                # 표 내부 행은 별도 children 저장소에도 보관
                if blk.get("type") == "table":
                    table_rows = blk["table"].get("children", [])
                    self.children[blk["id"]] = table_rows
                new_blocks.append(blk)

            lst = self.children.setdefault(bid, [])
            pos = payload.get("position")
            if pos is None or pos.get("type") == "end":
                lst.extend(new_blocks)
            elif pos.get("type") == "start":
                lst[0:0] = new_blocks
            elif pos.get("type") == "after_block":
                target_id = pos["after_block"]["id"]
                idx = next(i for i, b in enumerate(lst) if b["id"] == target_id)
                lst[idx + 1 : idx + 1] = new_blocks
            else:
                return HTTPResponse(400, {}, json.dumps({"code": "validation_error", "message": "bad position"}))

            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": new_blocks, "has_more": False}))

        if method == "DELETE" and path.startswith("/blocks/"):
            bid = path.split("/blocks/", 1)[1]
            self.deleted_ids.append(bid)
            for parent_list in self.children.values():
                for i, b in enumerate(parent_list):
                    if b.get("id") == bid:
                        parent_list.pop(i)
                        return HTTPResponse(200, {}, json.dumps({"object": "block", "id": bid}))
            return HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "missing"}))

        return HTTPResponse(404, {}, json.dumps({"code": "not_found", "message": f"unhandled {method} {path}"}))


def _dummy_report(now_kst: datetime) -> DashboardReport:
    return DashboardReport(
        updated_at_kst=now_kst,
        run_status="정상",
        failure_reasons=[],
        warnings=[],
        experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 1, 1),
        competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 1, 1),
        experience_analytics=None,
        competition_analytics=None,
    )


class TestDashboardPublisher(unittest.TestCase):
    def test_create_once_then_preserve_section_e_in_place_without_deleting_manual_blocks(self) -> None:
        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        with LocalCache(":memory:") as cache:
            pub = DashboardPublisher(client, cache)

            # 1차 실행: 페이지 최초 1회 생성
            r1 = pub.publish(_dummy_report(datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST)))
            self.assertTrue(r1.created_page)
            dash_id = r1.dashboard_page_id
            self.assertEqual(cache.get_state("dashboard_page_id"), dash_id)

            # 사용자가 노션 UI에서 섹션 E 마커 사이에 직접 블록을 수정/추가했다고 가정
            blocks = server.children[dash_id]
            start_idx = next(i for i, b in enumerate(blocks) if MANUAL_E_START_MARKER in extract_block_plain_text(b))
            end_idx = next(i for i, b in enumerate(blocks) if MANUAL_E_END_MARKER in extract_block_plain_text(b))
            custom_block = {
                "object": "block",
                "id": "user-manual-custom-block-1",
                "type": "bulleted_list_item",
                "in_trash": False,
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": "티스토리: 애드센스 승인 완료 (수익 12,300원)"}}]
                },
            }
            blocks[start_idx + 1 : end_idx] = [custom_block]

            # 2차 실행: 기존 페이지 재사용 + 사용자 수동 블록 ID가 삭제되지 않고 정확히 유지되어야 함
            server.deleted_ids.clear()
            r2 = pub.publish(_dummy_report(datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST)))
            self.assertFalse(r2.created_page)
            self.assertEqual(r2.dashboard_page_id, dash_id)
            self.assertEqual(r2.preserved_manual_blocks, 1)
            self.assertNotIn("user-manual-custom-block-1", server.deleted_ids)

            final_blocks = server.children[dash_id]
            texts = [extract_block_plain_text(b) for b in final_blocks]
            idx_e_heading = next(i for i, t in enumerate(texts) if t == "E. 수동 입력 칸")
            idx_start = next(i for i, t in enumerate(texts) if MANUAL_E_START_MARKER in t)
            idx_user = next(i for i, b in enumerate(final_blocks) if b["id"] == "user-manual-custom-block-1")
            idx_end = next(i for i, t in enumerate(texts) if MANUAL_E_END_MARKER in t)
            idx_f_heading = next(i for i, t in enumerate(texts) if t == "F. 경쟁 기록 요약")

            # 순서가 정확히 E 제목 < START < 사용자 블록 < END < F 제목 이어야 한다
            self.assertTrue(idx_e_heading < idx_start < idx_user < idx_end < idx_f_heading)

    def test_missing_end_marker_recovers_without_losing_user_content(self) -> None:
        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        with LocalCache(":memory:") as cache:
            pub = DashboardPublisher(client, cache)
            r1 = pub.publish(_dummy_report(datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST)))
            dash_id = r1.dashboard_page_id

            # 사용자가 실수로 END 마커만 지웠다고 시뮬레이션
            blocks = server.children[dash_id]
            end_idx = next(i for i, b in enumerate(blocks) if MANUAL_E_END_MARKER in extract_block_plain_text(b))
            blocks.pop(end_idx)

            report2 = _dummy_report(datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST))
            r2 = pub.publish(report2)
            self.assertGreaterEqual(r2.preserved_manual_blocks, 1)
            self.assertTrue(any("자동 복구" in w for w in r2.warnings))
            # END 마커가 복구되었는지 확인
            final_texts = [extract_block_plain_text(b) for b in server.children[dash_id]]
            self.assertTrue(any(MANUAL_E_END_MARKER in t for t in final_texts))

    def test_safety_guard_blocks_overwriting_non_dashboard_page(self) -> None:
        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        server.add_page("rules-page-1", "허브 운영 규정", parent_page_id="hub-parent-1")
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)

        with LocalCache(":memory:") as cache:
            pub = DashboardPublisher(
                client,
                cache,
                parent_page_id="hub-parent-1",
                dashboard_page_id="rules-page-1",  # 실수로 기존 규정 페이지 ID를 지정한 경우
            )
            with self.assertRaises(ReadOnlyGuardError):
                pub.publish(_dummy_report(datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST)))

    def test_batch_chaining_and_large_table_overflow(self) -> None:
        from kkultong_dash.renderer import make_rich_text, paragraph, table_block

        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        server.add_page("dash-big", DASHBOARD_PAGE_TITLE, parent_page_id="hub-parent-1")
        # 기존 수동 마커를 페이지에 먼저 배치
        server.children["dash-big"] = [
            {"object": "block", "id": "m-start", "type": "paragraph", "in_trash": False, "paragraph": {"rich_text": [{"plain_text": MANUAL_E_START_MARKER}]}},
            {"object": "block", "id": "m-user", "type": "paragraph", "in_trash": False, "paragraph": {"rich_text": [{"plain_text": "유저 메모"}]}},
            {"object": "block", "id": "m-end", "type": "paragraph", "in_trash": False, "paragraph": {"rich_text": [{"plain_text": MANUAL_E_END_MARKER}]}},
        ]

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        with LocalCache(":memory:") as cache:
            pub = DashboardPublisher(client, cache, parent_page_id="hub-parent-1", dashboard_page_id="dash-big")

            # 105개의 paragraph + 120행 테이블을 position={"type": "start"}로 삽입
            big_rows = [[make_rich_text(f"row-{i}"), make_rich_text("val")] for i in range(120)]
            big_blocks = [paragraph(f"p-{i}") for i in range(105)] + [table_block(["col1", "col2"], big_rows)]
            pub._append_blocks_in_order("dash-big", big_blocks, initial_position={"type": "start"})

            children = server.children["dash-big"]
            # 순서 확인: p-0 .. p-104, table, m-start, m-user, m-end
            self.assertEqual(extract_block_plain_text(children[0]), "p-0")
            self.assertEqual(extract_block_plain_text(children[104]), "p-104")
            self.assertEqual(children[105]["type"], "table")
            self.assertEqual(children[106]["id"], "m-start")
            self.assertEqual(children[107]["id"], "m-user")
            self.assertEqual(children[108]["id"], "m-end")

            # 테이블 행이 헤더 1행 + 데이터 120행 = 총 121행 모두 추가되었는지 확인
            table_id = children[105]["id"]
            self.assertEqual(len(server.children[table_id]), 121)


if __name__ == "__main__":
    unittest.main()
