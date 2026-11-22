"""Notion 블록 렌더러 및 CLI 텍스트 요약기.

공식 문서 크기 한도 준수 (https://developers.notion.com/reference/request-limits):
- Rich text text.content 최대 2,000자 -> 자동 분할
- Rich text 배열 최대 100개 요소
- 단일 Append Block Children 요청 최대 100개 블록 -> publisher에서 청크 전송
"""

from __future__ import annotations

from typing import Any, Optional

from . import (
    DEFAULT_SETTLEMENT_PAGE_TITLE,
    DEFAULT_SETTLEMENT_PAGE_URL,
    KST,
    MANUAL_E_END_MARKER,
    MANUAL_E_START_MARKER,
)
from .analytics import SELF_PURCHASE_RULE_TEXT, compute_next_scheduled_run_kst
from .models import CompetitionAnalytics, DashboardReport, ExperienceAnalytics

MAX_RICH_TEXT_CONTENT = 2000
MAX_RICH_TEXT_ELEMENTS = 100
VERIFY_GRADE_WARNING_TEXT = "자기 신고 등급 — 정확도 아님, 정확도는 P3 성적표에서"


def make_rich_text(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    color: str = "default",
    link_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Notion rich_text 배열을 생성한다 (2,000자 한도 자동 분할)."""
    link_obj = {"url": link_url} if link_url else None
    if text == "":
        text_payload: dict[str, Any] = {"content": ""}
        if link_obj is not None:
            text_payload["link"] = link_obj
        return [
            {
                "type": "text",
                "text": text_payload,
                "annotations": {
                    "bold": bold,
                    "italic": italic,
                    "strikethrough": False,
                    "underline": False,
                    "code": code,
                    "color": color,
                },
            }
        ]

    chunks = [
        text[i : i + MAX_RICH_TEXT_CONTENT]
        for i in range(0, len(text), MAX_RICH_TEXT_CONTENT)
    ]
    if len(chunks) > MAX_RICH_TEXT_ELEMENTS:
        chunks = chunks[: MAX_RICH_TEXT_ELEMENTS - 1] + [
            chunks[MAX_RICH_TEXT_ELEMENTS - 1][: MAX_RICH_TEXT_CONTENT - 20] + "... (길이 제한으로 생략)"
        ]

    result: list[dict[str, Any]] = []
    for chunk in chunks:
        text_payload = {"content": chunk}
        if link_obj is not None:
            text_payload["link"] = link_obj
        result.append(
            {
                "type": "text",
                "text": text_payload,
                "annotations": {
                    "bold": bold,
                    "italic": italic,
                    "strikethrough": False,
                    "underline": False,
                    "code": code,
                    "color": color,
                },
            }
        )
    return result


def heading_2(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": make_rich_text(text),
            "color": "default",
            "is_toggleable": False,
        },
    }


def heading_3(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": make_rich_text(text),
            "color": "default",
            "is_toggleable": False,
        },
    }


def paragraph(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str = "default",
) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": make_rich_text(text, bold=bold, italic=italic, color=color),
            "color": "default",
        },
    }


def bullet(
    text: str,
    *,
    bold: bool = False,
    color: str = "default",
) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": make_rich_text(text, bold=bold, color=color),
            "color": "default",
        },
    }


def callout(
    text: str,
    *,
    emoji: str = "📌",
    color: str = "gray_background",
    text_color: str = "default",
    bold: bool = False,
) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": make_rich_text(text, bold=bold, color=text_color),
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def divider() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def table_row(cells: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": cells},
    }


def table_block(
    headers: list[str],
    rows: list[list[list[dict[str, Any]]]],
    *,
    has_row_header: bool = False,
) -> dict[str, Any]:
    """Notion table 블록을 생성한다 (header 행 포함 최소 1행 보장)."""
    width = len(headers)
    header_row = table_row([make_rich_text(h, bold=True) for h in headers])
    body_rows = [table_row(_pad_or_trim_cells(r, width)) for r in rows]
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": has_row_header,
            "children": [header_row, *body_rows],
        },
    }


def _pad_or_trim_cells(
    cells: list[list[dict[str, Any]]],
    width: int,
) -> list[list[dict[str, Any]]]:
    if len(cells) < width:
        return cells + [make_rich_text("-") for _ in range(width - len(cells))]
    return cells[:width]


# ------------------------------------------------------------------
# 섹션별 블록 조립
# ------------------------------------------------------------------


def build_manual_e_start_marker_block() -> dict[str, Any]:
    return paragraph(
        f"{MANUAL_E_START_MARKER} 이 마커와 끝 마커 사이의 내용은 자동 갱신 시 덮어쓰지 않고 보존됩니다. (마커 줄 삭제 금지)",
        italic=True,
        color="gray",
    )


def build_manual_e_end_marker_block() -> dict[str, Any]:
    return paragraph(
        f"{MANUAL_E_END_MARKER} 수동 입력 보존 구간 끝 (이 줄 아래부터 다시 자동 갱신됩니다)",
        italic=True,
        color="gray",
    )


def build_manual_e_default_blocks() -> list[dict[str, Any]]:
    """최초 페이지 생성 시 넣는 섹션 E 기본 템플릿(보존 마커 포함)."""
    return [
        build_manual_e_start_marker_block(),
        bullet("티스토리: (수동 입력)"),
        bullet("애드센스: (수동 입력)"),
        bullet("쿠팡 파트너스: (수동 입력)"),
        build_manual_e_end_marker_block(),
    ]


def build_top_blocks(report: DashboardReport) -> list[dict[str, Any]]:
    """보존 구간(섹션 E) 위에 배치할 자동 생성 블록: 섹션 A, B, C, D + 섹션 E 제목."""
    blocks: list[dict[str, Any]] = []
    blocks.extend(_build_section_a(report))
    blocks.append(divider())
    blocks.extend(_build_section_b(report))
    blocks.append(divider())
    blocks.extend(_build_section_c(report))
    blocks.append(divider())
    blocks.extend(_build_section_d(report))
    blocks.append(divider())
    blocks.append(heading_2("E. 수동 입력 칸"))
    return blocks


def build_bottom_blocks(report: DashboardReport) -> list[dict[str, Any]]:
    """보존 구간(섹션 E) 아래에 배치할 자동 생성 블록: 섹션 F, G, H."""
    blocks: list[dict[str, Any]] = []
    blocks.append(divider())
    blocks.extend(_build_section_f(report))
    blocks.append(divider())
    blocks.extend(_build_section_g(report))
    blocks.append(divider())
    blocks.extend(_build_section_h(report))
    return blocks


def _build_section_a(report: DashboardReport) -> list[dict[str, Any]]:
    updated_str = report.updated_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    next_run_str = compute_next_scheduled_run_kst(report.updated_at_kst).strftime("%Y-%m-%d %H:%M KST")

    if report.run_status == "정상":
        emoji = "✅"
        bg_color = "green_background"
        text_color = "default"
    elif report.run_status == "부분 실패":
        emoji = "⚠️"
        bg_color = "yellow_background"
        text_color = "default"
    else:
        emoji = "🚨"
        bg_color = "red_background"
        text_color = "red"

    failure_summary = "; ".join(report.failure_reasons) if report.failure_reasons else "없음"
    header_lines = [
        f"갱신 시각: {updated_str}",
        f"다음 예정 갱신 시각: {next_run_str}",
        f"실행 결과: {report.run_status}",
        f"실패 원인: {failure_summary}",
        (
            f"동기화 요약: 체험단 DB({report.experience_sync.sync_mode}, "
            f"{'성공 ' + str(report.experience_sync.row_count) + '건' if report.experience_sync.ok else '실패'}) | "
            f"경쟁 기록 DB({report.competition_sync.sync_mode}, "
            f"{'성공 ' + str(report.competition_sync.row_count) + '건' if report.competition_sync.ok else '실패'})"
        ),
    ]

    blocks: list[dict[str, Any]] = [
        heading_2("A. 실행 상태 (머리줄)"),
        callout(
            "\n".join(header_lines),
            emoji=emoji,
            color=bg_color,
            text_color=text_color,
            bold=(report.run_status != "정상"),
        ),
    ]

    if report.warnings:
        blocks.append(
            callout(
                "경고 사항:\n" + "\n".join(f"- {w}" for w in report.warnings),
                emoji="⚠️",
                color="yellow_background",
            )
        )

    if report.fact_alerts:
        blocks.append(heading_3("팩트 경보"))
        alert_lines = [
            f"총 {len(report.fact_alerts)}건의 팩트 장부 경보가 감지되었습니다. (원문 재확인 또는 충돌 해소 필요)"
        ]
        blocks.append(
            callout(
                "\n".join(alert_lines),
                emoji="🚨",
                color="red_background",
                text_color="red",
                bold=True,
            )
        )
        alert_rows: list[list[list[dict[str, Any]]]] = []
        for alert in report.fact_alerts:
            if alert.old_grade and alert.new_grade:
                grade_change = (
                    f"{alert.new_grade} (유지)"
                    if alert.old_grade == alert.new_grade
                    else f"{alert.old_grade} → {alert.new_grade}"
                )
            else:
                grade_change = alert.new_grade or "-"
            url_cell = (
                make_rich_text(alert.url, link_url=alert.url)
                if alert.url
                else make_rich_text("-")
            )
            alert_rows.append(
                [
                    make_rich_text(alert.claim or "(주장 미입력)", bold=True, color="red"),
                    make_rich_text(alert.topic_key or "-"),
                    make_rich_text(alert.reason, color="red"),
                    make_rich_text(grade_change),
                    url_cell,
                ]
            )
        blocks.append(
            table_block(
                ["주장", "주제키", "경보 사유", "등급 변경", "근거 URL"],
                alert_rows,
            )
        )
    return blocks


def _build_section_b(report: DashboardReport) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading_2("B. 마감 임박")]
    exp = report.experience_analytics
    if exp is None:
        blocks.append(
            callout(
                f"체험단 DB 조회 실패로 마감 임박 데이터를 표시하지 않습니다. (이전 숫자 미표시)\n"
                f"원인: {report.experience_sync.error_message or '알 수 없는 오류'}",
                emoji="🚨",
                color="red_background",
                text_color="red",
            )
        )
        return blocks

    blocks.append(
        paragraph(
            "대상: 상태가 '당첨'·'리뷰대기'인 행 (본인 구매 리뷰 제외) | 정렬: D-day 오름차순 | D-3 이하 빨간색 강조",
            color="gray",
        )
    )

    if exp.deadlines:
        rows_cells: list[list[list[dict[str, Any]]]] = []
        for item in exp.deadlines:
            color = "red" if item.is_urgent else "default"
            rows_cells.append(
                [
                    make_rich_text(item.d_day_label, bold=item.is_urgent, color=color),
                    make_rich_text(item.review_due_date.isoformat(), color=color),
                    make_rich_text(item.campaign_name, bold=item.is_urgent, color=color),
                    make_rich_text(item.site, color=color),
                    make_rich_text(item.status, color=color),
                    make_rich_text(item.reward_text or "(미입력)", color=color),
                ]
            )
        blocks.append(
            table_block(
                ["D-day", "리뷰마감일", "캠페인명", "사이트", "상태", "제공내역"],
                rows_cells,
            )
        )
    else:
        blocks.append(paragraph("리뷰마감일이 입력된 당첨·리뷰대기 행이 없습니다."))

    blocks.append(heading_3("마감일 미입력"))
    if exp.missing_deadlines:
        missing_cells: list[list[list[dict[str, Any]]]] = []
        for item in exp.missing_deadlines:
            announced_str = item.announced_date.isoformat() if item.announced_date else "-"
            missing_cells.append(
                [
                    make_rich_text(item.campaign_name),
                    make_rich_text(item.site),
                    make_rich_text(item.status),
                    make_rich_text(announced_str),
                    make_rich_text(item.reward_text or "(미입력)"),
                ]
            )
        blocks.append(
            table_block(
                ["캠페인명", "사이트", "상태", "선정발표일", "제공내역"],
                missing_cells,
            )
        )
    else:
        blocks.append(paragraph("마감일이 비어 있는 당첨·리뷰대기 행이 없습니다."))

    return blocks


def _build_section_c(report: DashboardReport) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading_2("C. 체험단 현황")]
    exp = report.experience_analytics
    if exp is None:
        blocks.append(
            callout(
                f"체험단 DB 조회 실패로 체험단 현황을 표시하지 않습니다. (이전 숫자 미표시)\n"
                f"원인: {report.experience_sync.error_message or '알 수 없는 오류'}",
                emoji="🚨",
                color="red_background",
                text_color="red",
            )
        )
        return blocks

    blocks.append(paragraph(f"전체 행 수: {exp.total_count}건", bold=True))

    status_rows = [
        [make_rich_text(status), make_rich_text(f"{count}건")]
        for status, count in exp.status_counts
    ]
    status_rows.append([make_rich_text("전체 합계", bold=True), make_rich_text(f"{exp.total_count}건", bold=True)])
    blocks.append(heading_3("상태별 행 수"))
    blocks.append(table_block(["상태", "행 수"], status_rows))

    excluded_count = len(exp.self_purchase_reviews)
    blocks.append(heading_3("사이트별 신청·당첨 수 및 당첨률"))
    blocks.append(
        paragraph(
            "당첨률 = (당첨 + 리뷰대기 + 리뷰완료) ÷ 발표가 끝난 행(당첨 + 리뷰대기 + 리뷰완료 + 미당첨). "
            f"괄호 안 n=○은 발표가 끝난 표본 수입니다. "
            f"'본인 구매 리뷰' {excluded_count}건은 당첨률 계산에서 제외합니다.",
            color="gray",
        )
    )

    site_rows: list[list[list[dict[str, Any]]]] = []
    for s in exp.site_stats:
        site_rows.append(
            [
                make_rich_text(s.site),
                make_rich_text(str(s.applied_count)),
                make_rich_text(str(s.won_count)),
                make_rich_text(str(s.lost_count)),
                make_rich_text(str(s.pending_count)),
                make_rich_text(s.win_rate_label()),
            ]
        )
    t = exp.total_site_stats
    total_site_label = f"{t.site} (본인 구매 리뷰 {excluded_count}건 제외)"
    site_rows.append(
        [
            make_rich_text(total_site_label, bold=True),
            make_rich_text(str(t.applied_count), bold=True),
            make_rich_text(str(t.won_count), bold=True),
            make_rich_text(str(t.lost_count), bold=True),
            make_rich_text(str(t.pending_count), bold=True),
            make_rich_text(t.win_rate_label(), bold=True),
        ]
    )
    blocks.append(
        table_block(
            ["사이트", "신청", "당첨", "미당첨", "발표대기/신청완료", "당첨률 (표본 수)"],
            site_rows,
            has_row_header=True,
        )
    )
    return blocks


def _build_section_d(report: DashboardReport) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading_2("D. 현물 실적")]
    exp = report.experience_analytics
    if exp is None:
        blocks.append(
            callout(
                f"체험단 DB 조회 실패로 현물 실적을 표시하지 않습니다. (이전 숫자 미표시)\n"
                f"원인: {report.experience_sync.error_message or '알 수 없는 오류'}",
                emoji="🚨",
                color="red_background",
                text_color="red",
            )
        )
        return blocks

    blocks.append(
        paragraph(
            f"대상: 상태가 '당첨'·'리뷰대기'·'리뷰완료'인 행 중 본인 구매 리뷰 제외 (총 {len(exp.rewards)}건) | "
            "제공내역은 DB 원문 그대로 표기하며 금액을 추정하거나 합산하지 않습니다.",
            color="gray",
        )
    )

    if exp.rewards:
        reward_rows = [
            [
                make_rich_text(item.campaign_name),
                make_rich_text(item.site),
                make_rich_text(item.status),
                make_rich_text(item.reward_text or "(미입력)"),
            ]
            for item in exp.rewards
        ]
        blocks.append(table_block(["캠페인명", "사이트", "상태", "제공내역 (원문)"], reward_rows))
    else:
        blocks.append(paragraph("당첨·리뷰대기·리뷰완료 행이 없습니다."))

    blocks.append(heading_3("쿠팡 리뷰 점수 작업"))
    blocks.append(paragraph(SELF_PURCHASE_RULE_TEXT, color="gray"))
    if exp.self_purchase_reviews:
        self_purchase_rows = [
            [
                make_rich_text(item.campaign_name),
                make_rich_text(item.site),
                make_rich_text(item.status),
                make_rich_text(item.reward_text or "(미입력)"),
            ]
            for item in exp.self_purchase_reviews
        ]
        blocks.append(
            table_block(["캠페인명", "사이트", "상태", "제공내역 (원문)"], self_purchase_rows)
        )
    else:
        blocks.append(paragraph("본인 구매 리뷰로 분류된 행이 없습니다."))
    return blocks


def _build_section_f(report: DashboardReport) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading_2("F. 경쟁 기록 요약")]
    comp = report.competition_analytics
    if comp is None:
        blocks.append(
            callout(
                f"경쟁 기록 DB 조회 실패로 경쟁 기록 요약을 표시하지 않습니다. (이전 숫자 미표시)\n"
                f"원인: {report.competition_sync.error_message or '알 수 없는 오류'}",
                emoji="🚨",
                color="red_background",
                text_color="red",
            )
        )
        return blocks

    # 1) 최근 24시간 새 행 수 (담당별)
    blocks.append(heading_3("최근 24시간 새 행 수 (담당별)"))
    blocks.append(
        paragraph(
            f"기준: 페이지 created_time이 갱신 시각 기준 최근 24시간 이내인 행 (합계 {comp.recent_24h_total}건)",
            color="gray",
        )
    )
    recent_rows = [
        [make_rich_text(owner), make_rich_text(f"{cnt}건")]
        for owner, cnt in comp.recent_24h_by_owner
    ]
    recent_rows.append(
        [make_rich_text("합계", bold=True), make_rich_text(f"{comp.recent_24h_total}건", bold=True)]
    )
    blocks.append(table_block(["담당", "최근 24시간 새 행"], recent_rows, has_row_header=True))

    # 2) 담당 × 구분 표
    blocks.append(heading_3("담당 × 구분 표"))
    matrix_headers = ["담당", *comp.matrix_categories, "합계"]
    matrix_rows: list[list[list[dict[str, Any]]]] = []
    col_totals = [0 for _ in comp.matrix_categories]
    grand_total = 0

    for owner in comp.matrix_owners:
        row_cells = [make_rich_text(owner)]
        row_sum = 0
        for idx, cat in enumerate(comp.matrix_categories):
            cnt = comp.matrix_counts.get((owner, cat), 0)
            row_cells.append(make_rich_text(str(cnt)))
            row_sum += cnt
            col_totals[idx] += cnt
        grand_total += row_sum
        row_cells.append(make_rich_text(str(row_sum), bold=True))
        matrix_rows.append(row_cells)

    total_row = (
        [make_rich_text("합계", bold=True)]
        + [make_rich_text(str(v), bold=True) for v in col_totals]
        + [make_rich_text(str(grand_total), bold=True)]
    )
    matrix_rows.append(total_row)
    blocks.append(table_block(matrix_headers, matrix_rows, has_row_header=True))

    # 3) 확인 등급 분포
    blocks.append(heading_3("확인 등급 분포 (담당별)"))
    blocks.append(
        callout(
            VERIFY_GRADE_WARNING_TEXT,
            emoji="⚠️",
            color="yellow_background",
            bold=True,
        )
    )
    blocks.append(
        paragraph(
            "비율 분모 n = 확인됨 + 원문대조필요 + 미확인 + 빈 값 (해당없음 제외). "
            "앱은 등급을 '확인됨'으로 상향하지 않으며, 확인 등급이 비어 있는 행은 '미확인'에 합치지 않고 '빈 값' 칸에 별도 집계합니다.",
            color="gray",
        )
    )
    verify_rows: list[list[list[dict[str, Any]]]] = []
    for stat in comp.verify_stats:
        verify_rows.append(
            [
                make_rich_text(stat.owner),
                make_rich_text(f"{stat.total_count}건 (n={stat.graded_sample_size})"),
                make_rich_text(stat.ratio_label(stat.confirmed_count)),
                make_rich_text(stat.ratio_label(stat.needs_check_count)),
                make_rich_text(stat.ratio_label(stat.unverified_count)),
                make_rich_text(stat.ratio_label(stat.empty_count)),
                make_rich_text(f"{stat.not_applicable_count}건"),
            ]
        )
    tv = comp.total_verify_stats
    verify_rows.append(
        [
            make_rich_text(tv.owner, bold=True),
            make_rich_text(f"{tv.total_count}건 (n={tv.graded_sample_size})", bold=True),
            make_rich_text(tv.ratio_label(tv.confirmed_count), bold=True),
            make_rich_text(tv.ratio_label(tv.needs_check_count), bold=True),
            make_rich_text(tv.ratio_label(tv.unverified_count), bold=True),
            make_rich_text(tv.ratio_label(tv.empty_count), bold=True),
            make_rich_text(f"{tv.not_applicable_count}건", bold=True),
        ]
    )
    blocks.append(
        table_block(
            ["담당", "전체 (평가 n)", "확인됨", "원문대조필요", "미확인", "빈 값", "해당없음"],
            verify_rows,
            has_row_header=True,
        )
    )
    return blocks


def _settlement_link_paragraph(title: str, url: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": (
                make_rich_text("정산판: ", bold=True)
                + make_rich_text(title, bold=True, link_url=url)
                + make_rich_text(f" ({url})", color="gray")
            ),
            "color": "default",
        },
    }


def _build_section_g(report: DashboardReport) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [heading_2("G. 정산")]
    s = report.settlement_summary

    if s is None:
        blocks.append(_settlement_link_paragraph(DEFAULT_SETTLEMENT_PAGE_TITLE, DEFAULT_SETTLEMENT_PAGE_URL))
        blocks.append(
            callout(
                "정산: 수동 확인\n(정산판 구조 미확인 — 정산판 링크에서 직접 확인하세요.)",
                emoji="🧾",
                color="gray_background",
            )
        )
        return blocks

    blocks.append(_settlement_link_paragraph(s.title, s.url))

    if s.structured:
        summary_text = "\n".join(s.summary_lines) if s.summary_lines else "정산판 구조화 데이터를 원문 그대로 요약했습니다."
        blocks.append(
            callout(
                f"정산판 핵심 요약 (원문 기준, 추측·임의 합산 없음)\n{summary_text}",
                emoji="🧾",
                color="blue_background",
            )
        )
        if s.table_headers:
            if s.table_rows:
                rendered_rows = [
                    [make_rich_text(cell) for cell in row]
                    for row in s.table_rows
                ]
                blocks.append(table_block(s.table_headers, rendered_rows))
            else:
                blocks.append(paragraph("정산판 구조화 데이터에 표시할 행이 없습니다.", color="gray"))
        return blocks

    # 비구조화 페이지, 접근 불가, 또는 미설정: 링크 유지 + '수동 확인' 유지
    note_lines = ["정산: 수동 확인"]
    if s.error_message:
        note_lines.append(f"정산판 조회 실패: {s.error_message}")
    elif s.summary_lines:
        note_lines.extend(s.summary_lines)
    else:
        note_lines.append("구조화된 DB/표가 확인되지 않아 수동 확인을 유지합니다.")
    blocks.append(
        callout(
            "\n".join(note_lines),
            emoji="🧾",
            color="gray_background",
        )
    )
    return blocks


def _build_section_h(report: DashboardReport) -> list[dict[str, Any]]:
    """섹션 H: 에이전트 성적표 (📒 팩트 장부 기준).

    표기 규칙:
    - 오직 📒 팩트 장부 데이터만 사용하며 경쟁 기록 DB로 대체하지 않는다.
    - 숫자(및 '표본 부족', '집계 불가')만 표시하고 순위·서열·우열·평가 문구를 넣지 않는다.
    - 에이전트 행은 고정 순서(뮤즈·피클·큐·인스팅트·클실장)를 유지하며 수치 정렬하지 않는다.
    """
    blocks: list[dict[str, Any]] = [heading_2("H. 에이전트 성적표 (팩트 장부 기준)")]
    sc = report.agent_scorecard

    if sc is None:
        blocks.append(
            callout(
                "📒 팩트 장부 데이터가 연결되지 않아 에이전트 성적표를 표시하지 않습니다. "
                "(집계 불가 — 경쟁 기록 DB로 대체하지 않음)",
                emoji="📒",
                color="gray_background",
            )
        )
        return blocks

    if not sc.ok:
        err = sc.error_message or "알 수 없는 오류"
        blocks.append(
            callout(
                "📒 팩트 장부 조회 실패로 에이전트 성적표를 표시하지 않습니다. "
                "(집계 불가 — 이전 숫자 및 경쟁 기록 DB 미사용)\n"
                f"원인: {err}",
                emoji="🚨",
                color="red_background",
                text_color="red",
            )
        )
        return blocks

    caption_lines = [
        f"데이터 출처: 📒 팩트 장부 (전체 {sc.total_ledger_rows}건) | "
        f"대상 에이전트: {', '.join(r.agent for r in sc.agent_rows)}",
        "유지율 = 등급 '확인됨' 행 ÷ (유지 중 + 내려간 행) | 표본 부족: 유지율 분모(유지 중 + 내려간 행) 5 미만",
        "내려간 행 기준(현재 필드): 등급 '불일치', (등급 '원문대조필요' 및 자동 재검증 결과 '발췌 사라짐'), 또는 '대체됨' 연결 있음 ('자동확인불가' 제외)",
        "잡아낸 수: 타 에이전트 또는 '회장님'이 올린 행을 정정한 수 ('회장님' 행은 괄호로 분리 표기)",
        "자동 경보 수 기준(현재 필드): (등급 '불일치' 및 충돌 연결 있음) 또는 (등급 '원문대조필요' 및 자동 재검증 결과 '발췌 사라짐')",
    ]
    if sc.missing_fields:
        caption_lines.append(
            f"스키마 누락 필드({', '.join(sc.missing_fields)}): 해당 지표는 '집계 불가'로 표시"
        )
    if sc.is_incomplete:
        caption_lines.append("참고: 팩트 장부 쿼리 10,000건 상한(request_status=incomplete) 도달")

    blocks.append(paragraph("\n".join(caption_lines), color="gray"))

    score_rows: list[list[list[dict[str, Any]]]] = []
    for row in sc.agent_rows:
        score_rows.append(
            [
                make_rich_text(row.agent),
                make_rich_text(row.count_label(row.uploaded_count)),
                make_rich_text(row.retention_label()),
                make_rich_text(row.count_label(row.corrected_against_count)),
                make_rich_text(row.caught_label()),
                make_rich_text(row.count_label(row.auto_alert_count)),
            ]
        )

    blocks.append(
        table_block(
            ["에이전트", "올린 팩트 수", "유지율", "정정당한 수", "잡아낸 수", "자동 경보 수"],
            score_rows,
            has_row_header=True,
        )
    )
    return blocks


def format_cli_summary(report: DashboardReport) -> str:
    """실행 결과 콘솔 요약 문자열을 생성한다 (--dry-run 및 실행 로그용)."""
    updated_str = report.updated_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    next_run_str = compute_next_scheduled_run_kst(report.updated_at_kst).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"[꿀통 대시보드] 갱신 시각: {updated_str}",
        f"다음 예정 갱신 시각: {next_run_str}",
        f"실행 결과: {report.run_status}",
        f"실패 원인: {'; '.join(report.failure_reasons) if report.failure_reasons else '없음'}",
    ]
    if report.warnings:
        lines.append("경고:")
        for w in report.warnings:
            lines.append(f"  - {w}")
    if report.fact_alerts:
        lines.append(f"팩트 경보: {len(report.fact_alerts)}건")
        for alert in report.fact_alerts:
            lines.append(f"  - [{alert.topic_key}] {alert.claim}: {alert.reason}")
    if report.experience_analytics is not None:
        exp: ExperienceAnalytics = report.experience_analytics
        urgent = sum(1 for d in exp.deadlines if d.is_urgent)
        lines.append(
            f"체험단 DB: 전체 {exp.total_count}건 | 마감임박 {len(exp.deadlines)}건(D-3 이하 {urgent}건) | "
            f"마감일 미입력 {len(exp.missing_deadlines)}건 | 현물실적 {len(exp.rewards)}건 | "
            f"본인구매리뷰 {len(exp.self_purchase_reviews)}건 | "
            f"전체 당첨률 {exp.total_site_stats.win_rate_label()}"
        )
    else:
        lines.append("체험단 DB: 조회 실패 (이전 숫자 미표시)")

    if report.competition_analytics is not None:
        comp: CompetitionAnalytics = report.competition_analytics
        lines.append(
            f"경쟁 기록 DB: 전체 {comp.total_count}건 | 최근 24시간 {comp.recent_24h_total}건"
        )
    else:
        lines.append("경쟁 기록 DB: 조회 실패 (이전 숫자 미표시)")

    if report.settlement_summary and report.settlement_summary.structured:
        lines.append(
            f"정산: 구조화 요약 연결 ({report.settlement_summary.structure_type}, {report.settlement_summary.url})"
        )
    else:
        url = report.settlement_summary.url if report.settlement_summary else DEFAULT_SETTLEMENT_PAGE_URL
        lines.append(f"정산: 수동 확인 ({url})")

    if report.agent_scorecard is not None:
        sc = report.agent_scorecard
        if sc.ok:
            agent_parts = ", ".join(
                f"{r.agent}({r.count_label(r.uploaded_count)}건/{r.retention_label()})"
                for r in sc.agent_rows
            )
            lines.append(f"에이전트 성적표(팩트 장부 {sc.total_ledger_rows}건): {agent_parts}")
        else:
            lines.append(
                f"에이전트 성적표(팩트 장부): 집계 불가 ({sc.error_message or '조회 실패'})"
            )
    return "\n".join(lines)
