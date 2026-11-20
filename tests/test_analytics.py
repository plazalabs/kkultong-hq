"""섹션 B, C, D, F 집계 로직 및 렌더러 단위 테스트."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from kkultong_dash import KST
from kkultong_dash.analytics import (
    SELF_PURCHASE_RULE_TEXT,
    compute_competition_analytics,
    compute_experience_analytics,
    is_self_purchase_review,
)
from kkultong_dash.models import CompetitionRow, DashboardReport, ExperienceRow, SourceSyncResult
from kkultong_dash.parsers import (
    extract_block_plain_text,
    normalize_verify_grade,
    parse_competition_row,
    parse_experience_row,
)
from kkultong_dash.renderer import (
    VERIFY_GRADE_WARNING_TEXT,
    build_bottom_blocks,
    build_top_blocks,
    make_rich_text,
)


class TestAnalyticsAndRenderer(unittest.TestCase):
    def setUp(self) -> None:
        self.now_kst = datetime(2026, 11, 20, 11, 20, 5, tzinfo=KST)

    def test_section_b_deadlines_sorting_urgent_red_and_missing(self) -> None:
        rows = [
            ExperienceRow("1", "D+1 초과건", "레뷰", "식품", "고기세트", None, None, date(2026, 11, 19), "당첨", None, None),
            ExperienceRow("2", "D-Day 오늘", "리뷰노트", "생활", "세제", None, None, date(2026, 11, 20), "리뷰대기", None, None),
            ExperienceRow("3", "D-3 경계", "서울오빠", "뷰티", "크림", None, None, date(2026, 11, 23), "당첨", None, None),
            ExperienceRow("4", "D-4 여유", "쿠팡", "가전", "가습기", None, None, date(2026, 11, 24), "리뷰대기", None, None),
            ExperienceRow("5", "마감일 누락", "무신사", "의류", "후드티", None, date(2026, 11, 18), None, "당첨", None, None),
            ExperienceRow("6", "완료 제외", "레뷰", "식품", "커피", None, None, date(2026, 11, 21), "리뷰완료", None, None),
            ExperienceRow("7", "미당첨 제외", "레뷰", "식품", "빵", None, None, date(2026, 11, 21), "미당첨", None, None),
        ]

        res = compute_experience_analytics(rows, now_kst=self.now_kst)

        self.assertEqual([d.campaign_name for d in res.deadlines], ["D+1 초과건", "D-Day 오늘", "D-3 경계", "D-4 여유"])
        self.assertEqual([d.d_day for d in res.deadlines], [-1, 0, 3, 4])
        self.assertEqual([d.is_urgent for d in res.deadlines], [True, True, True, False])
        self.assertEqual([d.d_day_label for d in res.deadlines], ["D+1 (초과)", "D-Day", "D-3", "D-4"])

        self.assertEqual(len(res.missing_deadlines), 1)
        self.assertEqual(res.missing_deadlines[0].campaign_name, "마감일 누락")

        # 렌더러에서 D-3 이하는 빨간색(color='red'), D-4는 기본색(color='default') 확인
        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", len(rows), len(rows)),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=res,
            competition_analytics=None,
        )
        top_blocks = build_top_blocks(report)
        # 섹션 B 첫 번째 테이블 찾기
        tables = [b for b in top_blocks if b.get("type") == "table"]
        deadline_table = tables[0]
        body_rows = deadline_table["table"]["children"][1:]
        colors = [row["table_row"]["cells"][0][0]["annotations"]["color"] for row in body_rows]
        self.assertEqual(colors, ["red", "red", "red", "default"])

    def test_section_c_win_rate_and_sample_size(self) -> None:
        rows = [
            ExperienceRow("1", "A", "레뷰", "식품", "x", None, None, None, "당첨", None, None),
            ExperienceRow("2", "B", "레뷰", "식품", "x", None, None, None, "리뷰대기", None, None),
            ExperienceRow("3", "C", "레뷰", "식품", "x", None, None, None, "리뷰완료", None, None),
            ExperienceRow("4", "D", "레뷰", "식품", "x", None, None, None, "미당첨", None, None),
            ExperienceRow("5", "E", "레뷰", "식품", "x", None, None, None, "신청완료", None, None),
            ExperienceRow("6", "F", "쿠팡", "생활", "x", None, None, None, "발표대기", None, None),
        ]

        res = compute_experience_analytics(rows, now_kst=self.now_kst)
        self.assertEqual(res.total_count, 6)
        self.assertEqual(sum(cnt for _, cnt in res.status_counts), 6)

        by_site = {s.site: s for s in res.site_stats}
        revu = by_site["레뷰"]
        self.assertEqual(revu.applied_count, 5)
        self.assertEqual(revu.won_count, 3)
        self.assertEqual(revu.announced_count, 4)
        self.assertEqual(revu.win_rate_label(), "75.0% (n=4)")

        coupang = by_site["쿠팡"]
        self.assertEqual(coupang.applied_count, 1)
        self.assertEqual(coupang.announced_count, 0)
        self.assertEqual(coupang.win_rate_label(), "- (n=0)")

        self.assertEqual(res.total_site_stats.win_rate_label(), "75.0% (n=4)")

    def test_section_d_verbatim_reward_text_no_sum(self) -> None:
        raw_reward = "한우 500g + 2만원 식사권 (부가세 별도, 금액 미정)"
        rows = [
            ExperienceRow("1", "한우 체험", "서울오빠", "식품", raw_reward, None, None, None, "당첨", None, None),
            ExperienceRow("2", "미당첨 건", "서울오빠", "식품", "10만원 상품권", None, None, None, "미당첨", None, None),
            ExperienceRow("3", "리뷰완료 건", "미블", "뷰티", "스킨케어 3종", None, None, None, "리뷰완료", None, None),
        ]
        res = compute_experience_analytics(rows, now_kst=self.now_kst)
        self.assertEqual(len(res.rewards), 2)
        texts = {r.campaign_name: r.reward_text for r in res.rewards}
        self.assertEqual(texts["한우 체험"], raw_reward)
        self.assertEqual(texts["리뷰완료 건"], "스킨케어 3종")

    def test_section_f_24h_matrix_and_no_grade_upgrade(self) -> None:
        # now_kst = 2026-11-20 11:20:05 KST = 2026-11-20 02:20:05 UTC
        inside_24h = datetime(2026, 11, 19, 2, 21, 0, tzinfo=timezone.utc)
        outside_24h = datetime(2026, 11, 19, 2, 19, 0, tzinfo=timezone.utc)

        # 규칙 5 + 수정 2 테스트:
        # 빈 값은 '미확인'에 합치지 않고 '빈 값'으로 분리하며, 알 수 없는 값은 '확인됨'으로 올리지 않고 '미확인'으로 내려감
        self.assertEqual(normalize_verify_grade(None), "빈 값")
        self.assertEqual(normalize_verify_grade(""), "빈 값")
        self.assertEqual(normalize_verify_grade("   "), "빈 값")
        self.assertEqual(normalize_verify_grade("임의승인"), "미확인")
        self.assertEqual(normalize_verify_grade("확인됨"), "확인됨")

        raw_pages = [
            {
                "id": "c1",
                "created_time": inside_24h.isoformat(),
                "last_edited_time": inside_24h.isoformat(),
                "properties": {
                    "내용": {"type": "title", "title": [{"plain_text": "꿀통 1"}]},
                    "담당": {"type": "select", "select": {"name": "뮤즈"}},
                    "구분": {"type": "select", "select": {"name": "꿀통"}},
                    "확인": {"type": "select", "select": {"name": "확인됨"}},
                },
            },
            {
                "id": "c2",
                "created_time": inside_24h.isoformat(),
                "last_edited_time": inside_24h.isoformat(),
                "properties": {
                    "내용": {"type": "title", "title": [{"plain_text": "점검 1"}]},
                    "담당": {"type": "select", "select": {"name": "뮤즈"}},
                    "구분": {"type": "select", "select": {"name": "점검"}},
                    "확인": {"type": "select", "select": None},  # 빈 값 -> '빈 값' 칸에 별도 집계
                },
            },
            {
                "id": "c3",
                "created_time": outside_24h.isoformat(),
                "last_edited_time": outside_24h.isoformat(),
                "properties": {
                    "내용": {"type": "title", "title": [{"plain_text": "검증 1"}]},
                    "담당": {"type": "select", "select": {"name": "피클"}},
                    "구분": {"type": "select", "select": {"name": "검증"}},
                    "확인": {"type": "select", "select": {"name": "원문대조필요"}},
                },
            },
            {
                "id": "c4",
                "created_time": outside_24h.isoformat(),
                "last_edited_time": outside_24h.isoformat(),
                "properties": {
                    "내용": {"type": "title", "title": [{"plain_text": "자료 1"}]},
                    "담당": {"type": "select", "select": {"name": "뮤즈"}},
                    "구분": {"type": "select", "select": {"name": "자료"}},
                    "확인": {"type": "select", "select": {"name": "해당없음"}},
                },
            },
        ]

        rows = [parse_competition_row(p) for p in raw_pages]
        res = compute_competition_analytics(rows, now_kst=self.now_kst)

        self.assertEqual(res.recent_24h_total, 2)
        recent_map = dict(res.recent_24h_by_owner)
        self.assertEqual(recent_map["뮤즈"], 2)
        self.assertEqual(recent_map["피클"], 0)
        self.assertEqual(recent_map["큐"], 0)
        self.assertEqual(recent_map["인스팅트"], 0)

        self.assertEqual(res.matrix_counts[("뮤즈", "꿀통")], 1)
        self.assertEqual(res.matrix_counts[("뮤즈", "점검")], 1)
        self.assertEqual(res.matrix_counts[("피클", "검증")], 1)

        verify_map = {v.owner: v for v in res.verify_stats}
        muse = verify_map["뮤즈"]
        self.assertEqual(muse.total_count, 3)
        self.assertEqual(muse.confirmed_count, 1)
        self.assertEqual(muse.unverified_count, 0)
        self.assertEqual(muse.empty_count, 1)
        self.assertEqual(muse.not_applicable_count, 1)
        self.assertEqual(muse.graded_sample_size, 2)
        self.assertEqual(muse.ratio_label(muse.confirmed_count), "1건 (50.0%)")
        self.assertEqual(muse.ratio_label(muse.empty_count), "1건 (50.0%)")

        # 섹션 G 정산 수동 확인 렌더링 확인
        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 4, 4),
            experience_analytics=None,
            competition_analytics=res,
        )
        bottom = build_bottom_blocks(report)
        flat_text = " ".join(
            b[b["type"]]["rich_text"][0]["text"]["content"]
            for b in bottom
            if b["type"] in {"heading_2", "callout", "paragraph"}
        )
        self.assertIn("정산: 수동 확인", flat_text)

    def test_rich_text_splits_over_2000_chars(self) -> None:
        long_text = "가" * 4500
        rt = make_rich_text(long_text)
        self.assertEqual(len(rt), 3)
        self.assertEqual(len(rt[0]["text"]["content"]), 2000)
        self.assertEqual(len(rt[1]["text"]["content"]), 2000)
        self.assertEqual(len(rt[2]["text"]["content"]), 500)

    def test_parse_experience_date_with_utc_timestamp_converts_to_kst(self) -> None:
        page = {
            "id": "p1",
            "created_time": "2026-11-19T16:00:00.000Z",
            "last_edited_time": "2026-11-19T16:00:00.000Z",
            "properties": {
                "캠페인명": {"type": "title", "title": [{"plain_text": "시간포함 마감일"}]},
                "리뷰마감일": {"type": "date", "date": {"start": "2026-11-19T16:00:00.000Z"}},
                "상태": {"type": "select", "select": {"name": "당첨"}},
            },
        }
        row = parse_experience_row(page)
        # 2026-11-19 16:00 UTC == 2026-11-20 01:00 KST
        self.assertEqual(row.review_due_date, date(2026, 11, 20))

    def test_self_purchase_review_classification_rule(self) -> None:
        cases = [
            ("쿠팡 리뷰: 생수 2L", "리뷰 점수 작업", True),
            ("쿠팡 리뷰:세제", "", True),
            ("  쿠팡 리뷰: 앞공백 포함", "일반 제공", True),
            ("일반 캠페인", "본인 구매 후 리뷰 작성", True),
            ("생활용품 체험", "쿠팡 본인 구매 1건", True),
            ("쿠팡 리뷰: 휴지", "본인 구매", True),
            ("쿠팡 체험단", "제품 무료 제공", False),
            ("쿠팡 리뷰 이벤트", "제품 무료 제공", False),
            ("백김치 체험단", "백김치 1kg 제공", False),
        ]
        for campaign_name, reward_text, expected in cases:
            row = ExperienceRow(
                "id", campaign_name, "쿠팡", "생활", reward_text, None, None, None, "리뷰완료", None, None
            )
            self.assertEqual(
                is_self_purchase_review(row),
                expected,
                msg=f"campaign_name={campaign_name!r}, reward_text={reward_text!r}",
            )

    def test_self_purchase_excluded_from_win_rate_and_section_d_with_separate_table(self) -> None:
        rows = [
            ExperienceRow("1", "백김치 체험", "레뷰", "식품", "백김치 1kg", None, None, date(2026, 11, 30), "당첨", None, None),
            ExperienceRow("2", "커피 체험", "레뷰", "식품", "아메리카노", None, None, None, "미당첨", None, None),
            ExperienceRow("3", "쿠팡 리뷰: 생수", "쿠팡", "생활", "점수 작업", None, None, None, "리뷰완료", None, None),
            ExperienceRow("4", "생활용품 리뷰", "쿠팡", "생활", "본인 구매 1개", None, None, None, "리뷰대기", None, None),
            ExperienceRow("5", "신청건", "레뷰", "식품", "간식", None, None, None, "신청완료", None, None),
        ]

        res = compute_experience_analytics(rows, now_kst=self.now_kst)

        # 전체 행 수·상태별 행 수는 원본 DB 기준 유지
        self.assertEqual(res.total_count, 5)
        self.assertEqual(sum(cnt for _, cnt in res.status_counts), 5)

        # 당첨률 계산(섹션 C)에서 본인 구매 리뷰 2건 제외 -> 레뷰 1/2 = 50.0% (n=2), 쿠팡은 제외되어 site_stats에 없음
        by_site = {s.site: s for s in res.site_stats}
        self.assertNotIn("쿠팡", by_site)
        self.assertEqual(by_site["레뷰"].applied_count, 3)
        self.assertEqual(by_site["레뷰"].won_count, 1)
        self.assertEqual(by_site["레뷰"].announced_count, 2)
        self.assertEqual(by_site["레뷰"].win_rate_label(), "50.0% (n=2)")
        self.assertEqual(res.total_site_stats.win_rate_label(), "50.0% (n=2)")

        # 섹션 D 현물 실적에서 본인 구매 리뷰 제외, 별도 목록으로 분리
        self.assertEqual([r.campaign_name for r in res.rewards], ["백김치 체험"])
        self.assertEqual(
            sorted(r.campaign_name for r in res.self_purchase_reviews),
            ["생활용품 리뷰", "쿠팡 리뷰: 생수"],
        )

        # 대시보드 렌더링 검증: '쿠팡 리뷰 점수 작업' 표와 분류 규칙 문구 포함
        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", len(rows), len(rows)),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", 0, 0),
            experience_analytics=res,
            competition_analytics=None,
        )
        top_blocks = build_top_blocks(report)
        block_texts = [extract_block_plain_text(b) for b in top_blocks]
        self.assertIn("쿠팡 리뷰 점수 작업", block_texts)
        self.assertIn(SELF_PURCHASE_RULE_TEXT, block_texts)

        # '쿠팡 리뷰 점수 작업' 제목 바로 뒤의 테이블에 본인 구매 리뷰 2건이 들어 있는지 확인
        sp_heading_idx = block_texts.index("쿠팡 리뷰 점수 작업")
        sp_table = next(b for b in top_blocks[sp_heading_idx + 1 :] if b.get("type") == "table")
        sp_names = [
            row["table_row"]["cells"][0][0]["text"]["content"]
            for row in sp_table["table"]["children"][1:]
        ]
        self.assertEqual(sorted(sp_names), ["생활용품 리뷰", "쿠팡 리뷰: 생수"])

    def test_section_f_empty_grade_aggregation_and_warning_callout(self) -> None:
        now_utc = self.now_kst.astimezone(timezone.utc)
        rows = [
            CompetitionRow("1", "항목1", None, "뮤즈", "꿀통", "확인됨", None, "", now_utc, now_utc),
            CompetitionRow("2", "항목2", None, "뮤즈", "꿀통", "원문대조필요", None, "", now_utc, now_utc),
            CompetitionRow("3", "항목3", None, "뮤즈", "꿀통", "미확인", None, "", now_utc, now_utc),
            CompetitionRow("4", "항목4", None, "뮤즈", "꿀통", None, None, "", now_utc, now_utc),
            CompetitionRow("5", "항목5", None, "뮤즈", "꿀통", "   ", None, "", now_utc, now_utc),
            CompetitionRow("6", "항목6", None, "뮤즈", "자료", "해당없음", None, "", now_utc, now_utc),
        ]

        res = compute_competition_analytics(rows, now_kst=self.now_kst)
        muse = {v.owner: v for v in res.verify_stats}["뮤즈"]
        self.assertEqual(muse.total_count, 6)
        self.assertEqual(muse.confirmed_count, 1)
        self.assertEqual(muse.needs_check_count, 1)
        self.assertEqual(muse.unverified_count, 1)  # 빈 값 2건이 합쳐지지 않음
        self.assertEqual(muse.empty_count, 2)
        self.assertEqual(muse.not_applicable_count, 1)
        self.assertEqual(muse.graded_sample_size, 5)
        self.assertEqual(muse.ratio_label(muse.unverified_count), "1건 (20.0%)")
        self.assertEqual(muse.ratio_label(muse.empty_count), "2건 (40.0%)")

        self.assertEqual(res.total_verify_stats.unverified_count, 1)
        self.assertEqual(res.total_verify_stats.empty_count, 2)

        report = DashboardReport(
            updated_at_kst=self.now_kst,
            run_status="정상",
            failure_reasons=[],
            warnings=[],
            experience_sync=SourceSyncResult("experience", "체험단 DB", True, "full", 0, 0),
            competition_sync=SourceSyncResult("competition", "경쟁 기록 DB", True, "full", len(rows), len(rows)),
            experience_analytics=None,
            competition_analytics=res,
        )
        bottom_blocks = build_bottom_blocks(report)
        block_texts = [extract_block_plain_text(b) for b in bottom_blocks]

        # 경고 문구가 확인 등급 분포 표 위에 배치되어 있는지 확인
        self.assertIn(VERIFY_GRADE_WARNING_TEXT, block_texts)
        warning_idx = block_texts.index(VERIFY_GRADE_WARNING_TEXT)
        verify_table_idx = next(
            i for i, b in enumerate(bottom_blocks) if i > warning_idx and b.get("type") == "table"
        )
        self.assertLess(warning_idx, verify_table_idx)

        headers = [
            cell[0]["text"]["content"]
            for cell in bottom_blocks[verify_table_idx]["table"]["children"][0]["table_row"]["cells"]
        ]
        self.assertEqual(
            headers,
            ["담당", "전체 (평가 n)", "확인됨", "원문대조필요", "미확인", "빈 값", "해당없음"],
        )


if __name__ == "__main__":
    unittest.main()
