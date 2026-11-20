"""도메인 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class ExperienceRow:
    """체험단 신청·당첨 관리 DB의 정규화된 1행."""

    page_id: str
    campaign_name: str
    site: Optional[str]
    category: Optional[str]
    reward_text: str  # 제공내역 원문 그대로 (금액 추정/합산 금지)
    applied_date: Optional[date]
    announced_date: Optional[date]
    review_due_date: Optional[date]
    status: Optional[str]
    created_time: Optional[datetime]
    last_edited_time: Optional[datetime]


@dataclass(frozen=True)
class CompetitionRow:
    """경쟁 기록 DB의 정규화된 1행."""

    page_id: str
    content: str
    record_date: Optional[date]
    owner: Optional[str]
    category: Optional[str]
    verify_grade: Optional[str]  # 앱은 절대 '확인됨'으로 상향하지 않는다
    link: Optional[str]
    chairman_verdict: str
    created_time: Optional[datetime]
    last_edited_time: Optional[datetime]


@dataclass
class SourceSyncResult:
    """단일 데이터 소스의 동기화 결과."""

    source_key: str
    source_label: str
    ok: bool
    sync_mode: str  # "incremental" | "full"
    row_count: int = 0
    fetched_count: int = 0
    error_message: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    rows_raw: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class DeadlineItem:
    """섹션 B: 마감 임박 항목."""

    campaign_name: str
    site: str
    status: str
    review_due_date: date
    d_day: int  # (review_due_date - today_kst).days; <= 3이면 빨간색
    reward_text: str

    @property
    def is_urgent(self) -> bool:
        return self.d_day <= 3

    @property
    def d_day_label(self) -> str:
        if self.d_day == 0:
            return "D-Day"
        if self.d_day > 0:
            return f"D-{self.d_day}"
        return f"D+{abs(self.d_day)} (초과)"


@dataclass(frozen=True)
class MissingDeadlineItem:
    """섹션 B: 마감일이 비어 있는 당첨/리뷰대기 항목."""

    campaign_name: str
    site: str
    status: str
    announced_date: Optional[date]
    reward_text: str


@dataclass(frozen=True)
class SiteStats:
    """섹션 C: 사이트별 체험단 통계."""

    site: str
    applied_count: int
    won_count: int
    lost_count: int
    pending_count: int
    announced_count: int  # 당첨률 표본 수 n = 당첨 + 리뷰대기 + 리뷰완료 + 미당첨

    @property
    def win_rate_pct(self) -> Optional[float]:
        if self.announced_count <= 0:
            return None
        return (self.won_count / self.announced_count) * 100.0

    def win_rate_label(self) -> str:
        if self.win_rate_pct is None:
            return f"- (n={self.announced_count})"
        return f"{self.win_rate_pct:.1f}% (n={self.announced_count})"


@dataclass(frozen=True)
class RewardItem:
    """섹션 D: 현물 실적 항목 (제공내역 원문 그대로)."""

    campaign_name: str
    site: str
    status: str
    reward_text: str  # 원문 그대로, 금액 추정/합산 금지


@dataclass(frozen=True)
class ExperienceAnalytics:
    """체험단 DB 집계 결과 (섹션 B, C, D)."""

    total_count: int
    status_counts: list[tuple[str, int]]
    deadlines: list[DeadlineItem]
    missing_deadlines: list[MissingDeadlineItem]
    site_stats: list[SiteStats]
    total_site_stats: SiteStats
    rewards: list[RewardItem]
    self_purchase_reviews: list[RewardItem] = field(default_factory=list)


@dataclass(frozen=True)
class OwnerVerifyStats:
    """섹션 F: 담당별 확인 등급 분포."""

    owner: str
    total_count: int
    confirmed_count: int
    needs_check_count: int
    unverified_count: int
    not_applicable_count: int
    empty_count: int = 0

    @property
    def graded_sample_size(self) -> int:
        """비율 분모: 해당없음을 제외한 평가 대상 행 수 (빈 값 포함)."""
        return (
            self.confirmed_count
            + self.needs_check_count
            + self.unverified_count
            + self.empty_count
        )

    def ratio_label(self, count: int) -> str:
        n = self.graded_sample_size
        if n <= 0:
            return f"{count}건 (-)"
        pct = (count / n) * 100.0
        return f"{count}건 ({pct:.1f}%)"


@dataclass(frozen=True)
class CompetitionAnalytics:
    """경쟁 기록 DB 집계 결과 (섹션 F)."""

    total_count: int
    recent_24h_total: int
    recent_24h_by_owner: list[tuple[str, int]]
    matrix_owners: list[str]
    matrix_categories: list[str]
    matrix_counts: dict[tuple[str, str], int]
    verify_stats: list[OwnerVerifyStats]
    total_verify_stats: OwnerVerifyStats


@dataclass
class DashboardReport:
    """대시보드 전체 렌더링 입력 모델."""

    updated_at_kst: datetime
    run_status: str  # "정상" | "부분 실패" | "실패"
    failure_reasons: list[str]
    warnings: list[str]
    experience_sync: SourceSyncResult
    competition_sync: SourceSyncResult
    experience_analytics: Optional[ExperienceAnalytics]
    competition_analytics: Optional[CompetitionAnalytics]
