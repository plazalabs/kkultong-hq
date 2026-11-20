"""후속 수정 4건 + README 저장소 경로 확인 테스트."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path

from kkultong_dash import (
    DEFAULT_SETTLEMENT_PAGE_ID,
    DEFAULT_SETTLEMENT_PAGE_TITLE,
    DEFAULT_SETTLEMENT_PAGE_URL,
    KST,
)
from kkultong_dash.analytics import compute_experience_analytics, compute_next_scheduled_run_kst
from kkultong_dash.config import AppConfig, normalize_notion_id
from kkultong_dash.models import DashboardReport, ExperienceRow, SourceSyncResult
from kkultong_dash.notion_client import HTTPResponse, NotionClient, ReadOnlyGuardError
from kkultong_dash.parsers import extract_block_plain_text
from kkultong_dash.renderer import build_bottom_blocks, build_top_blocks
from kkultong_dash.settlement import inspect_settlement_page
from tests.test_fact_ledger import FakeFactNotionServer


class TestFollowUpFixes(unittest.TestCase):
    # ------------------------------------------------------------------
    # 후속 수정 1: 섹션 A '다음 예정 갱신 시각' (11:20 / 17:50 KST 중 현재 이후 가까운 시각)
    # ------------------------------------------------------------------

    def test_section_a_next_scheduled_update_time(self) -> None:
        cases = [
            (datetime(2026, 11, 20, 8, 0, 0, tzinfo=KST), datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 11, 19, 59, tzinfo=KST), datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 11, 20, 0, tzinfo=KST), datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST), datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 17, 49, 59, tzinfo=KST), datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 17, 50, 0, tzinfo=KST), datetime(2026, 11, 21, 11, 20, 0, tzinfo=KST)),
            (datetime(2026, 11, 20, 23, 15, 0, tzinfo=KST), datetime(2026, 11, 21, 11, 20, 0, tzinfo=KST)),
        ]
        for now_kst, expected in cases:
            self.assertEqual(compute_next_scheduled_run_kst(now_kst), expected)

        report = DashboardReport(
            updated_at_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 1, 1),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 1, 1),
            experience_analytics=None,
            competition_analytics=None,
        )
        top_blocks = build_top_blocks(report)
        section_a_callout = next(b for b in top_blocks if b.get("type") == "callout")
        text = extract_block_plain_text(section_a_callout)
        self.assertIn("다음 예정 갱신 시각: 2026-11-20 17:50 KST", text)

    # ------------------------------------------------------------------
    # 후속 수정 2: 섹션 C 사이트별 표 합계에 실측 제외 건수 병기 '(본인 구매 리뷰 N건 제외)'
    # ------------------------------------------------------------------

    def test_section_c_per_site_totals_appends_real_excluded_count(self) -> None:
        now_kst = datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST)

        for excluded_n in (4, 1, 0):
            rows = [
                ExperienceRow("n1", "백김치 체험", "레뷰", "식품", "백김치 1kg", None, None, date(2026, 11, 30), "당첨", None, None),
                ExperienceRow("n2", "커피 체험", "레뷰", "식품", "커피", None, None, None, "미당첨", None, None),
            ]
            for i in range(excluded_n):
                rows.append(
                    ExperienceRow(f"sp-{i}", f"쿠팡 리뷰: 상품{i}", "쿠팡", "생활", "본인 구매", None, None, None, "리뷰완료", None, None)
                )

            exp = compute_experience_analytics(rows, now_kst=now_kst)
            report = DashboardReport(
                updated_at_kst=now_kst,
                run_status="정상",
                failure_reasons=[],
                warnings=[],
                experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", len(rows), len(rows)),
                competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
                experience_analytics=exp,
                competition_analytics=None,
            )
            top_blocks = build_top_blocks(report)
            texts = [extract_block_plain_text(b) for b in top_blocks]
            site_h_idx = texts.index("사이트별 신청·당첨 수 및 당첨률")
            site_table = next(b for b in top_blocks[site_h_idx + 1 :] if b.get("type") == "table")
            last_row = site_table["table"]["children"][-1]
            total_cell_text = last_row["table_row"]["cells"][0][0]["text"]["content"]
            self.assertEqual(total_cell_text, f"합계 (본인 구매 리뷰 {excluded_n}건 제외)")

    # ------------------------------------------------------------------
    # 후속 수정 3: 섹션 B 전체(마감 임박 + 마감일 미입력)에서 본인 구매 리뷰 제외
    # ------------------------------------------------------------------

    def test_section_b_excludes_self_purchase_from_both_deadlines_and_missing_deadlines(self) -> None:
        now_kst = datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST)
        rows = [
            ExperienceRow("1", "정상 마감있음", "레뷰", "식품", "김치 1kg", None, None, date(2026, 11, 23), "당첨", None, None),
            ExperienceRow("2", "정상 마감미입력", "서울오빠", "뷰티", "크림", None, date(2026, 11, 19), None, "리뷰대기", None, None),
            ExperienceRow("3", "쿠팡 리뷰: 생수", "쿠팡", "생활", "점수 작업", None, None, date(2026, 11, 21), "당첨", None, None),
            ExperienceRow("4", "생활용품 리뷰", "쿠팡", "생활", "본인 구매 1개", None, date(2026, 11, 18), None, "리뷰대기", None, None),
            ExperienceRow("5", "쿠팡 리뷰: 세제", "쿠팡", "생활", "본인 구매", None, None, None, "당첨", None, None),
        ]

        exp = compute_experience_analytics(rows, now_kst=now_kst)
        self.assertEqual([d.campaign_name for d in exp.deadlines], ["정상 마감있음"])
        self.assertEqual([m.campaign_name for m in exp.missing_deadlines], ["정상 마감미입력"])
        self.assertEqual(
            sorted(r.campaign_name for r in exp.self_purchase_reviews),
            ["생활용품 리뷰", "쿠팡 리뷰: 생수", "쿠팡 리뷰: 세제"],
        )

    # ------------------------------------------------------------------
    # 후속 수정 4: 섹션 G 정산판 구조 판별 (DB / 표 / 비구조화 / 접근 불가) 및 양쪽 렌더링
    # ------------------------------------------------------------------

    def test_settlement_structured_database_rendering_and_read_only_protection(self) -> None:
        server = FakeFactNotionServer()
        server.add_page(DEFAULT_SETTLEMENT_PAGE_ID, DEFAULT_SETTLEMENT_PAGE_TITLE)
        # 정산판 하위에 수익 DB 생성
        server.databases["rev-db-1"] = {
            "object": "database",
            "id": "rev-db-1",
            "in_trash": False,
            "title": [{"plain_text": "월별 수익 표"}],
            "data_sources": [{"id": "rev-ds-1", "name": "월별 수익 표"}],
        }
        server.data_sources["rev-ds-1"] = {
            "object": "data_source",
            "id": "rev-ds-1",
            "in_trash": False,
            "name": "월별 수익 표",
            "properties": {},
        }
        server.children[DEFAULT_SETTLEMENT_PAGE_ID].append(
            {
                "object": "block",
                "id": "rev-db-1",
                "type": "child_database",
                "in_trash": False,
                "child_database": {"title": "월별 수익 표"},
            }
        )
        server.ds_rows["rev-ds-1"] = [
            {
                "object": "page",
                "id": "rev-row-1",
                "in_trash": False,
                "properties": {
                    "항목": {"type": "title", "title": [{"plain_text": "11월 애드센스"}]},
                    "금액": {"type": "rich_text", "rich_text": [{"plain_text": "$12.50"}]},
                    "상태": {"type": "select", "select": {"name": "확인"}},
                },
            }
        ]

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        summary = inspect_settlement_page(client, DEFAULT_SETTLEMENT_PAGE_ID)

        self.assertTrue(summary.accessible)
        self.assertTrue(summary.structured)
        self.assertEqual(summary.structure_type, "database")
        self.assertEqual(summary.table_headers, ["항목", "금액", "상태"])
        self.assertEqual(summary.table_rows, [["11월 애드센스", "$12.50", "확인"]])

        # 정산판 페이지 및 하위 DB가 읽기 전용 보호 목록에 등록되어 쓰기가 차단되는지 확인
        with self.assertRaises(ReadOnlyGuardError):
            client.append_block_children(DEFAULT_SETTLEMENT_PAGE_ID, [{"type": "divider", "divider": {}}])
        with self.assertRaises(ReadOnlyGuardError):
            client.delete_block("rev-db-1")

        # 섹션 G 렌더링: 링크 + 핵심 요약 + 원문 표 포함, '정산: 수동 확인' 아님
        report = DashboardReport(
            updated_at_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=None,
            competition_analytics=None,
            settlement_summary=summary,
        )
        bottom = build_bottom_blocks(report)
        flat = "\n".join(extract_block_plain_text(b) for b in bottom)
        self.assertIn(DEFAULT_SETTLEMENT_PAGE_TITLE, flat)
        self.assertIn(DEFAULT_SETTLEMENT_PAGE_URL, flat)
        self.assertIn("정산판 핵심 요약", flat)
        self.assertNotIn("정산: 수동 확인", flat)

        g_idx = next(i for i, b in enumerate(bottom) if extract_block_plain_text(b) == "G. 정산")
        g_tables = [b for b in bottom[g_idx + 1 :] if b.get("type") == "table"]
        self.assertEqual(len(g_tables), 1)
        data_row_cells = [
            cell[0]["text"]["content"]
            for cell in g_tables[0]["table"]["children"][1]["table_row"]["cells"]
        ]
        self.assertEqual(data_row_cells, ["11월 애드센스", "$12.50", "확인"])

    def test_settlement_structured_table_block_rendering(self) -> None:
        server = FakeFactNotionServer()
        server.add_page(
            DEFAULT_SETTLEMENT_PAGE_ID,
            DEFAULT_SETTLEMENT_PAGE_TITLE,
            blocks=[
                {
                    "object": "block",
                    "id": "tbl-1",
                    "type": "table",
                    "in_trash": False,
                    "table": {"table_width": 2, "has_column_header": True},
                }
            ],
        )
        server.children["tbl-1"] = [
            {
                "object": "block",
                "id": "tr-0",
                "type": "table_row",
                "in_trash": False,
                "table_row": {"cells": [[{"plain_text": "구분"}], [{"plain_text": "누적"}]]},
            },
            {
                "object": "block",
                "id": "tr-1",
                "type": "table_row",
                "in_trash": False,
                "table_row": {"cells": [[{"plain_text": "체험단"}], [{"plain_text": "5건"}]]},
            },
        ]

        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        summary = inspect_settlement_page(client, DEFAULT_SETTLEMENT_PAGE_ID)
        self.assertTrue(summary.structured)
        self.assertEqual(summary.structure_type, "table")
        self.assertEqual(summary.table_headers, ["구분", "누적"])
        self.assertEqual(summary.table_rows, [["체험단", "5건"]])

    def test_settlement_unstructured_and_inaccessible_keep_link_and_manual_note(self) -> None:
        # (A) 비구조화 페이지(체크리스트/문단만 존재)
        server = FakeFactNotionServer()
        server.add_page(
            DEFAULT_SETTLEMENT_PAGE_ID,
            DEFAULT_SETTLEMENT_PAGE_TITLE,
            blocks=[
                {
                    "object": "block",
                    "id": "todo-1",
                    "type": "to_do",
                    "in_trash": False,
                    "to_do": {"rich_text": [{"plain_text": "월말 정산 확인"}], "checked": False},
                }
            ],
        )
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=server.transport)
        unstruct = inspect_settlement_page(client, DEFAULT_SETTLEMENT_PAGE_ID)
        self.assertTrue(unstruct.accessible)
        self.assertFalse(unstruct.structured)
        self.assertEqual(unstruct.structure_type, "unstructured")

        report_u = DashboardReport(
            updated_at_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=None,
            competition_analytics=None,
            settlement_summary=unstruct,
        )
        flat_u = "\n".join(extract_block_plain_text(b) for b in build_bottom_blocks(report_u))
        self.assertIn(DEFAULT_SETTLEMENT_PAGE_URL, flat_u)
        self.assertIn("정산: 수동 확인", flat_u)

        # (B) 접근 불가 (404/미공유) -> 추측 금지, 링크 + '정산: 수동 확인' + 실패 사유 표시
        empty_server = FakeFactNotionServer()
        client_err = NotionClient("secret_test", min_interval_sec=0.0, transport=empty_server.transport)
        err_summary = inspect_settlement_page(client_err, DEFAULT_SETTLEMENT_PAGE_ID)
        self.assertFalse(err_summary.accessible)
        self.assertFalse(err_summary.structured)
        self.assertEqual(err_summary.structure_type, "error")
        self.assertIsNotNone(err_summary.error_message)

        report_e = DashboardReport(
            updated_at_kst=datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST),
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=None,
            competition_analytics=None,
            settlement_summary=err_summary,
        )
        flat_e = "\n".join(extract_block_plain_text(b) for b in build_bottom_blocks(report_e))
        self.assertIn(DEFAULT_SETTLEMENT_PAGE_URL, flat_e)
        self.assertIn("정산: 수동 확인", flat_e)
        self.assertIn("404", flat_e)

    def test_settlement_page_url_normalization_in_config(self) -> None:
        self.assertEqual(
            normalize_notion_id("https://app.notion.com/p/41a89944f230817a9849d34d51978411"),
            DEFAULT_SETTLEMENT_PAGE_ID,
        )
        cfg = AppConfig.from_env(
            {
                "NOTION_TOKEN": "secret_x",
                "NOTION_SETTLEMENT_PAGE_ID": "https://app.notion.com/p/41a89944f230817a9849d34d51978411",
            }
        )
        self.assertEqual(cfg.settlement_page_id, DEFAULT_SETTLEMENT_PAGE_ID)

    # ------------------------------------------------------------------
    # 후속 수정 5: README에 저장소 폴더 경로 기재 확인 (섹션 8-3)
    # ------------------------------------------------------------------

    def test_readme_contains_repo_folder_paths(self) -> None:
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("~/workspace/work/kkultong-hq-local", readme)
        self.assertIn("github.com/plazalabs/kkultong-hq", readme)


if __name__ == "__main__":
    unittest.main()
