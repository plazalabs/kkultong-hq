"""대시보드 페이지 식별 및 섹션 E 인플레이스 보존 발행기.

설계 핵심:
1. 대시보드 페이지 식별 (4단계 우선순위):
   (1) 환경변수 NOTION_DASHBOARD_PAGE_ID (존재 + 미삭제 + 제목 일치 안전 검증)
   (2) SQLite app_state.dashboard_page_id (존재 + 미삭제 + 부모/제목 일치 검증)
   (3) 부모 페이지('천하제일 꿀통대회')의 1단계 하위 child_page 블록 직접 스캔 (강한 일관성, 검색 지연 회피)
   (4) 위 3단계에서 찾지 못할 때만 부모 페이지 아래에 최초 1회 생성 후 ID를 SQLite에 저장

2. 섹션 E(수동 입력 칸) 인플레이스 보존:
   - [KKULTONG-MANUAL-E:START] ~ [KKULTONG-MANUAL-E:END] 마커 블록과 그 사이의 사용자 작성 블록은
     절대 삭제하거나 다시 쓰지 않고 Notion 페이지 안에 그대로 둔다(중첩 블록·서식·이미지·표 100% 무손실).
   - 마커 바깥쪽 자동 블록만 삭제한 뒤, Notion API 2026-03-11의 position 객체로
     상단 블록(A~D + E 제목)은 position={"type": "start"} + after_block 체이닝으로 삽입하고,
     하단 블록(F~G)은 position={"type": "end"}로 추가한다.
   - 만약 사용자가 끝 마커나 두 마커를 실수로 지운 경우에도 기존 수동 입력 블록을 잃지 않도록
     복구 로직 + SQLite 로컬 백업 스냅샷을 함께 수행한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import (
    DASHBOARD_PAGE_TITLE,
    MANUAL_E_END_MARKER,
    MANUAL_E_START_MARKER,
    PARENT_PAGE_TITLE,
)
from .cache import LocalCache
from .models import DashboardReport
from .notion_client import NotionAPIError, NotionClient, ReadOnlyGuardError
from .parsers import extract_block_plain_text, extract_page_title
from .renderer import (
    build_bottom_blocks,
    build_manual_e_default_blocks,
    build_manual_e_end_marker_block,
    build_manual_e_start_marker_block,
    build_top_blocks,
)

# Notion 단일 Append 요청당 최대 100개 블록 (표 내부 행 포함 여유분 고려해 80/90으로 보수 분할)
MAX_CHILDREN_PER_BATCH = 80
MAX_TABLE_ROWS_PER_BATCH = 90


@dataclass(frozen=True)
class PublishResult:
    parent_page_id: str
    dashboard_page_id: str
    created_page: bool
    preserved_manual_blocks: int
    deleted_auto_blocks: int
    appended_blocks: int
    warnings: list[str]


class DashboardPublisher:
    """대시보드 페이지를 식별·생성하고 섹션 E를 보존하며 본문을 갱신한다."""

    def __init__(
        self,
        client: NotionClient,
        cache: LocalCache,
        *,
        parent_page_id: Optional[str] = None,
        dashboard_page_id: Optional[str] = None,
        parent_page_title: str = PARENT_PAGE_TITLE,
        dashboard_page_title: str = DASHBOARD_PAGE_TITLE,
    ) -> None:
        self.client = client
        self.cache = cache
        self.configured_parent_page_id = parent_page_id
        self.configured_dashboard_page_id = dashboard_page_id
        self.parent_page_title = parent_page_title
        self.dashboard_page_title = dashboard_page_title

    def resolve_parent_page_id(self) -> str:
        """'천하제일 꿀통대회' 부모 페이지 ID를 결정하고 읽기 전용 보호 목록에 등록한다."""
        if self.configured_parent_page_id:
            page = self.client.retrieve_page(self.configured_parent_page_id)
            if _is_trashed(page):
                raise NotionAPIError(
                    f"NOTION_PARENT_PAGE_ID({self.configured_parent_page_id})가 휴지통 상태입니다."
                )
            pid = str(page["id"])
            self.client.register_protected_id(pid)
            self.cache.set_state("parent_page_id", pid)
            return pid

        cached_parent_id = self.cache.get_state("parent_page_id")
        if cached_parent_id:
            try:
                page = self.client.retrieve_page(cached_parent_id)
                if not _is_trashed(page) and extract_page_title(page) == self.parent_page_title:
                    pid = str(page["id"])
                    self.client.register_protected_id(pid)
                    return pid
            except NotionAPIError:
                pass

        candidates = self.client.search_pages(self.parent_page_title)
        exact_matches = [
            p
            for p in candidates
            if not _is_trashed(p) and extract_page_title(p) == self.parent_page_title
        ]
        if len(exact_matches) == 0:
            raise NotionAPIError(
                f"부모 페이지 '{self.parent_page_title}'를 찾을 수 없습니다. "
                "해당 페이지에 통합을 연결(Add connections)하거나 .env에 NOTION_PARENT_PAGE_ID를 지정하세요."
            )
        if len(exact_matches) > 1:
            ids = ", ".join(str(p.get("id")) for p in exact_matches)
            raise NotionAPIError(
                f"제목이 '{self.parent_page_title}'인 페이지가 {len(exact_matches)}건 발견되어 식별할 수 없습니다 "
                f"(후보 ID: {ids}). .env에 NOTION_PARENT_PAGE_ID를 명시하세요."
            )

        pid = str(exact_matches[0]["id"])
        self.client.register_protected_id(pid)
        self.cache.set_state("parent_page_id", pid)
        return pid

    def resolve_or_create_dashboard_page(self, parent_page_id: str) -> tuple[str, bool]:
        """대시보드 페이지를 4단계 우선순위로 식별하고, 없을 때만 최초 1회 생성한다."""
        # 1) 명시적 환경변수 NOTION_DASHBOARD_PAGE_ID
        if self.configured_dashboard_page_id:
            page = self.client.retrieve_page(self.configured_dashboard_page_id)
            self._assert_safe_dashboard_page(page, parent_page_id, strict_parent=False)
            did = str(page["id"])
            self.cache.set_state("dashboard_page_id", did)
            return did, False

        # 2) 로컬 SQLite 저장 ID 검증
        cached_dash_id = self.cache.get_state("dashboard_page_id")
        if cached_dash_id:
            try:
                page = self.client.retrieve_page(cached_dash_id)
                if not _is_trashed(page) and extract_page_title(page) == self.dashboard_page_title:
                    self._assert_safe_dashboard_page(page, parent_page_id, strict_parent=True)
                    return str(page["id"]), False
            except (NotionAPIError, ReadOnlyGuardError):
                pass

        # 3) 부모 페이지의 직접 하위 child_page 스캔 (검색 API 지연 회피 + 강한 범위 한정)
        parent_children = self.client.list_block_children(parent_page_id)
        matching_children = [
            b
            for b in parent_children
            if not _is_trashed(b)
            and b.get("type") == "child_page"
            and extract_block_plain_text(b) == self.dashboard_page_title
        ]
        if len(matching_children) == 1:
            did = str(matching_children[0]["id"])
            self.cache.set_state("dashboard_page_id", did)
            return did, False
        if len(matching_children) > 1:
            # 중복 생성된 경우 마커를 가진 유일한 페이지를 우선 선택, 그래도 모호하면 안전 중단
            marker_holders: list[str] = []
            for child in matching_children:
                cid = str(child["id"])
                child_blocks = self.client.list_block_children(cid)
                if any(MANUAL_E_START_MARKER in extract_block_plain_text(cb) for cb in child_blocks):
                    marker_holders.append(cid)
            if len(marker_holders) == 1:
                did = marker_holders[0]
                self.cache.set_state("dashboard_page_id", did)
                return did, False
            ids = ", ".join(str(b.get("id")) for b in matching_children)
            raise NotionAPIError(
                f"'{self.parent_page_title}' 아래에 '{self.dashboard_page_title}' 페이지가 "
                f"{len(matching_children)}개 중복 존재합니다(ID: {ids}). "
                "중복 페이지를 정리하거나 .env에 NOTION_DASHBOARD_PAGE_ID를 지정하세요."
            )

        # 4) 없을 때만 최초 1회 생성
        created = self.client.create_dashboard_page(
            parent_page_id=parent_page_id,
            title=self.dashboard_page_title,
        )
        did = str(created["id"])
        self.cache.set_state("dashboard_page_id", did)
        return did, True

    def _assert_safe_dashboard_page(
        self,
        page: dict[str, Any],
        parent_page_id: str,
        *,
        strict_parent: bool,
    ) -> None:
        """기존 허브 페이지나 다른 문서에 실수로 쓰는 사고를 원천 차단한다."""
        page_id = str(page.get("id") or "")
        if _is_trashed(page):
            raise NotionAPIError(f"대시보드 페이지({page_id})가 휴지통 상태입니다.")
        if NotionClient._canonical_id(page_id) == NotionClient._canonical_id(parent_page_id):
            raise ReadOnlyGuardError("대시보드 페이지 ID가 부모 페이지 ID와 동일하여 쓰기를 차단했습니다.")
        title = extract_page_title(page)
        if title != self.dashboard_page_title:
            raise ReadOnlyGuardError(
                f"대상 페이지 제목({title!r})이 대시보드 제목({self.dashboard_page_title!r})과 달라 "
                "기존 페이지 보호를 위해 쓰기를 차단했습니다."
            )
        if strict_parent:
            parent = page.get("parent")
            if isinstance(parent, dict) and parent.get("type") == "page_id":
                actual_parent = str(parent.get("page_id") or "")
                if NotionClient._canonical_id(actual_parent) != NotionClient._canonical_id(parent_page_id):
                    raise ReadOnlyGuardError(
                        "캐시된 대시보드 페이지의 부모가 변경되어 재탐색합니다."
                    )

    def publish(self, report: DashboardReport) -> PublishResult:
        """대시보드 페이지를 식별/생성하고 섹션 E를 보존하며 본문을 갱신한다."""
        parent_page_id = self.resolve_parent_page_id()
        dashboard_page_id, created_page = self.resolve_or_create_dashboard_page(parent_page_id)

        existing_blocks = [] if created_page else self.client.list_block_children(dashboard_page_id)
        existing_active = [b for b in existing_blocks if not _is_trashed(b)]

        warnings: list[str] = []
        start_idx, end_idx = _find_manual_marker_indices(existing_active)

        # 케이스 1: 빈 페이지(최초 생성 직후) -> 전체 블록 + 섹션 E 기본 템플릿 일괄 작성
        if not existing_active:
            all_blocks = (
                build_top_blocks(report)
                + build_manual_e_default_blocks()
                + build_bottom_blocks(report)
            )
            appended = self._append_blocks_in_order(
                dashboard_page_id,
                all_blocks,
                initial_position=None,
            )
            return PublishResult(
                parent_page_id=parent_page_id,
                dashboard_page_id=dashboard_page_id,
                created_page=created_page,
                preserved_manual_blocks=0,
                deleted_auto_blocks=0,
                appended_blocks=appended,
                warnings=warnings,
            )

        # 케이스 2: START와 END 마커가 모두 존재 -> 마커 구간 인플레이스 무손실 보존!
        if start_idx is not None and end_idx is not None and start_idx < end_idx:
            preserved_slice = existing_active[start_idx : end_idx + 1]
            self.cache.save_manual_section_snapshot(
                report.updated_at_kst, dashboard_page_id, preserved_slice
            )
            blocks_to_delete = existing_active[:start_idx] + existing_active[end_idx + 1 :]
            deleted = self._delete_blocks(blocks_to_delete)

            top_blocks = build_top_blocks(report)
            bottom_blocks = build_bottom_blocks(report)
            appended_top = self._append_blocks_in_order(
                dashboard_page_id,
                top_blocks,
                initial_position={"type": "start"},
            )
            appended_bottom = self._append_blocks_in_order(
                dashboard_page_id,
                bottom_blocks,
                initial_position={"type": "end"},
            )
            return PublishResult(
                parent_page_id=parent_page_id,
                dashboard_page_id=dashboard_page_id,
                created_page=created_page,
                preserved_manual_blocks=max(0, len(preserved_slice) - 2),
                deleted_auto_blocks=deleted,
                appended_blocks=appended_top + appended_bottom,
                warnings=warnings,
            )

        # 케이스 3: START 마커만 있고 END 마커가 유실된 경우 -> 안전 복구
        if start_idx is not None and end_idx is None:
            recover_end = _find_recovery_end_index(existing_active, start_idx)
            preserved_slice = existing_active[start_idx : recover_end + 1]
            self.cache.save_manual_section_snapshot(
                report.updated_at_kst, dashboard_page_id, preserved_slice
            )
            warning_msg = (
                "섹션 E 끝 마커([KKULTONG-MANUAL-E:END])가 유실되어 기존 수동 입력 구간을 보존한 뒤 "
                "끝 마커를 자동 복구했습니다."
            )
            warnings.append(warning_msg)
            report.warnings.append(warning_msg)

            blocks_to_delete = existing_active[:start_idx] + existing_active[recover_end + 1 :]
            deleted = self._delete_blocks(blocks_to_delete)

            last_preserved_id = str(existing_active[recover_end]["id"])
            appended_marker = self._append_blocks_in_order(
                dashboard_page_id,
                [build_manual_e_end_marker_block()],
                initial_position={"type": "after_block", "after_block": {"id": last_preserved_id}},
            )
            appended_top = self._append_blocks_in_order(
                dashboard_page_id,
                build_top_blocks(report),
                initial_position={"type": "start"},
            )
            appended_bottom = self._append_blocks_in_order(
                dashboard_page_id,
                build_bottom_blocks(report),
                initial_position={"type": "end"},
            )
            return PublishResult(
                parent_page_id=parent_page_id,
                dashboard_page_id=dashboard_page_id,
                created_page=created_page,
                preserved_manual_blocks=max(0, len(preserved_slice) - 1),
                deleted_auto_blocks=deleted,
                appended_blocks=appended_marker + appended_top + appended_bottom,
                warnings=warnings,
            )

        # 케이스 4: 비어 있지 않은 페이지인데 마커가 모두 유실된 경우
        # 'E. 수동 입력' 구간이 보이면 그 아래 사용자 블록을 인플레이스로 살리고 마커를 재부착한다.
        manual_range = _find_heading_e_content_range(existing_active)
        if manual_range is not None:
            m_start, m_end = manual_range
            preserved_slice = existing_active[m_start : m_end + 1]
            self.cache.save_manual_section_snapshot(
                report.updated_at_kst, dashboard_page_id, preserved_slice
            )
            warning_msg = (
                "섹션 E 보존 마커가 모두 유실되었으나 'E. 수동 입력' 하위 블록을 감지해 보존하고 "
                "보존 마커를 자동 복구했습니다."
            )
            warnings.append(warning_msg)
            report.warnings.append(warning_msg)

            blocks_to_delete = existing_active[:m_start] + existing_active[m_end + 1 :]
            deleted = self._delete_blocks(blocks_to_delete)

            # 이제 페이지에는 preserved_slice만 남아 있으므로 앞쪽에 top + START, 뒤쪽에 END + bottom 삽입
            appended_top = self._append_blocks_in_order(
                dashboard_page_id,
                build_top_blocks(report) + [build_manual_e_start_marker_block()],
                initial_position={"type": "start"},
            )
            appended_bottom = self._append_blocks_in_order(
                dashboard_page_id,
                [build_manual_e_end_marker_block()] + build_bottom_blocks(report),
                initial_position={"type": "end"},
            )
            return PublishResult(
                parent_page_id=parent_page_id,
                dashboard_page_id=dashboard_page_id,
                created_page=created_page,
                preserved_manual_blocks=len(preserved_slice),
                deleted_auto_blocks=deleted,
                appended_blocks=appended_top + appended_bottom,
                warnings=warnings,
            )

        # 마커도 E 섹션도 전혀 없는 비정상 상태: 기존 블록 스냅샷을 로컬 백업 후 기본 구조로 재작성
        self.cache.save_manual_section_snapshot(
            report.updated_at_kst, dashboard_page_id, existing_active
        )
        warning_msg = (
            "대시보드 페이지에서 섹션 E 보존 마커를 찾지 못해 기존 블록을 로컬 SQLite에 백업한 뒤 "
            "기본 수동 입력 칸으로 초기화했습니다."
        )
        warnings.append(warning_msg)
        report.warnings.append(warning_msg)
        deleted = self._delete_blocks(existing_active)
        all_blocks = (
            build_top_blocks(report)
            + build_manual_e_default_blocks()
            + build_bottom_blocks(report)
        )
        appended = self._append_blocks_in_order(
            dashboard_page_id,
            all_blocks,
            initial_position=None,
        )
        return PublishResult(
            parent_page_id=parent_page_id,
            dashboard_page_id=dashboard_page_id,
            created_page=created_page,
            preserved_manual_blocks=0,
            deleted_auto_blocks=deleted,
            appended_blocks=appended,
            warnings=warnings,
        )

    def _delete_blocks(self, blocks: list[dict[str, Any]]) -> int:
        deleted = 0
        for block in blocks:
            bid = block.get("id")
            if not bid:
                continue
            self.client.delete_block(str(bid))
            deleted += 1
        return deleted

    def _append_blocks_in_order(
        self,
        page_id: str,
        blocks: list[dict[str, Any]],
        *,
        initial_position: Optional[dict[str, Any]],
    ) -> int:
        """블록 목록을 Notion 100-block 한도와 table row 상한을 지켜 순서대로 삽입한다.

        initial_position={"type": "start"} 인 경우 첫 배치만 start로 삽입하고,
        후속 배치는 직전 배치의 마지막 블록 ID 기준 after_block으로 체이닝해 순서를 보장한다.
        """
        if not blocks:
            return 0

        # 큰 table 블록(> MAX_TABLE_ROWS_PER_BATCH 행)은 초기 행과 초과 행을 분리해 2단계로 추가한다.
        prepared: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for blk in blocks:
            if blk.get("type") == "table" and isinstance(blk.get("table"), dict):
                rows = blk["table"].get("children") or []
                if len(rows) > MAX_TABLE_ROWS_PER_BATCH:
                    head_blk = {
                        "object": "block",
                        "type": "table",
                        "table": {
                            "table_width": blk["table"]["table_width"],
                            "has_column_header": blk["table"].get("has_column_header", True),
                            "has_row_header": blk["table"].get("has_row_header", False),
                            "children": rows[:MAX_TABLE_ROWS_PER_BATCH],
                        },
                    }
                    prepared.append((head_blk, rows[MAX_TABLE_ROWS_PER_BATCH:]))
                    continue
            prepared.append((blk, []))

        total_appended = 0
        current_position = initial_position

        idx = 0
        while idx < len(prepared):
            batch_blocks: list[dict[str, Any]] = []
            batch_overflows: list[tuple[int, list[dict[str, Any]]]] = []
            estimated_elements = 0

            while idx < len(prepared) and len(batch_blocks) < MAX_CHILDREN_PER_BATCH:
                blk, overflow_rows = prepared[idx]
                blk_weight = 1
                if blk.get("type") == "table":
                    blk_weight += len((blk.get("table") or {}).get("children") or [])
                # Notion 단일 페이로드 전체 블록 요소 상한 1,000개 이내 유지 (여유 있게 500 제한)
                if batch_blocks and estimated_elements + blk_weight > 500:
                    break
                if overflow_rows:
                    batch_overflows.append((len(batch_blocks), overflow_rows))
                batch_blocks.append(blk)
                estimated_elements += blk_weight
                idx += 1

            created = self.client.append_block_children(
                page_id,
                batch_blocks,
                position=current_position,
            )
            total_appended += len(batch_blocks)

            # 초과 table_row가 있으면 방금 생성된 table block ID로 청크 추가
            for local_pos, extra_rows in batch_overflows:
                if local_pos < len(created) and created[local_pos].get("id"):
                    table_id = str(created[local_pos]["id"])
                    for r_start in range(0, len(extra_rows), MAX_TABLE_ROWS_PER_BATCH):
                        chunk = extra_rows[r_start : r_start + MAX_TABLE_ROWS_PER_BATCH]
                        self.client.append_block_children(table_id, chunk)
                        total_appended += len(chunk)

            # start / after_block 모드에서는 직전 배치의 마지막 블록 뒤로 체이닝
            if current_position is not None and current_position.get("type") in {"start", "after_block"}:
                if created and created[-1].get("id"):
                    current_position = {
                        "type": "after_block",
                        "after_block": {"id": str(created[-1]["id"])},
                    }

        return total_appended


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("archived"))


def _find_manual_marker_indices(blocks: list[dict[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    start_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for idx, block in enumerate(blocks):
        text = extract_block_plain_text(block)
        if start_idx is None and MANUAL_E_START_MARKER in text:
            start_idx = idx
        elif start_idx is not None and MANUAL_E_END_MARKER in text:
            end_idx = idx
            break
    return start_idx, end_idx


def _find_recovery_end_index(blocks: list[dict[str, Any]], start_idx: int) -> int:
    """END 마커 유실 시 다음 자동 섹션(F. 경쟁 기록 요약 / G. 정산) 직전까지를 수동 구간으로 추정한다."""
    for idx in range(start_idx + 1, len(blocks)):
        block = blocks[idx]
        text = extract_block_plain_text(block).strip()
        if block.get("type") == "heading_2" and (text.startswith("F.") or text.startswith("G.")):
            # 직전 블록이 divider라면 그 앞까지 보존
            if idx - 1 > start_idx and blocks[idx - 1].get("type") == "divider":
                return idx - 2
            return idx - 1
    return len(blocks) - 1


def _find_heading_e_content_range(blocks: list[dict[str, Any]]) -> Optional[tuple[int, int]]:
    """마커가 모두 없을 때 'E. 수동 입력' 제목 아래의 사용자 블록 범위를 찾는다."""
    heading_idx: Optional[int] = None
    for idx, block in enumerate(blocks):
        if block.get("type") == "heading_2" and "수동 입력" in extract_block_plain_text(block):
            heading_idx = idx
            break
    if heading_idx is None:
        return None
    end_idx = _find_recovery_end_index(blocks, heading_idx)
    if end_idx <= heading_idx:
        return None
    return (heading_idx + 1, end_idx)
