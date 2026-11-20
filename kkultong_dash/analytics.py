"""대시보드 섹션 B, C, D, F 집계 로직 (순수 함수)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from . import (
    COMPETITION_CATEGORIES,
    COMPETITION_OWNERS,
    EXPERIENCE_ANNOUNCED_STATUSES,
    EXPERIENCE_DEADLINE_STATUSES,
    EXPERIENCE_STATUS_ORDER,
    EXPERIENCE_WON_STATUSES,
    KST,
)
from .models import (
    CompetitionAnalytics,
    CompetitionRow,
    DeadlineItem,
    ExperienceAnalytics,
    ExperienceRow,
    MissingDeadlineItem,
    OwnerVerifyStats,
    RewardItem,
    SiteStats,
)
from .parsers import normalize_verify_grade


def compute_experience_analytics(
    rows: list[ExperienceRow],
    *,
    now_kst: datetime,
) -> ExperienceAnalytics:
    """체험단 신청·당첨 관리 DB 행 목록으로부터 섹션 B, C, D 통계를 계산한다."""
    today_kst = now_kst.astimezone(KST).date()

    # 1) 상태별 행 수
    status_counter: Counter[str] = Counter()
    for row in rows:
        status_counter[row.status or "(미입력)"] += 1

    ordered_statuses: list[tuple[str, int]] = []
    seen_statuses: set[str] = set()
    for status in EXPERIENCE_STATUS_ORDER:
        ordered_statuses.append((status, status_counter.get(status, 0)))
        seen_statuses.add(status)
    for status in sorted(status_counter):
        if status not in seen_statuses:
            ordered_statuses.append((status, status_counter[status]))

    # 2) 섹션 B: 마감 임박 (상태: 당첨·리뷰대기)
    deadlines: list[DeadlineItem] = []
    missing_deadlines: list[MissingDeadlineItem] = []
    for row in rows:
        if row.status not in EXPERIENCE_DEADLINE_STATUSES:
            continue
        site_label = row.site or "(미입력)"
        if row.review_due_date is not None:
            d_day = (row.review_due_date - today_kst).days
            deadlines.append(
                DeadlineItem(
                    campaign_name=row.campaign_name,
                    site=site_label,
                    status=row.status,
                    review_due_date=row.review_due_date,
                    d_day=d_day,
                    reward_text=row.reward_text,
                )
            )
        else:
            missing_deadlines.append(
                MissingDeadlineItem(
                    campaign_name=row.campaign_name,
                    site=site_label,
                    status=row.status,
                    announced_date=row.announced_date,
                    reward_text=row.reward_text,
                )
            )

    deadlines.sort(key=lambda item: (item.d_day, item.review_due_date, item.campaign_name))
    missing_deadlines.sort(
        key=lambda item: (
            item.announced_date is None,
            item.announced_date or date.max,
            item.campaign_name,
        )
    )

    # 3) 섹션 C: 사이트별 신청·당첨 수와 당첨률
    rows_by_site: dict[str, list[ExperienceRow]] = defaultdict(list)
    for row in rows:
        rows_by_site[row.site or "(미입력)"].append(row)

    site_stats: list[SiteStats] = []
    for site_name, site_rows in rows_by_site.items():
        site_stats.append(_build_site_stats(site_name, site_rows))
    site_stats.sort(key=lambda s: (-s.applied_count, -s.won_count, s.site))
    total_site_stats = _build_site_stats("합계", rows)

    # 4) 섹션 D: 현물 실적 (상태: 당첨·리뷰대기·리뷰완료, 제공내역 원문 그대로)
    reward_candidates = [r for r in rows if r.status in EXPERIENCE_WON_STATUSES]
    reward_candidates.sort(
        key=lambda r: (
            -(r.review_due_date or r.announced_date or r.applied_date or date.min).toordinal(),
            r.campaign_name,
        )
    )
    rewards = [
        RewardItem(
            campaign_name=r.campaign_name,
            site=r.site or "(미입력)",
            status=r.status or "(미입력)",
            reward_text=r.reward_text,  # 원문 그대로 유지
        )
        for r in reward_candidates
    ]

    return ExperienceAnalytics(
        total_count=len(rows),
        status_counts=ordered_statuses,
        deadlines=deadlines,
        missing_deadlines=missing_deadlines,
        site_stats=site_stats,
        total_site_stats=total_site_stats,
        rewards=rewards,
    )


def _build_site_stats(site_name: str, rows: list[ExperienceRow]) -> SiteStats:
    applied = len(rows)
    won = sum(1 for r in rows if r.status in EXPERIENCE_WON_STATUSES)
    lost = sum(1 for r in rows if r.status == "미당첨")
    announced = sum(1 for r in rows if r.status in EXPERIENCE_ANNOUNCED_STATUSES)
    pending = applied - announced
    return SiteStats(
        site=site_name,
        applied_count=applied,
        won_count=won,
        lost_count=lost,
        pending_count=pending,
        announced_count=announced,
    )


def compute_competition_analytics(
    rows: list[CompetitionRow],
    *,
    now_kst: datetime,
) -> CompetitionAnalytics:
    """경쟁 기록 DB 행 목록으로부터 섹션 F 통계를 계산한다."""
    now_utc = now_kst.astimezone(timezone.utc)
    cutoff_utc = now_utc - timedelta(hours=24)

    # 담당자 순서: 기본 4명 + 실제 데이터에만 나타난 추가/미지정 담당자
    extra_owners = sorted(
        {
            (r.owner or "(미지정)")
            for r in rows
            if (r.owner or "(미지정)") not in COMPETITION_OWNERS
        }
    )
    owners = list(COMPETITION_OWNERS) + extra_owners

    # 구분 순서: 기본 7개 + 실제 데이터에만 나타난 추가/미지정 구분
    extra_categories = sorted(
        {
            (r.category or "(미지정)")
            for r in rows
            if (r.category or "(미지정)") not in COMPETITION_CATEGORIES
        }
    )
    categories = list(COMPETITION_CATEGORIES) + extra_categories

    # 1) 최근 24시간 새 행 수 (created_time 기준)
    recent_counter: Counter[str] = Counter()
    recent_total = 0
    for row in rows:
        if row.created_time is not None and row.created_time.astimezone(timezone.utc) >= cutoff_utc:
            owner_key = row.owner or "(미지정)"
            recent_counter[owner_key] += 1
            recent_total += 1
    recent_by_owner = [(owner, recent_counter.get(owner, 0)) for owner in owners]

    # 2) 담당 × 구분 교차표
    matrix_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        owner_key = row.owner or "(미지정)"
        cat_key = row.category or "(미지정)"
        matrix_counts[(owner_key, cat_key)] += 1

    # 3) 확인 등급 분포 (규칙 5: 절대 '확인됨'으로 상향하지 않음)
    grade_by_owner: dict[str, Counter[str]] = {owner: Counter() for owner in owners}
    total_grades: Counter[str] = Counter()
    for row in rows:
        owner_key = row.owner or "(미지정)"
        grade = normalize_verify_grade(row.verify_grade)
        grade_by_owner[owner_key][grade] += 1
        total_grades[grade] += 1

    verify_stats: list[OwnerVerifyStats] = []
    for owner in owners:
        c = grade_by_owner[owner]
        owner_total = sum(c.values())
        verify_stats.append(
            OwnerVerifyStats(
                owner=owner,
                total_count=owner_total,
                confirmed_count=c.get("확인됨", 0),
                needs_check_count=c.get("원문대조필요", 0),
                unverified_count=c.get("미확인", 0),
                not_applicable_count=c.get("해당없음", 0),
            )
        )

    total_verify_stats = OwnerVerifyStats(
        owner="합계",
        total_count=len(rows),
        confirmed_count=total_grades.get("확인됨", 0),
        needs_check_count=total_grades.get("원문대조필요", 0),
        unverified_count=total_grades.get("미확인", 0),
        not_applicable_count=total_grades.get("해당없음", 0),
    )

    return CompetitionAnalytics(
        total_count=len(rows),
        recent_24h_total=recent_total,
        recent_24h_by_owner=recent_by_owner,
        matrix_owners=owners,
        matrix_categories=categories,
        matrix_counts=dict(matrix_counts),
        verify_stats=verify_stats,
        total_verify_stats=total_verify_stats,
    )
