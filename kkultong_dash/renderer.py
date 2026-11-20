"""Notion 블록 렌더러 및 CLI 텍스트 요약기.

공식 문서 크기 한도 준수 (https://developers.notion.com/reference/request-limits):
- Rich text text.content 최대 2,000자 -> 자동 분할
- Rich text 배열 최대 100개 요소
- 단일 Append Block Children 요청 최대 100개 블록 -> publisher에서 청크 전송
"""

from __future__ import annotations

from typing import Any

from . import KST, MANUAL_E_END_MARKER, MANUAL_E_START_MARKER
from .models import CompetitionAnalytics, DashboardReport, ExperienceAnalytics

MAX_RICH_TEXT_CONTENT = 2000
MAX_RICH_TEXT_ELEMENTS = 100


def make_rich_text(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    color: str = "default",
) -> list[dict[str, Any]]:
    """Notion rich_text 배열을 생성한다 (2,000자 한도 자동 분할)."""
    if text == "":
        return [
            {
                "type": "text",
                "text": {"content": ""},
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

    return [
        {
            "type": "text",
            "text": {"content": chunk},
            "annotations": {
                "bold": bold,
                "italic": italic,
                "strikethrough": False,
                "underline": False,
                "code": code,
                "color": color,
            },
        }
        for chunk in chunks
    ]


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
    """보존 구간(섹션 E) 아래에 배치할 자동 생성 블록: 섹션 F, G."""
    blocks: list[dict[str, Any]] = []
    blocks.append(divider())
    blocks.extend(_build_section_f(report))
    blocks.append(divider())
    blocks.extend(_build_section_g())
    return blocks


def _build_section_a(report: DashboardReport) -> list[dict[str, Any]]:
    updated_str = report.updated_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")

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
            "대상: 상태가 '당첨'·'리뷰대기'인 행 | 정렬: D-day 오름차순 | D-3 이하 빨간색 강조",
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

    blocks.append(heading_3("사이트별 신청·당첨 수 및 당첨률"))
    blocks.append(
        paragraph(
            "당첨률 = (당첨 + 리뷰대기 + 리뷰완료) ÷ 발표가 끝난 행(당첨 + 리뷰대기 + 리뷰완료 + 미당첨). "
            "괄호 안 n=○은 발표가 끝난 표본 수입니다.",
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
    site_rows.append(
        [
            make_rich_text(t.site, bold=True),
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
            f"대상: 상태가 '당첨'·'리뷰대기'·'리뷰완료'인 행 (총 {len(exp.rewards)}건) | "
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
        paragraph(
            "비율 분모 n = 확인됨 + 원문대조필요 + 미확인 (해당없음 제외). "
            "앱은 등급을 '확인됨'으로 상향하지 않으며 빈 값/미분류는 '미확인'으로 표시합니다.",
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
            make_rich_text(f"{tv.not_applicable_count}건", bold=True),
        ]
    )
    blocks.append(
        table_block(
            ["담당", "전체 (평가 n)", "확인됨", "원문대조필요", "미확인", "해당없음"],
            verify_rows,
            has_row_header=True,
        )
    )
    return blocks


def _build_section_g() -> list[dict[str, Any]]:
    return [
        heading_2("G. 정산"),
        callout(
            "정산: 수동 확인\n(정산판 위치 미확인 — P1에서는 자동 집계하지 않고 수동 확인으로 표시합니다.)",
            emoji="🧾",
            color="gray_background",
        ),
    ]


def format_cli_summary(report: DashboardReport) -> str:
    """실행 결과 콘솔 요약 문자열을 생성한다 (--dry-run 및 실행 로그용)."""
    updated_str = report.updated_at_kst.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    lines = [
        f"[꿀통 대시보드] 갱신 시각: {updated_str}",
        f"실행 결과: {report.run_status}",
        f"실패 원인: {'; '.join(report.failure_reasons) if report.failure_reasons else '없음'}",
    ]
    if report.warnings:
        lines.append("경고:")
        for w in report.warnings:
            lines.append(f"  - {w}")
    if report.experience_analytics is not None:
        exp: ExperienceAnalytics = report.experience_analytics
        urgent = sum(1 for d in exp.deadlines if d.is_urgent)
        lines.append(
            f"체험단 DB: 전체 {exp.total_count}건 | 마감임박 {len(exp.deadlines)}건(D-3 이하 {urgent}건) | "
            f"마감일 미입력 {len(exp.missing_deadlines)}건 | 현물실적 {len(exp.rewards)}건 | "
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

    lines.append("정산: 수동 확인")
    return "\n".join(lines)
