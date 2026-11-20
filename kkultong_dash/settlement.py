"""섹션 G 정산판 읽기 전용 점검 및 구조 판별기.

대상 페이지(기본값, 피클 06:22 답변 기준):
- 제목: '📊 일별·월별 수익 통계 & 실행 체크리스트'
- URL: https://app.notion.com/p/41a89944f230817a9849d34d51978411
- Page ID: 41a89944-f230-817a-9849-d34d51978411

원칙:
- 정산판은 절대 수정하지 않는다(읽기 전용 보호 목록에 등록).
- 구조화된 데이터(child_database 또는 table 블록)가 있을 때만 실제 행/셀 원문을 그대로 요약한다.
- 숫자를 추측하거나 임의로 합산·생성하지 않는다.
- 구조화 데이터가 없거나 페이지 접근에 실패하면 링크와 '수동 확인' 안내를 유지한다.
"""

from __future__ import annotations

from typing import Any, Optional

from . import DEFAULT_SETTLEMENT_PAGE_TITLE, DEFAULT_SETTLEMENT_PAGE_URL
from .models import SettlementSummary
from .notion_client import NotionAPIError, NotionClient
from .parsers import (
    extract_block_plain_text,
    extract_database_title,
    extract_page_title,
    extract_rich_text_plain,
)

MAX_SUMMARY_COLUMNS = 6
MAX_SUMMARY_ROWS = 10
CONTAINER_BLOCK_TYPES = frozenset(
    {"column_list", "column", "toggle", "callout", "synced_block", "quote"}
)


def _build_page_url(page_id: Optional[str], api_url: Optional[str] = None) -> str:
    if api_url and api_url.strip():
        return api_url.strip()
    if page_id and page_id.strip():
        hex32 = page_id.replace("-", "").strip().lower()
        if len(hex32) == 32:
            return f"https://app.notion.com/p/{hex32}"
    return DEFAULT_SETTLEMENT_PAGE_URL


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


def _format_property_value_verbatim(prop: dict[str, Any]) -> str:
    """Notion DB 행 속성을 추측/계산 없이 원문 문자열 그대로 포맷한다."""
    if not isinstance(prop, dict):
        return ""
    ptype = prop.get("type")
    if ptype == "title":
        return extract_rich_text_plain(prop.get("title")).strip()
    if ptype == "rich_text":
        return extract_rich_text_plain(prop.get("rich_text")).strip()
    if ptype == "number":
        val = prop.get("number")
        return "" if val is None else str(val)
    if ptype == "select":
        sel = prop.get("select")
        return str(sel.get("name") or "").strip() if isinstance(sel, dict) else ""
    if ptype == "status":
        st = prop.get("status")
        return str(st.get("name") or "").strip() if isinstance(st, dict) else ""
    if ptype == "multi_select":
        ms = prop.get("multi_select")
        if isinstance(ms, list):
            return ", ".join(str(x.get("name") or "").strip() for x in ms if isinstance(x, dict) and x.get("name"))
        return ""
    if ptype == "date":
        dt = prop.get("date")
        if isinstance(dt, dict):
            start = str(dt.get("start") or "").strip()
            end = str(dt.get("end") or "").strip()
            return f"{start} ~ {end}" if (start and end) else start
        return ""
    if ptype == "checkbox":
        return "✓" if prop.get("checkbox") else "☐"
    if ptype == "url":
        return str(prop.get("url") or "").strip()
    if ptype == "formula":
        f = prop.get("formula")
        if isinstance(f, dict):
            ft = f.get("type")
            val = f.get(ft) if isinstance(ft, str) else None
            return "" if val is None else str(val)
        return ""
    if ptype == "rollup":
        r = prop.get("rollup")
        if isinstance(r, dict):
            rt = r.get("type")
            val = r.get(rt) if isinstance(rt, str) else None
            if isinstance(val, (str, int, float)):
                return str(val)
        return ""
    return ""


