"""P3 에이전트 성적표 (대시보드 섹션 H — 📒 팩트 장부 기준) 회귀 테스트.

검증 범위:
1. 올린 이 / 정정한 이 조합별 카운트 (본인 정정, 타 에이전트 정정, 회장님 정정, 대상 외 올린 이, 빈 값, 공백)
2. 유지율 계산 및 내려간 행·자동 경보 수 판정 기준 (확인됨/불일치/발췌 사라짐/확인 불가/열림 실패/로그인 필요/대체됨)
3. 표본 부족 표시 (올린 팩트 수 5 미만 경계값)
4. 집계 불가 표시 (분모 0, 스키마 필수 필드 누락, 팩트 장부 조회 실패)
5. 표기 규칙 준수 (G 섹션 다음 배치, 고정 순서, 순위·서열·우열·판정 문구 및 성과 색칠 금지)
6. 데이터 출처 격리 (오직 📒 팩트 장부만 사용, 경쟁 기록 DB 미사용, 읽기 전용 무수정)
7. runner 종단간 파이프라인 통합 검증 (기존 섹션 A~G 동작·숫자 불변)
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from kkultong_dash import (
    DEFAULT_COMPETITION_DATA_SOURCE_ID,
    DEFAULT_EXPERIENCE_DATA_SOURCE_ID,
    KST,
    PARENT_PAGE_TITLE,
    SCOREBOARD_AGENTS,
)
from kkultong_dash.analytics import compute_competition_analytics
from kkultong_dash.cache import LocalCache
from kkultong_dash.config import AppConfig
from kkultong_dash.fact_ledger import ensure_fact_ledger_database
from kkultong_dash.models import (
    AgentScorecard,
    CompetitionRow,
    DashboardReport,
    FactLedgerRow,
    SourceSyncResult,
)
from kkultong_dash.notion_client import HTTPResponse, NotionClient
from kkultong_dash.parsers import extract_block_plain_text
from kkultong_dash.renderer import build_bottom_blocks, format_cli_summary
from kkultong_dash.runner import EXIT_OK, run_dashboard_update
from kkultong_dash.scorecard import (
    compute_agent_scorecard,
    is_fact_row_auto_alert_downgraded,
    is_fact_row_downgraded,
)
from tests.test_fact_ledger import FakeFactNotionServer
from tests.test_publisher import FakeNotionServer
from tests.test_runner import _comp_row, _exp_row


def _fact(
    page_id: str,
    *,
    uploader: str | None = None,
    corrector: str | None = None,
    grade: str | None = "확인됨",
    auto_revalidation_result: str | None = None,
    conflict_ids: tuple[str, ...] = (),
    superseded_ids: tuple[str, ...] = (),
) -> FactLedgerRow:
    return FactLedgerRow(
        page_id=page_id,
        claim=f"주장-{page_id}",
        topic_key=f"topic.{page_id}",
        value="10%",
        source_url="https://example.com/doc",
        excerpt="10% fee",
        grade=grade,
        auto_revalidation_result=auto_revalidation_result,
        conflict_ids=conflict_ids,
        superseded_ids=superseded_ids,
        uploader=uploader,
        corrector=corrector,
    )


def _extract_table_matrix(table_blk: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_blk in table_blk["table"]["children"]:
        cells = row_blk["table_row"]["cells"]
        rows.append(["".join(rt["text"]["content"] for rt in cell) for cell in cells])
    return rows


class TestAgentScorecardP3(unittest.TestCase):
    def setUp(self) -> None:
        self.now_kst = datetime(2026, 11, 21, 11, 20, 5, tzinfo=KST)

    # ------------------------------------------------------------------
    # 1) 올린 이 / 정정한 이 조합별 카운트
    # ------------------------------------------------------------------

    def test_uploader_and_corrector_combinations(self) -> None:
        rows = [
            # 본인 정정: 뮤즈 올림 + 뮤즈 정정 -> 올린 +1, 정정당한 0, 잡아낸 0
            _fact("f1", uploader="뮤즈", corrector="뮤즈", grade="확인됨"),
            # 타 에이전트 정정: 뮤즈 올림 + 피클 정정 -> 뮤즈(올린+1, 정정당한+1), 피클(잡아낸+1)
            _fact("f2", uploader="뮤즈", corrector="피클", grade="확인됨"),
            # 공백 포함 이름 정규화: " 뮤즈 " 올림 + " 큐 " 정정 -> 뮤즈(올린+1, 정정당한+1), 큐(잡아낸+1)
            _fact("f3", uploader=" 뮤즈 ", corrector=" 큐 ", grade="확인됨"),
            # 비대상자('회장님')가 정정: 피클 올림 + 회장님 정정 -> 피클(올린+1, 정정당한+1), 잡아낸 변동 없음
            _fact("f4", uploader="피클", corrector="회장님", grade="확인됨"),
            # 비대상자('회장님')가 올린 행을 에이전트(큐)가 정정 -> 큐 잡아낸 0 ('올린 이'가 대상 에이전트 아님)
            _fact("f5", uploader="회장님", corrector="큐", grade="확인됨"),
            # '올린 이' 미입력 행을 인스팅트가 정정 -> 인스팅트 잡아낸 0
            _fact("f6", uploader=None, corrector="인스팅트", grade="확인됨"),
            # '정정한 이'가 공백 문자열 -> 정정당한 0
            _fact("f7", uploader="클실장", corrector="   ", grade="확인됨"),
            # 정상 미정정 행
            _fact("f8", uploader="인스팅트", corrector=None, grade="확인됨"),
        ]

        sc = compute_agent_scorecard(rows)
        self.assertTrue(sc.ok)
        self.assertEqual(sc.total_ledger_rows, len(rows))
        self.assertEqual([r.agent for r in sc.agent_rows], list(SCOREBOARD_AGENTS))

        by_agent = {r.agent: r for r in sc.agent_rows}

        # 뮤즈: f1, f2, f3 -> 올린 3, 정정당한 2(f2, f3), 잡아낸 0
        self.assertEqual(by_agent["뮤즈"].uploaded_count, 3)
        self.assertEqual(by_agent["뮤즈"].corrected_against_count, 2)
        self.assertEqual(by_agent["뮤즈"].caught_count, 0)

        # 피클: f4 -> 올린 1, 정정당한 1(f4), 잡아낸 1(f2)
        self.assertEqual(by_agent["피클"].uploaded_count, 1)
        self.assertEqual(by_agent["피클"].corrected_against_count, 1)
        self.assertEqual(by_agent["피클"].caught_count, 1)

        # 큐: 올린 0, 정정당한 0, 잡아낸 1(f3; f5는 회장님 올린 행이라 제외)
        self.assertEqual(by_agent["큐"].uploaded_count, 0)
        self.assertEqual(by_agent["큐"].corrected_against_count, 0)
        self.assertEqual(by_agent["큐"].caught_count, 1)

        # 인스팅트: f8 -> 올린 1, 정정당한 0, 잡아낸 0(f6은 올린 이 미입력이라 제외)
        self.assertEqual(by_agent["인스팅트"].uploaded_count, 1)
        self.assertEqual(by_agent["인스팅트"].corrected_against_count, 0)
        self.assertEqual(by_agent["인스팅트"].caught_count, 0)

        # 클실장: f7 -> 올린 1, 정정당한 0(공백 corrector 무시), 잡아낸 0
        self.assertEqual(by_agent["클실장"].uploaded_count, 1)
        self.assertEqual(by_agent["클실장"].corrected_against_count, 0)
        self.assertEqual(by_agent["클실장"].caught_count, 0)

    # ------------------------------------------------------------------
    # 2) 유지율 계산 및 내려간 행 / 자동 경보 수 판정 기준
    # ------------------------------------------------------------------

    def test_retention_rate_downgraded_and_auto_alert_criteria(self) -> None:
        # 개별 행 판별 함수 단위 검증
        self.assertFalse(is_fact_row_downgraded(_fact("ok", grade="확인됨")))
        self.assertFalse(
            is_fact_row_downgraded(_fact("ok-old-res", grade="확인됨", auto_revalidation_result="발췌 사라짐"))
        )
        self.assertFalse(
            is_fact_row_auto_alert_downgraded(_fact("ok-old-res2", grade="확인됨", auto_revalidation_result="발췌 사라짐"))
        )

        # 발췌 사라짐 + 원문대조필요 -> 내려간 행 O, 자동 경보 O
        row_missing_excerpt = _fact("d1", uploader="뮤즈", grade="원문대조필요", auto_revalidation_result="발췌 사라짐")
        self.assertTrue(is_fact_row_downgraded(row_missing_excerpt))
        self.assertTrue(is_fact_row_auto_alert_downgraded(row_missing_excerpt))

        # 충돌 + 불일치 (자동 충돌 판정) -> 내려간 행 O, 자동 경보 O
        row_auto_conflict = _fact(
            "d2",
            uploader="뮤즈",
            grade="불일치",
            auto_revalidation_result="발췌 그대로",
            conflict_ids=("peer-1",),
        )
        self.assertTrue(is_fact_row_downgraded(row_auto_conflict))
        self.assertTrue(is_fact_row_auto_alert_downgraded(row_auto_conflict))

        # 수동 불일치 (충돌 relation 비어 있음) -> 내려간 행 O, 자동 경보 X
        row_manual_conflict = _fact("d3", uploader="뮤즈", grade="불일치", conflict_ids=())
        self.assertTrue(is_fact_row_downgraded(row_manual_conflict))
        self.assertFalse(is_fact_row_auto_alert_downgraded(row_manual_conflict))

        # 제외 대상: 자동확인불가(로그인 필요), 원문대조필요(확인 불가/열림 실패/발췌 그대로/None), 미확인, 대체됨
        excluded_rows = [
            _fact("x1", uploader="뮤즈", grade="자동확인불가", auto_revalidation_result="로그인 필요"),
            _fact("x2", uploader="뮤즈", grade="원문대조필요", auto_revalidation_result="확인 불가"),
            _fact("x3", uploader="뮤즈", grade="원문대조필요", auto_revalidation_result="열림 실패 (HTTP 403)"),
            _fact("x4", uploader="뮤즈", grade="원문대조필요", auto_revalidation_result="발췌 그대로"),
            _fact("x5", uploader="뮤즈", grade="원문대조필요", auto_revalidation_result=None),
            _fact("x6", uploader="뮤즈", grade="미확인", auto_revalidation_result=None),
            _fact("x7", uploader="뮤즈", grade="미확인", superseded_ids=("newer-1",)),
        ]
        for r in excluded_rows:
            self.assertFalse(is_fact_row_downgraded(r), msg=r.page_id)
            self.assertFalse(is_fact_row_auto_alert_downgraded(r), msg=r.page_id)

        # 전체 집계 검증 (뮤즈: 유지 3 + 내려간 3 + 제외 7 = 올린 13건)
        rows = [
            _fact("m1", uploader="뮤즈", grade="확인됨", auto_revalidation_result="발췌 그대로"),
            _fact("m2", uploader="뮤즈", grade="확인됨", auto_revalidation_result=None),
            _fact("m3", uploader="뮤즈", grade="확인됨", auto_revalidation_result="발췌 사라짐"),
            row_missing_excerpt,
            row_auto_conflict,
            row_manual_conflict,
            *excluded_rows,
        ]
        sc = compute_agent_scorecard(rows)
        muse = {r.agent: r for r in sc.agent_rows}["뮤즈"]

        self.assertEqual(muse.uploaded_count, 13)
        self.assertEqual(muse.maintained_count, 3)
        self.assertEqual(muse.downgraded_count, 3)
        self.assertEqual(muse.retention_denominator, 6)
        self.assertEqual(muse.retention_label(), "50.0% (3/6)")
        self.assertEqual(muse.auto_alert_count, 2)  # d1(발췌 사라짐) + d2(충돌 relation 불일치)

    # ------------------------------------------------------------------
    # 3) 표본 부족 경계값 (올린 팩트 수 5 미만)
    # ------------------------------------------------------------------

    def test_low_sample_boundary_under_five_uploads(self) -> None:
        rows = [
            # 피클: 올린 4건 (유지 3, 내려간 1) -> 75.0% (3/4, 표본 부족)
            _fact("p1", uploader="피클", grade="확인됨"),
            _fact("p2", uploader="피클", grade="확인됨"),
            _fact("p3", uploader="피클", grade="확인됨"),
            _fact("p4", uploader="피클", grade="불일치", conflict_ids=("other",)),
            # 큐: 올린 5건 (유지 2, 내려간 1, 미확인 2) -> 올린 팩트 수 5이므로 표본 부족 미표기!
            _fact("q1", uploader="큐", grade="확인됨"),
            _fact("q2", uploader="큐", grade="확인됨"),
            _fact("q3", uploader="큐", grade="원문대조필요", auto_revalidation_result="발췌 사라짐"),
            _fact("q4", uploader="큐", grade="미확인"),
            _fact("q5", uploader="큐", grade="미확인"),
            # 인스팅트: 올린 5건 (유지 4, 내려간 1) -> 80.0% (4/5)
            _fact("i1", uploader="인스팅트", grade="확인됨"),
            _fact("i2", uploader="인스팅트", grade="확인됨"),
            _fact("i3", uploader="인스팅트", grade="확인됨"),
            _fact("i4", uploader="인스팅트", grade="확인됨"),
            _fact("i5", uploader="인스팅트", grade="불일치", conflict_ids=("other",)),
        ]

        sc = compute_agent_scorecard(rows)
        by_agent = {r.agent: r for r in sc.agent_rows}

        self.assertTrue(by_agent["뮤즈"].is_low_sample)
        self.assertEqual(by_agent["뮤즈"].retention_label(), "집계 불가 (표본 부족)")

        self.assertTrue(by_agent["피클"].is_low_sample)
        self.assertEqual(by_agent["피클"].retention_label(), "75.0% (3/4, 표본 부족)")

        self.assertFalse(by_agent["큐"].is_low_sample)
        self.assertEqual(by_agent["큐"].retention_label(), "66.7% (2/3)")
        self.assertNotIn("표본 부족", by_agent["큐"].retention_label())

        self.assertFalse(by_agent["인스팅트"].is_low_sample)
        self.assertEqual(by_agent["인스팅트"].retention_label(), "80.0% (4/5)")

    # ------------------------------------------------------------------
    # 4) 집계 불가 표시 (분모 0 / 필수 스키마 필드 누락 / 장부 조회 실패)
    # ------------------------------------------------------------------

    def test_uncomputable_display_for_zero_denominator_missing_fields_and_errors(self) -> None:
        # (A) 올린 팩트 수 >= 5이지만 유지 중 + 내려간 행 == 0 -> '집계 불가' (표본 부족 미표기)
        #     올린 팩트 수 < 5이고 분모 == 0 -> '집계 불가 (표본 부족)'
        rows = [
            *[_fact(f"m{i}", uploader="뮤즈", grade="미확인") for i in range(6)],
            *[_fact(f"p{i}", uploader="피클", grade="원문대조필요", auto_revalidation_result="확인 불가") for i in range(3)],
        ]
        sc_zero_denom = compute_agent_scorecard(rows)
        by_agent = {r.agent: r for r in sc_zero_denom.agent_rows}
        self.assertEqual(by_agent["뮤즈"].uploaded_count, 6)
        self.assertEqual(by_agent["뮤즈"].retention_label(), "집계 불가")
        self.assertEqual(by_agent["피클"].uploaded_count, 3)
        self.assertEqual(by_agent["피클"].retention_label(), "집계 불가 (표본 부족)")

        # (B) 스키마에서 '자동 재검증 결과'가 누락된 경우 -> 유지율·자동 경보 수 '집계 불가', 나머지는 정상 집계
        sample_rows = [
            _fact("s1", uploader="뮤즈", corrector="피클", grade="확인됨"),
            _fact("s2", uploader="뮤즈", grade="확인됨"),
            _fact("s3", uploader="뮤즈", grade="확인됨"),
            _fact("s4", uploader="뮤즈", grade="확인됨"),
            _fact("s5", uploader="뮤즈", grade="확인됨"),
        ]
        sc_no_reval = compute_agent_scorecard(
            sample_rows,
            available_properties={"주장", "등급", "올린 이", "정정한 이", "충돌"},
        )
        self.assertEqual(sc_no_reval.missing_fields, ("자동 재검증 결과",))
        muse_nr = {r.agent: r for r in sc_no_reval.agent_rows}["뮤즈"]
        pickle_nr = {r.agent: r for r in sc_no_reval.agent_rows}["피클"]
        self.assertEqual(muse_nr.count_label(muse_nr.uploaded_count), "5")
        self.assertEqual(muse_nr.retention_label(), "집계 불가")
        self.assertEqual(muse_nr.count_label(muse_nr.corrected_against_count), "1")
        self.assertEqual(muse_nr.count_label(muse_nr.auto_alert_count), "집계 불가")
        self.assertEqual(pickle_nr.count_label(pickle_nr.caught_count), "1")
        self.assertEqual(pickle_nr.retention_label(), "집계 불가 (표본 부족)")

        # (C) 스키마에서 '충돌'만 누락된 경우 -> 유지율은 집계 가능, 자동 경보 수만 '집계 불가'
        sc_no_conflict = compute_agent_scorecard(
            sample_rows,
            available_properties={"주장", "등급", "올린 이", "정정한 이", "자동 재검증 결과"},
        )
        muse_nc = {r.agent: r for r in sc_no_conflict.agent_rows}["뮤즈"]
        self.assertEqual(muse_nc.retention_label(), "100.0% (5/5)")
        self.assertEqual(muse_nc.count_label(muse_nc.auto_alert_count), "집계 불가")

        # (D) 팩트 장부 조회 실패 -> 섹션 H 콜아웃에 '집계 불가'와 실패 원인 표시, 표 미출력
        failed_sc = AgentScorecard(ok=False, error_message="HTTP 403 권한 없음")
        report_err = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=None,
            competition_analytics=None,
            agent_scorecard=failed_sc,
        )
        bottom = build_bottom_blocks(report_err)
        h_idx = next(
            i for i, b in enumerate(bottom) if extract_block_plain_text(b) == "H. 에이전트 성적표 (팩트 장부 기준)"
        )
        h_blocks = bottom[h_idx:]
        h_text = "\n".join(extract_block_plain_text(b) for b in h_blocks)
        self.assertIn("집계 불가", h_text)
        self.assertIn("HTTP 403", h_text)
        self.assertEqual([b for b in h_blocks if b.get("type") == "table"], [])

    # ------------------------------------------------------------------
    # 5) 표기 규칙 준수: G 섹션 다음 배치, 고정 순서, 순위·서열·우열·판정 문구 금지
    # ------------------------------------------------------------------

    def test_section_h_layout_fixed_order_and_no_ranking_or_judgment_text(self) -> None:
        # 클실장이 가장 많고 유지율 100%, 뮤즈가 0건인 픽스처 -> 그래도 순서는 뮤즈·피클·큐·인스팅트·클실장 고정!
        rows = [
            *[_fact(f"k{i}", uploader="클실장", grade="확인됨") for i in range(6)],
            _fact("i1", uploader="인스팅트", grade="확인됨"),
            _fact("i2", uploader="인스팅트", grade="불일치", conflict_ids=("other",)),
        ]
        sc = compute_agent_scorecard(rows)
        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=None,
            competition_analytics=None,
            agent_scorecard=sc,
        )

        bottom = build_bottom_blocks(report)
        texts = [extract_block_plain_text(b) for b in bottom]
        idx_f = texts.index("F. 경쟁 기록 요약")
        idx_g = texts.index("G. 정산")
        idx_h = texts.index("H. 에이전트 성적표 (팩트 장부 기준)")
        self.assertTrue(idx_f < idx_g < idx_h)

        h_blocks = bottom[idx_h:]
        h_table = next(b for b in h_blocks if b.get("type") == "table")
        matrix = _extract_table_matrix(h_table)

        self.assertEqual(
            matrix[0],
            ["에이전트", "올린 팩트 수", "유지율", "정정당한 수", "잡아낸 수", "자동 경보 수"],
        )
        self.assertEqual([r[0] for r in matrix[1:]], list(SCOREBOARD_AGENTS))
        self.assertEqual(matrix[1], ["뮤즈", "0", "집계 불가 (표본 부족)", "0", "0", "0"])
        self.assertEqual(matrix[4], ["인스팅트", "2", "50.0% (1/2, 표본 부족)", "0", "0", "1"])
        self.assertEqual(matrix[5], ["클실장", "6", "100.0% (6/6)", "0", "0", "0"])

        # 순위·서열·우열·판정 문구 금지 검증
        h_all_text = "\n".join(
            [extract_block_plain_text(b) for b in h_blocks]
            + [" ".join(row) for row in matrix]
        )
        forbidden_tokens = (
            "1위",
            "2위",
            "3위",
            "순위",
            "서열",
            "랭킹",
            "우열",
            "최고",
            "최저",
            "우수",
            "미흡",
            "합격",
            "불합격",
            "판정",
            "평가",
            "선두",
            "꼴찌",
            "잘함",
            "못함",
            "🥇",
            "🥈",
            "🥉",
            "🏆",
        )
        for token in forbidden_tokens:
            self.assertNotIn(token, h_all_text, msg=f"섹션 H에 금지 문구 포함: {token!r}")

        # 모든 데이터 셀이 성과 색칠 없이 default 색상인지 확인
        for row_blk in h_table["table"]["children"][1:]:
            for cell in row_blk["table_row"]["cells"]:
                for rt in cell:
                    self.assertEqual(rt["annotations"]["color"], "default")

    # ------------------------------------------------------------------
    # 6) 데이터 출처 격리: 경쟁 기록 DB의 자기 신고 등급을 절대 사용하지 않음
    # ------------------------------------------------------------------

    def test_section_h_never_uses_competition_db_grades(self) -> None:
        now_utc = self.now_kst.astimezone(timezone.utc)
        comp_rows = [
            CompetitionRow(f"c{i}", f"기록{i}", None, "뮤즈", "꿀통", "확인됨", None, "", now_utc, now_utc)
            for i in range(10)
        ]
        comp_analytics = compute_competition_analytics(comp_rows, now_kst=self.now_kst)

        # 팩트 장부가 빈 상태(0건)일 때: 경쟁 기록에 뮤즈 '확인됨' 10건이 있어도 섹션 H는 0건/집계 불가!
        empty_sc = compute_agent_scorecard([])
        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 10, 10),
            experience_analytics=None,
            competition_analytics=comp_analytics,
            agent_scorecard=empty_sc,
        )
        bottom = build_bottom_blocks(report)
        h_idx = next(
            i for i, b in enumerate(bottom) if extract_block_plain_text(b) == "H. 에이전트 성적표 (팩트 장부 기준)"
        )
        h_table = next(b for b in bottom[h_idx + 1 :] if b.get("type") == "table")
        matrix = _extract_table_matrix(h_table)
        self.assertEqual(matrix[1], ["뮤즈", "0", "집계 불가 (표본 부족)", "0", "0", "0"])

    # ------------------------------------------------------------------
    # 7) 종단간 통합 테스트: runner가 팩트 장부를 읽기 전용으로 읽어 섹션 H 출력 + A~G 불변
    # ------------------------------------------------------------------

    def test_end_to_end_runner_renders_section_h_read_only_and_keeps_a_to_g_intact(self) -> None:
        fact_server = FakeFactNotionServer()
        fact_server.add_page("hub-parent-1", PARENT_PAGE_TITLE)
        dash_server = FakeNotionServer()
        dash_server.add_page("hub-parent-1", PARENT_PAGE_TITLE)

        # 1) P2 방식으로 팩트 장부 DB 및 시드 행 준비
        setup_client = NotionClient("secret_test", min_interval_sec=0.0, transport=fact_server.transport)
        with LocalCache(":memory:") as setup_cache:
            init_res = ensure_fact_ledger_database(setup_client, setup_cache)
            fact_ds_id = init_res.data_source_id
            fact_db_id = init_res.database_id

        # 팩트 장부 행 추가 (올린 이/정정한 이/등급/재검증 결과/충돌 포함 + 휴지통 행 1건 제외 검증)
        def _add_ledger_page(
            pid: str,
            *,
            uploader: str | None,
            corrector: str | None,
            grade: str,
            reval: str | None = None,
            conflict_ids: list[str] | None = None,
            in_trash: bool = False,
        ) -> None:
            page = fact_server.add_fact_row(
                fact_ds_id,
                pid,
                claim=f"주장 {pid}",
                topic_key=f"topic.{pid}",
                value="10%",
                source_url="https://example.com/p",
                excerpt="10%",
                grade=grade,
                conflict_ids=conflict_ids,
            )
            page["in_trash"] = in_trash
            page["properties"]["올린 이"] = {
                "type": "select",
                "select": {"name": uploader} if uploader else None,
            }
            page["properties"]["정정한 이"] = {
                "type": "select",
                "select": {"name": corrector} if corrector else None,
            }
            page["properties"]["자동 재검증 결과"] = {
                "type": "select",
                "select": {"name": reval} if reval else None,
            }

        for idx in range(4):
            _add_ledger_page(f"fl-m-ok-{idx}", uploader="뮤즈", corrector=None, grade="확인됨", reval="발췌 그대로")
        _add_ledger_page("fl-m-down", uploader="뮤즈", corrector="큐", grade="원문대조필요", reval="발췌 사라짐")
        _add_ledger_page("fl-p-conf", uploader="피클", corrector="뮤즈", grade="불일치", reval="발췌 그대로", conflict_ids=["fl-q-conf"])
        _add_ledger_page("fl-q-conf", uploader="큐", corrector=None, grade="불일치", reval="발췌 그대로", conflict_ids=["fl-p-conf"])
        _add_ledger_page("fl-trashed", uploader="뮤즈", corrector=None, grade="확인됨", in_trash=True)

        # 부모 페이지 하위에 팩트 장부 child_database 블록도 dash_server에 등록
        dash_server.children["hub-parent-1"].append(
            {
                "object": "block",
                "id": fact_db_id,
                "type": "child_database",
                "in_trash": False,
                "child_database": {"title": "📒 팩트 장부"},
            }
        )

        exp_rows = [
            _exp_row("e1", "백김치 체험", "레뷰", "당첨", "2026-11-30", "백김치 1kg"),
            _exp_row("e2", "커피 체험", "레뷰", "미당첨", None, "아메리카노"),
        ]
        comp_rows = [
            _comp_row("c1", "꿀통 1", "뮤즈", "꿀통", "확인됨"),
            _comp_row("c2", "BM 초안", "피클", "BM 제안", "미확인"),
        ]

        def combined_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> HTTPResponse:
            if f"/data_sources/{DEFAULT_EXPERIENCE_DATA_SOURCE_ID}/query" in url:
                return HTTPResponse(200, {}, json.dumps({"object": "list", "results": exp_rows, "has_more": False}))
            if f"/data_sources/{DEFAULT_COMPETITION_DATA_SOURCE_ID}/query" in url:
                return HTTPResponse(200, {}, json.dumps({"object": "list", "results": comp_rows, "has_more": False}))
            if f"/databases/{fact_db_id}" in url or f"/data_sources/{fact_ds_id}" in url:
                return fact_server.transport(method, url, headers, body, timeout)
            return dash_server.transport(method, url, headers, body, timeout)

        fact_server.page_update_calls.clear()
        client = NotionClient("secret_test", min_interval_sec=0.0, transport=combined_transport)
        cfg = AppConfig(notion_token="secret_test")

        with LocalCache(":memory:") as cache:
            outcome = run_dashboard_update(
                cfg,
                now_kst=self.now_kst,
                client=client,
                cache=cache,
            )

        # A~G 상태 및 숫자 유지 확인
        self.assertEqual(outcome.exit_code, EXIT_OK)
        self.assertEqual(outcome.report.run_status, "정상")
        self.assertIsNotNone(outcome.report.experience_analytics)
        self.assertEqual(outcome.report.experience_analytics.total_count, 2)
        self.assertIsNotNone(outcome.report.competition_analytics)
        self.assertEqual(outcome.report.competition_analytics.total_count, 2)

        # 팩트 장부 읽기 전용 확인 (페이지 수정/DB 재생성 0건)
        self.assertEqual(fact_server.page_update_calls, [])
        self.assertEqual(fact_server.db_create_calls, 1)

        # 섹션 H 집계값 검증 (휴지통 행 fl-trashed 제외 -> 전체 7건)
        sc = outcome.report.agent_scorecard
        self.assertIsNotNone(sc)
        self.assertTrue(sc.ok)
        self.assertEqual(sc.total_ledger_rows, 7)

        dash_id = outcome.publish_result.dashboard_page_id
        published_blocks = dash_server.children[dash_id]
        published_texts = [extract_block_plain_text(b) for b in published_blocks]
        idx_g = published_texts.index("G. 정산")
        idx_h = published_texts.index("H. 에이전트 성적표 (팩트 장부 기준)")
        self.assertLess(idx_g, idx_h)

        h_table = next(b for b in published_blocks[idx_h + 1 :] if b.get("type") == "table")
        matrix = _extract_table_matrix(h_table)
        self.assertEqual(matrix[1], ["뮤즈", "5", "80.0% (4/5)", "1", "1", "1"])
        self.assertEqual(matrix[2], ["피클", "1", "0.0% (0/1, 표본 부족)", "1", "0", "1"])
        self.assertEqual(matrix[3], ["큐", "1", "0.0% (0/1, 표본 부족)", "0", "1", "1"])
        self.assertEqual(matrix[4], ["인스팅트", "0", "집계 불가 (표본 부족)", "0", "0", "0"])
        self.assertEqual(matrix[5], ["클실장", "0", "집계 불가 (표본 부족)", "0", "0", "0"])

        # CLI 요약에도 중립적 섹션 H 요약 포함
        cli = format_cli_summary(outcome.report)
        self.assertIn("에이전트 성적표(팩트 장부 7건)", cli)
        self.assertIn("뮤즈(5건/80.0% (4/5))", cli)


if __name__ == "__main__":
    unittest.main()
