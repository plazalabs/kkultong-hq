"""전체 파이프라인 모의 통합 테스트 (정상 / 부분 실패 / 전체 실패 / CLI check-config)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from kkultong_dash import (
    DEFAULT_COMPETITION_DATA_SOURCE_ID,
    DEFAULT_EXPERIENCE_DATA_SOURCE_ID,
    KST,
    PARENT_PAGE_TITLE,
)
from kkultong_dash.cache import LocalCache
from kkultong_dash.config import AppConfig
from kkultong_dash.notion_client import HTTPResponse, NotionClient
from kkultong_dash.parsers import extract_block_plain_text
from kkultong_dash.runner import EXIT_FAILURE, EXIT_OK, EXIT_PARTIAL_FAILURE, run_dashboard_update
from tests.test_publisher import FakeNotionServer


def _exp_row(pid: str, name: str, site: str, status: str, due: str | None, reward: str) -> dict[str, Any]:
    return {
        "object": "page",
        "id": pid,
        "created_time": "2026-11-20T01:00:00.000Z",
        "last_edited_time": "2026-11-20T01:00:00.000Z",
        "in_trash": False,
        "is_archived": False,
        "properties": {
            "캠페인명": {"type": "title", "title": [{"plain_text": name}]},
            "사이트": {"type": "select", "select": {"name": site}},
            "분류": {"type": "select", "select": {"name": "식품"}},
            "제공내역": {"type": "rich_text", "rich_text": [{"plain_text": reward}]},
            "신청일": {"type": "date", "date": {"start": "2026-11-15"}},
            "선정발표일": {"type": "date", "date": {"start": "2026-11-18"}},
            "리뷰마감일": {"type": "date", "date": {"start": due} if due else None},
            "상태": {"type": "select", "select": {"name": status}},
        },
    }


def _comp_row(pid: str, content: str, owner: str, category: str, grade: str | None) -> dict[str, Any]:
    return {
        "object": "page",
        "id": pid,
        "created_time": "2026-11-20T01:30:00.000Z",
        "last_edited_time": "2026-11-20T01:30:00.000Z",
        "in_trash": False,
        "is_archived": False,
        "properties": {
            "내용": {"type": "title", "title": [{"plain_text": content}]},
            "날짜": {"type": "date", "date": {"start": "2026-11-20"}},
            "담당": {"type": "select", "select": {"name": owner}},
            "구분": {"type": "select", "select": {"name": category}},
            "확인": {"type": "select", "select": {"name": grade} if grade else None},
            "링크": {"type": "url", "url": "https://example.com"},
            "회장님 판정": {"type": "rich_text", "rich_text": []},
        },
    }


class TestRunnerEndToEnd(unittest.TestCase):
    def _make_combined_transport(
        self,
        server: FakeNotionServer,
        exp_handler,
        comp_handler,
    ):
        def transport(method, url, headers, body, timeout):
            if f"/data_sources/{DEFAULT_EXPERIENCE_DATA_SOURCE_ID}/query" in url:
                return exp_handler()
            if f"/data_sources/{DEFAULT_COMPETITION_DATA_SOURCE_ID}/query" in url:
                return comp_handler()
            return server.transport(method, url, headers, body, timeout)

        return transport

    def test_normal_then_partial_failure_never_displays_stale_numbers(self) -> None:
        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)

        exp_ok = True
        exp_rows = [
            _exp_row("e1", "치킨 체험단", "레뷰", "당첨", "2026-11-22", "치킨 1마리"),
            _exp_row("e2", "커피 체험단", "서울오빠", "미당첨", None, "아메리카노 2잔"),
        ]
        comp_rows = [
            _comp_row("c1", "체험단 꿀통 발굴", "뮤즈", "꿀통", "확인됨"),
            _comp_row("c2", "BM 초안", "큐", "BM 제안", "원문대조필요"),
        ]

        def exp_handler():
            if not exp_ok:
                return HTTPResponse(
                    403,
                    {},
                    json.dumps({"code": "restricted_resource", "message": "체험단 DB 권한 없음"}),
                )
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": exp_rows, "has_more": False}))

        def comp_handler():
            return HTTPResponse(200, {}, json.dumps({"object": "list", "results": comp_rows, "has_more": False}))

        client = NotionClient(
            "secret_test",
            min_interval_sec=0.0,
            transport=self._make_combined_transport(server, exp_handler, comp_handler),
        )
        cfg = AppConfig(notion_token="secret_test")

        with LocalCache(":memory:") as cache:
            # 1차 실행: 정상
            out1 = run_dashboard_update(
                cfg,
                now_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
                client=client,
                cache=cache,
            )
            self.assertEqual(out1.exit_code, EXIT_OK)
            self.assertEqual(out1.report.run_status, "정상")
            self.assertIsNotNone(out1.publish_result)
            dash_id = out1.publish_result.dashboard_page_id

            # 2차 실행: 체험단 DB에서 403 권한 오류 발생 -> 부분 실패!
            # 이전 실행의 "치킨 체험단" 숫자를 정상인 척 보여 주면 안 된다!
            exp_ok = False
            out2 = run_dashboard_update(
                cfg,
                now_kst=datetime(2026, 11, 20, 17, 50, 10, tzinfo=KST),
                client=client,
                cache=cache,
            )
            self.assertEqual(out2.exit_code, EXIT_PARTIAL_FAILURE)
            self.assertEqual(out2.report.run_status, "부분 실패")
            self.assertIsNone(out2.report.experience_analytics)
            self.assertIsNotNone(out2.report.competition_analytics)

            rendered_texts = [extract_block_plain_text(b) for b in server.children[dash_id]]
            joined = "\n".join(rendered_texts)
            self.assertIn("실행 결과: 부분 실패", joined)
            self.assertIn("403", joined)
            self.assertIn("이전 숫자 미표시", joined)
            self.assertNotIn("치킨 체험단", joined)

    def test_total_failure_writes_failure_status_to_section_a(self) -> None:
        server = FakeNotionServer()
        server.add_page("hub-parent-1", PARENT_PAGE_TITLE)

        client = NotionClient(
            "secret_test",
            min_interval_sec=0.0,
            transport=self._make_combined_transport(
                server,
                lambda: HTTPResponse(404, {}, json.dumps({"code": "object_not_found", "message": "not shared"})),
                lambda: HTTPResponse(200, {}, ""),  # 빈 응답
            ),
        )
        cfg = AppConfig(notion_token="secret_test")

        with LocalCache(":memory:") as cache:
            out = run_dashboard_update(
                cfg,
                now_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
                client=client,
                cache=cache,
            )
            self.assertEqual(out.exit_code, EXIT_FAILURE)
            self.assertEqual(out.report.run_status, "실패")
            self.assertIsNotNone(out.publish_result)
            dash_id = out.publish_result.dashboard_page_id
            joined = "\n".join(extract_block_plain_text(b) for b in server.children[dash_id])
            self.assertIn("실행 결과: 실패", joined)
            self.assertIn("404", joined)
            self.assertIn("빈 응답", joined)

    def test_cli_check_config_without_token_reports_waiting(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [str(repo_root / "kkultong-dash"), "check-config"],
            cwd=str(repo_root),
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, EXIT_FAILURE)
        self.assertIn("NOTION_TOKEN: 미설정", proc.stdout)
        self.assertIn("대기 중", proc.stderr)

    def test_dotenv_parsing_and_secret_masking(self) -> None:
        import tempfile
        from kkultong_dash.config import parse_dotenv_file

        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(
                "# comment\n"
                "NOTION_TOKEN='ntn_dummy_secret_value_123'\n"
                "export NOTION_EXPERIENCE_DATA_SOURCE_ID=\"collection://41a89944-f230-8091-9e54-000b1d8dea3c\"\n",
                encoding="utf-8",
            )
            parsed = parse_dotenv_file(env_path)
            cfg = AppConfig.from_env(parsed)
            self.assertEqual(cfg.experience_data_source_id, DEFAULT_EXPERIENCE_DATA_SOURCE_ID)
            summary_str = json.dumps(cfg.safe_summary(), ensure_ascii=False)
            self.assertNotIn("ntn_dummy_secret_value_123", summary_str)
            self.assertIn("마스킹됨", summary_str)


if __name__ == "__main__":
    unittest.main()