def _collect_structured_blocks(
    client: NotionClient,
    block_id: str,
    *,
    depth: int = 0,
    max_depth: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """페이지 하위 블록에서 child_database와 table 블록을 읽기 전용으로 탐색한다."""
    child_dbs: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    blocks = client.list_block_children(block_id)
    for blk in blocks:
        if _is_trashed(blk):
            continue
        btype = blk.get("type")
        if btype == "child_database":
            child_dbs.append(blk)
        elif btype == "table":
            tables.append(blk)
        elif (
            depth < max_depth
            and blk.get("has_children")
            and btype in CONTAINER_BLOCK_TYPES
            and blk.get("id")
        ):
            sub_dbs, sub_tables = _collect_structured_blocks(
                client,
                str(blk["id"]),
                depth=depth + 1,
                max_depth=max_depth,
            )
            child_dbs.extend(sub_dbs)
            tables.extend(sub_tables)
    return child_dbs, tables


def _summarize_child_database(
    client: NotionClient,
    db_block: dict[str, Any],
    *,
    page_id: str,
    page_title: str,
    page_url: str,
    total_db_count: int,
) -> Optional[SettlementSummary]:
    """정산판 내부 child_database 1개를 읽어 실제 행/열 원문을 요약한다."""
    db_id = str(db_block.get("id") or "")
    if not db_id:
        return None
    client.register_protected_id(db_id)

    db_meta = client.retrieve_database(db_id)
    db_title = extract_database_title(db_meta) or extract_block_plain_text(db_block) or "(제목 없는 DB)"

    data_sources = db_meta.get("data_sources")
    ds_id: Optional[str] = None
    if isinstance(data_sources, list) and data_sources:
        first_ds = data_sources[0]
        if isinstance(first_ds, dict) and first_ds.get("id"):
            ds_id = str(first_ds["id"])
    if not ds_id:
        return None

    client.register_protected_id(ds_id)
    pages, is_incomplete = client.query_data_source(ds_id)
    active_pages = [p for p in pages if not _is_trashed(p)]

    # 열 순서 결정: title 속성을 맨 앞에 두고 나머지 주요 속성을 최대 MAX_SUMMARY_COLUMNS개까지 표시
    headers: list[str] = []
    if active_pages:
        first_props = active_pages[0].get("properties")
        if isinstance(first_props, dict):
            title_keys = [k for k, v in first_props.items() if isinstance(v, dict) and v.get("type") == "title"]
            other_keys = [k for k in first_props.keys() if k not in title_keys]
            headers = (title_keys + other_keys)[:MAX_SUMMARY_COLUMNS]

    rows: list[list[str]] = []
    for p in active_pages[:MAX_SUMMARY_ROWS]:
        props = p.get("properties") if isinstance(p.get("properties"), dict) else {}
        rows.append([_format_property_value_verbatim(props.get(col, {})) or "-" for col in headers])

    summary_lines = [
        f"감지된 구조: 하위 데이터베이스 '{db_title}' (전체 {total_db_count}개 DB 중 대표 DB, 활성 행 {len(active_pages)}건)",
    ]
    if is_incomplete:
        summary_lines.append("주의: DB 쿼리 상한(10,000건)에 도달해 일부 행이 생략되었을 수 있습니다.")
    if len(active_pages) > MAX_SUMMARY_ROWS:
        summary_lines.append(f"표시 범위: 상위 {MAX_SUMMARY_ROWS}건 원문 표시 (전체 {len(active_pages)}건 중)")
    elif len(active_pages) == 0:
        summary_lines.append("현재 DB에 등록된 활성 행이 없습니다.")

    return SettlementSummary(
        page_id=page_id,
        title=page_title,
        url=page_url,
        accessible=True,
        structured=True,
        structure_type="database",
        summary_lines=summary_lines,
        table_headers=headers,
        table_rows=rows,
    )


def _summarize_table_block(
    client: NotionClient,
    table_block: dict[str, Any],
    *,
    page_id: str,
    page_title: str,
    page_url: str,
    total_table_count: int,
) -> Optional[SettlementSummary]:
    """정산판 내부 Notion table 블록을 읽어 실제 행/셀 원문을 요약한다."""
    table_id = str(table_block.get("id") or "")
    if not table_id:
        return None
    table_meta = table_block.get("table") if isinstance(table_block.get("table"), dict) else {}
    has_col_header = bool(table_meta.get("has_column_header", True))

    row_blocks = [
        b
        for b in client.list_block_children(table_id)
        if not _is_trashed(b) and b.get("type") == "table_row"
    ]
    if not row_blocks:
        return None

    parsed_rows: list[list[str]] = []
    for rb in row_blocks:
        cells = (rb.get("table_row") or {}).get("cells")
        if isinstance(cells, list):
            parsed_rows.append([extract_rich_text_plain(c).strip() or "-" for c in cells])

    if not parsed_rows:
        return None

    col_count = min(max(len(r) for r in parsed_rows), MAX_SUMMARY_COLUMNS)
    if has_col_header and len(parsed_rows) >= 1:
        headers = [c for c in parsed_rows[0][:col_count]]
        data_rows = [r[:col_count] for r in parsed_rows[1:]]
    else:
        headers = [f"열 {i + 1}" for i in range(col_count)]
        data_rows = [r[:col_count] for r in parsed_rows]

    summary_lines = [
        f"감지된 구조: 노션 표(table) {total_table_count}개 (대표 표 데이터 {len(data_rows)}행 원문 요약)",
    ]
    if len(data_rows) > MAX_SUMMARY_ROWS:
        summary_lines.append(f"표시 범위: 상위 {MAX_SUMMARY_ROWS}행 원문 표시 (전체 {len(data_rows)}행 중)")

    return SettlementSummary(
        page_id=page_id,
        title=page_title,
        url=page_url,
        accessible=True,
        structured=True,
        structure_type="table",
        summary_lines=summary_lines,
        table_headers=headers,
        table_rows=data_rows[:MAX_SUMMARY_ROWS],
    )


def inspect_settlement_page(
    client: NotionClient,
    page_id: Optional[str],
) -> SettlementSummary:
    """정산판 페이지를 읽기 전용으로 점검하여 구조화 여부와 요약 데이터를 반환한다."""
    if not page_id or not page_id.strip():
        return SettlementSummary(
            page_id=None,
            title=DEFAULT_SETTLEMENT_PAGE_TITLE,
            url=DEFAULT_SETTLEMENT_PAGE_URL,
            accessible=False,
            structured=False,
            structure_type="unconfigured",
            summary_lines=["정산판 페이지 ID가 설정되지 않았습니다."],
        )

    cleaned_id = page_id.strip()
    client.register_protected_id(cleaned_id)
    fallback_url = _build_page_url(cleaned_id)

    try:
        page = client.retrieve_page(cleaned_id)
        if _is_trashed(page):
            return SettlementSummary(
                page_id=cleaned_id,
                title=DEFAULT_SETTLEMENT_PAGE_TITLE,
                url=fallback_url,
                accessible=False,
                structured=False,
                structure_type="error",
                error_message="정산판 페이지가 휴지통 상태입니다.",
            )

        page_title = extract_page_title(page) or DEFAULT_SETTLEMENT_PAGE_TITLE
        page_url = _build_page_url(cleaned_id, page.get("url") if isinstance(page.get("url"), str) else None)

        child_dbs, tables = _collect_structured_blocks(client, cleaned_id)

        if child_dbs:
            for db_blk in child_dbs:
                summary = _summarize_child_database(
                    client,
                    db_blk,
                    page_id=cleaned_id,
                    page_title=page_title,
                    page_url=page_url,
                    total_db_count=len(child_dbs),
                )
                if summary is not None:
                    return summary

        if tables:
            for tbl_blk in tables:
                summary = _summarize_table_block(
                    client,
                    tbl_blk,
                    page_id=cleaned_id,
                    page_title=page_title,
                    page_url=page_url,
                    total_table_count=len(tables),
                )
                if summary is not None:
                    return summary

        return SettlementSummary(
            page_id=cleaned_id,
            title=page_title,
            url=page_url,
            accessible=True,
            structured=False,
            structure_type="unstructured",
            summary_lines=[
                "페이지에 구조화된 데이터베이스(DB) 또는 표(table) 블록이 없어 링크만 연결하고 수동 확인으로 유지합니다."
            ],
        )
    except NotionAPIError as exc:
        return SettlementSummary(
            page_id=cleaned_id,
            title=DEFAULT_SETTLEMENT_PAGE_TITLE,
            url=fallback_url,
            accessible=False,
            structured=False,
            structure_type="error",
            error_message=str(exc),
        )
