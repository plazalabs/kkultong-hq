"""꿀통 대시보드 P1 실행 오케스트레이터."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import KST
from .analytics import compute_competition_analytics, compute_experience_analytics
from .cache import LocalCache
from .config import AppConfig
from .models import DashboardReport, SourceSyncResult
from .notion_client import NotionClient
from .parsers import parse_competition_row, parse_experience_row
from .publisher import DashboardPublisher, PublishResult
from .renderer import format_cli_summary
from .settlement import inspect_settlement_page

# 종료 코드 규칙:
# 0 = 정상, 2 = 부분 실패 (섹션 A에 원인 기록 완료), 1 = 전체 실패
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_PARTIAL_FAILURE = 2


@dataclass(frozen=True)
class RunOutcome:
    report: DashboardReport
    publish_result: Optional[PublishResult]
    exit_code: int
    summary_text: str


def run_dashboard_update(
    config: AppConfig,
    *,
    force_full: bool = False,
    dry_run: bool = False,
    now_kst: Optional[datetime] = None,
    client: Optional[NotionClient] = None,
    cache: Optional[LocalCache] = None,
) -> RunOutcome:
    """두 노션 DB를 동기화하고 대시보드 페이지를 갱신한다."""
    current_kst = (now_kst or datetime.now(KST)).astimezone(KST)

    protected = [
        config.experience_data_source_id,
        config.competition_data_source_id,
    ]
    if config.parent_page_id:
        protected.append(config.parent_page_id)
    if config.settlement_page_id:
        protected.append(config.settlement_page_id)

    notion_client = client or NotionClient(
        config.notion_token,
        api_version=config.api_version,
        base_url=config.api_base_url,
        min_interval_sec=config.min_request_interval_sec,
        timeout_sec=config.request_timeout_sec,
        max_attempts=config.max_retry_attempts,
        max_retry_after_sec=config.max_retry_after_sec,
        protected_ids=protected,
    )
    for pid in protected:
        notion_client.register_protected_id(pid)

    owns_cache = cache is None
    local_cache = cache or LocalCache(config.db_path)

    try:
        exp_sync: SourceSyncResult = local_cache.sync_data_source(
            notion_client,
            source_key="experience",
            source_label="체험단 DB",
            data_source_id=config.experience_data_source_id,
            now_kst=current_kst,
            force_full=force_full,
        )
        comp_sync: SourceSyncResult = local_cache.sync_data_source(
            notion_client,
            source_key="competition",
            source_label="경쟁 기록 DB",
            data_source_id=config.competition_data_source_id,
            now_kst=current_kst,
            force_full=force_full,
        )

        # 실패한 소스는 절대 캐시된 이전 숫자로 계산하지 않는다 (공통 규칙 6)
        exp_analytics = (
            compute_experience_analytics(
                [parse_experience_row(p) for p in exp_sync.rows_raw],
                now_kst=current_kst,
            )
            if exp_sync.ok
            else None
        )
        comp_analytics = (
            compute_competition_analytics(
                [parse_competition_row(p) for p in comp_sync.rows_raw],
                now_kst=current_kst,
            )
            if comp_sync.ok
            else None
        )

        # 섹션 G 정산판 읽기 전용 점검 (접근 불가/비구조화 시에도 대시보드 갱신 실패로 처리하지 않고 수동 확인 유지)
        settlement_summary = inspect_settlement_page(notion_client, config.settlement_page_id)
        # 섹션 A 팩트 경보 로드 (P2 자동 재검증에서 기록된 경보)
        fact_alerts = local_cache.load_fact_alerts()

        failure_reasons: list[str] = []
        if not exp_sync.ok and exp_sync.error_message:
            failure_reasons.append(exp_sync.error_message)
        if not comp_sync.ok and comp_sync.error_message:
            failure_reasons.append(comp_sync.error_message)

        warnings: list[str] = [*exp_sync.warnings, *comp_sync.warnings]

        if exp_sync.ok and comp_sync.ok:
            run_status = "정상"
            exit_code = EXIT_OK
        elif exp_sync.ok or comp_sync.ok:
            run_status = "부분 실패"
            exit_code = EXIT_PARTIAL_FAILURE
        else:
            run_status = "실패"
            exit_code = EXIT_FAILURE

        report = DashboardReport(
            updated_at_kst=current_kst,
            run_status=run_status,
            failure_reasons=failure_reasons,
            warnings=warnings,
            experience_sync=exp_sync,
            competition_sync=comp_sync,
            experience_analytics=exp_analytics,
            competition_analytics=comp_analytics,
            fact_alerts=fact_alerts,
            settlement_summary=settlement_summary,
        )

        publish_result: Optional[PublishResult] = None
        if not dry_run:
            publisher = DashboardPublisher(
                notion_client,
                local_cache,
                parent_page_id=config.parent_page_id,
                dashboard_page_id=config.dashboard_page_id,
                parent_page_title=config.parent_page_title,
                dashboard_page_title=config.dashboard_page_title,
            )
            publish_result = publisher.publish(report)

        local_cache.record_run(
            current_kst,
            report.run_status,
            {
                "dry_run": dry_run,
                "experience": {
                    "ok": exp_sync.ok,
                    "mode": exp_sync.sync_mode,
                    "row_count": exp_sync.row_count,
                    "fetched_count": exp_sync.fetched_count,
                    "error": exp_sync.error_message,
                },
                "competition": {
                    "ok": comp_sync.ok,
                    "mode": comp_sync.sync_mode,
                    "row_count": comp_sync.row_count,
                    "fetched_count": comp_sync.fetched_count,
                    "error": comp_sync.error_message,
                },
                "fact_alerts_count": len(fact_alerts),
                "settlement_structure": settlement_summary.structure_type,
                "warnings": report.warnings,
                "dashboard_page_id": publish_result.dashboard_page_id if publish_result else None,
            },
        )

        summary = format_cli_summary(report)
        return RunOutcome(
            report=report,
            publish_result=publish_result,
            exit_code=exit_code,
            summary_text=summary,
        )
    finally:
        if owns_cache:
            local_cache.close()
