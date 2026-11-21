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


@dataclass(frozen=True)
class FactAlert:
    """대시보드 섹션 A '팩트 경보' 항목."""

    page_id: str
    claim: str
    topic_key: str
    reason: str
    url: Optional[str] = None
    old_grade: Optional[str] = None
    new_grade: Optional[str] = None
    detected_at_kst: Optional[str] = None


@dataclass(frozen=True)
class SettlementSummary:
    """대시보드 섹션 G 정산판 조회 결과."""

    page_id: Optional[str]
    title: str
    url: str
    accessible: bool
    structured: bool
    structure_type: str  # "database" | "table" | "unstructured" | "error" | "unconfigured"
    summary_lines: list[str] = field(default_factory=list)
    table_headers: list[str] = field(default_factory=list)
    table_rows: list[list[str]] = field(default_factory=list)
    error_message: Optional[str] = None


@dataclass(frozen=True)
class FactLedgerRow:
    """'📒 팩트 장부' DB의 정규화된 1행."""

    page_id: str
    claim: str  # 주장 (title)
    topic_key: str  # 주제키 (text)
    value: str  # 값 (text)
    source_url: Optional[str]  # 근거 URL (url)
    excerpt: str  # 원문 발췌 (text)
    verified_at: Optional[datetime] = None  # 확인 시각 (date, with time)
    verifier: Optional[str] = None  # 확인자 (select)
    grade: Optional[str] = None  # 등급 (select)
    auto_revalidated_at: Optional[datetime] = None  # 자동 재검증 시각 (date)
    auto_revalidation_result: Optional[str] = None  # 자동 재검증 결과 (select)
    conflict_ids: tuple[str, ...] = ()  # 충돌 (relation, self-DB)
    superseded_ids: tuple[str, ...] = ()  # 대체됨 (relation, self-DB)
    uploader: Optional[str] = None  # 올린 이 (select)
    corrector: Optional[str] = None  # 정정한 이 (select)


@dataclass(frozen=True)
class FactSeedCandidate:
    """시드 입력용으로 추출된 플랫폼 수치 후보 1건."""

    platform: str
    claim: str
    topic_key: str
    value: str
    source_url: Optional[str]
    grade: str  # 근거 URL 없음 -> '미확인', v2 근거 URL 있음 -> '원문대조필요' (절대 '확인됨' 불가)
    excerpt: str = ""
    uploader: Optional[str] = None
    source_page_label: str = ""


@dataclass(frozen=True)
class AgentScoreRow:
    """대시보드 섹션 H: 단일 에이전트의 📒 팩트 장부 기준 성적표 행 (숫자 전용, 순위·판정 없음)."""

    agent: str
    uploaded_count: Optional[int]  # 올린 팩트 수 ('올린 이' == agent)
    maintained_count: Optional[int]  # 등급 '확인됨'으로 유지 중인 행 수
    downgraded_count: Optional[int]  # 장부 현재 필드로 집계 가능한 내려간 행 수
    corrected_against_count: Optional[int]  # 정정당한 수 ('올린 이' == agent, '정정한 이' 있고 != '올린 이')
    caught_count: Optional[int]  # 잡아낸 수 ('정정한 이' == agent, '올린 이'가 다른 대상 에이전트)
    auto_alert_count: Optional[int]  # 자동 재검증으로 등급이 내려간 행 수
    min_sample_size: int = 5

    @property
    def is_low_sample(self) -> bool:
        """올린 팩트 수가 기준(기본 5) 미만이면 표본 부족."""
        return self.uploaded_count is not None and self.uploaded_count < self.min_sample_size

    @property
    def retention_denominator(self) -> Optional[int]:
        if self.maintained_count is None or self.downgraded_count is None:
            return None
        return self.maintained_count + self.downgraded_count

    @property
    def retention_rate_pct(self) -> Optional[float]:
        denom = self.retention_denominator
        if denom is None or denom <= 0 or self.maintained_count is None:
            return None
        return (self.maintained_count / denom) * 100.0

    def retention_label(self) -> str:
        """유지율 칸 문자열 (숫자·집계 불가·표본 부족만 표기, 순위·판정 문구 금지)."""
        pct = self.retention_rate_pct
        denom = self.retention_denominator
        if pct is None or denom is None or self.maintained_count is None:
            if self.is_low_sample:
                return "집계 불가 (표본 부족)"
            return "집계 불가"
        if self.is_low_sample:
            return f"{pct:.1f}% ({self.maintained_count}/{denom}, 표본 부족)"
        return f"{pct:.1f}% ({self.maintained_count}/{denom})"

    @staticmethod
    def count_label(value: Optional[int]) -> str:
        if value is None:
            return "집계 불가"
        return str(value)


@dataclass(frozen=True)
class AgentScorecard:
    """대시보드 섹션 H: 📒 팩트 장부 기준 에이전트 성적표 집계 결과."""

    ok: bool
    total_ledger_rows: int = 0
    agent_rows: list[AgentScoreRow] = field(default_factory=list)
    missing_fields: tuple[str, ...] = ()
    is_incomplete: bool = False
    error_message: Optional[str] = None


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
    fact_alerts: list[FactAlert] = field(default_factory=list)
    settlement_summary: Optional[SettlementSummary] = None
    agent_scorecard: Optional[AgentScorecard] = None
