"""P3 에이전트 성적표 (대시보드 섹션 H — 📒 팩트 장부 기준) 집계 및 읽기 전용 로더.

핵심 규칙:
1. 데이터 출처는 오직 '📒 팩트 장부'뿐이다. 경쟁 기록 DB의 자기 신고 등급은 일절 사용하지 않는다.
2. 대상 에이전트는 5명('뮤즈', '피클', '큐', '인스팅트', '클실장') 고정 순서로 집계하며,
   점수·건수 기준 정렬(순위·서열화)이나 우열·판정 문구를 생성하지 않는다.
3. '내려간 행'과 '자동 경보 수'는 팩트 장부의 현재 필드(등급, 자동 재검증 결과, 충돌, 대체됨)로
   객관적으로 셀 수 있는 범위에서만 정의하며, 추측으로 과거 이력을 추정하지 않는다.
4. 분모가 0이거나 필수 스키마 필드가 누락되었거나 장부 조회에 실패한 경우 해당 지표를
   '집계 불가'로 표시한다.
5. 유지율의 분모(유지 중 + 내려간 행)가 5 미만인 에이전트는 유지율 칸에 '표본 부족'을 병기한다.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Optional

from . import (
    FACT_LEDGER_DB_TITLE,
    PARENT_PAGE_TITLE,
    SCOREBOARD_AGENTS,
    SCOREBOARD_CHAIRMAN_ACTOR,
    SCOREBOARD_MIN_SAMPLE_SIZE,
)
from .cache import LocalCache
from .fact_ledger import ensure_fact_ledger_database
from .models import AgentScorecard, AgentScoreRow, FactLedgerRow
from .notion_client import NotionAPIError, NotionClient, ReadOnlyGuardError
from .parsers import parse_fact_ledger_row

# 지표별 필수 팩트 장부 속성 정의 (누락 시 추측하지 않고 '집계 불가' 처리)
# P2.5 변경: '대체됨' 관계만 있는 행도 내려간 행에 포함하므로 retention 필수 필드에 '대체됨'을 포함한다.
FACT_LEDGER_SCOREBOARD_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "uploaded": ("올린 이",),
    "retention": ("올린 이", "등급", "자동 재검증 결과", "대체됨"),
    "corrected_against": ("올린 이", "정정한 이"),
    "caught": ("올린 이", "정정한 이"),
    "auto_alert": ("올린 이", "등급", "자동 재검증 결과", "충돌"),
}


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


def is_fact_row_downgraded(row: FactLedgerRow) -> bool:
    """장부의 현재 필드만으로 셀 수 있는 '내려간 행' 여부를 판별한다.

    집계 기준 (P2.5 확정 판정 반영, 현재 14개 필드 범위 내):
    1. 등급 == '불일치':
       P2 충돌 탐지(detect_fact_conflicts)는 '확인됨' 상태의 행만 '불일치'로 내리며,
       수동으로 '불일치'가 부여된 행도 확인 유지에서 탈락한 충돌 행으로 집계한다.
    2. 등급 == '원문대조필요' 이고 자동 재검증 결과 == '발췌 사라짐':
       P2 자동 재검증(judge_revalidation)이 '확인됨' 행에서 원문 발췌 소실을 감지하면
       등급을 '원문대조필요'로 내리고 자동 재검증 결과를 '발췌 사라짐'으로 기록한다.
    3. '대체됨' relation이 1건 이상 연결된 행 (등급 '자동확인불가' 제외):
       P2.5 변경 근거 — 정본 정리·정정 과정에서 다른 행으로 대체된 사실은 더 이상 유지 중인
       정본이 아니므로, '대체됨' 관계만 연결된 행도 내려간 행에 포함한다.

    제외 및 경계:
    - 등급 '자동확인불가'(자동 재검증 결과 '로그인 필요')는 외부 로그인 장벽으로 인한
      자동 검증 불가 상태이며 P2에서도 팩트 경보 대상(add_to_fact_alerts=False)이 아니므로
      기존대로 내려간 행에서 제외한다.
    - '대체됨'이 비어 있고 등급이 '미확인' 또는 '원문대조필요'(발췌 사라짐 외: 미검증,
      발췌 그대로, 확인 불가, 열림 실패)인 행은 초기 대기 행과 수동 하향을 현재 필드만으로
      구분할 수 없어 포함하지 않는다.
    """
    grade = (row.grade or "").strip()
    reval_result = (row.auto_revalidation_result or "").strip()
    if grade == "자동확인불가":
        return False
    if grade == "불일치":
        return True
    if grade == "원문대조필요" and reval_result == "발췌 사라짐":
        return True
    if len(row.superseded_ids) > 0:
        return True
    return False


def is_fact_row_auto_alert_downgraded(row: FactLedgerRow) -> bool:
    """장부의 현재 필드만으로 셀 수 있는 '자동 재검증으로 등급이 내려간 행' 여부를 판별한다.

    집계 기준 (P2 revalidate_fact_ledger의 자동 강등 + 팩트 경보 경로 기준):
    1. 등급 == '원문대조필요' 이고 자동 재검증 결과 == '발췌 사라짐'
    2. 등급 == '불일치' 이고 '충돌' relation이 1건 이상 연결됨
       (detect_fact_conflicts는 '확인됨' 행들을 '불일치'로 내리면서 반드시 '충돌'을 연결함)

    제외 및 경계:
    - 등급 '자동확인불가'(로그인 필요)는 P2 팩트 경보 대상(add_to_fact_alerts=False)이 아니므로
      자동 경보 수에서 제외한다.
    - 현재 등급이 '확인됨'인 행은 과거 자동 재검증 결과 문자열이 남아 있더라도 제외한다.
    - 등급 '불일치'이나 '충돌' relation이 비어 있는 행은 자동 충돌 강등 여부를 확인할 수 없어
      자동 경보 수에서 제외한다(유지율의 내려간 행에는 포함).
    - '대체됨' relation은 수동 정리·대체 표시이므로 자동 경보 수에는 포함하지 않는다.
    """
    grade = (row.grade or "").strip()
    reval_result = (row.auto_revalidation_result or "").strip()
    if grade == "원문대조필요" and reval_result == "발췌 사라짐":
        return True
    if grade == "불일치" and len(row.conflict_ids) > 0:
        return True
    return False


def compute_agent_scorecard(
    rows: list[FactLedgerRow],
    *,
    agents: tuple[str, ...] = SCOREBOARD_AGENTS,
    min_sample_size: int = SCOREBOARD_MIN_SAMPLE_SIZE,
    available_properties: Optional[Collection[str]] = None,
    is_incomplete: bool = False,
) -> AgentScorecard:
    """📒 팩트 장부 행 목록으로부터 섹션 H 에이전트 성적표를 집계한다.

    지표 정의 (P2.5 확정 판정 3건 반영):
    - 올린 팩트 수: '올린 이'가 그 에이전트인 행 수
    - 유지율: 그 에이전트가 올린 행 중 등급 '확인됨' 유지 중('대체됨' 미연결) ÷ (유지 중 + 내려간 행)
      (분모가 0이거나 필수 필드 누락 시 '집계 불가',
       유지율 분모(유지 중 + 내려간 행) < 5이면 유지율 옆에 '표본 부족' 병기)
    - 정정당한 수: 그 에이전트가 올린 행 중 '정정한 이'가 있고 '올린 이'와 다른 행 수
    - 잡아낸 수: '정정한 이'가 그 에이전트이고 '올린 이'가 다른 대상 에이전트 또는 '회장님'인 행 수
      (P2.5 변경 근거: '회장님'이 올린 행의 오류를 에이전트가 정정한 기여도 포함하되,
       타 에이전트 정정과 혼동되지 않도록 대시보드에서 괄호로 분리 표기 — 예: '3 (회장님 행 1)'.
       '올린 이' 미입력, 본인 정정, 대상 외 타인은 제외)
    - 자동 경보 수: 그 에이전트가 올린 행 중 자동 재검증으로 등급이 내려간 행 수
      (필수 필드 누락 시 '집계 불가')
    """
    props_set: Optional[set[str]] = (
        set(available_properties) if available_properties is not None else None
    )

    def _has_fields(metric_key: str) -> bool:
        if props_set is None:
            return True
        return all(f in props_set for f in FACT_LEDGER_SCOREBOARD_REQUIRED_FIELDS[metric_key])

    can_uploaded = _has_fields("uploaded")
    can_retention = _has_fields("retention")
    can_corrected = _has_fields("corrected_against")
    can_caught = _has_fields("caught")
    can_auto_alert = _has_fields("auto_alert")

    missing_fields: list[str] = []
    if props_set is not None:
        all_required = ("올린 이", "정정한 이", "등급", "자동 재검증 결과", "충돌", "대체됨")
        missing_fields = [f for f in all_required if f not in props_set]

    agent_set = set(agents)
    uploaded_counts: dict[str, int] = {a: 0 for a in agents}
    maintained_counts: dict[str, int] = {a: 0 for a in agents}
    downgraded_counts: dict[str, int] = {a: 0 for a in agents}
    corrected_against_counts: dict[str, int] = {a: 0 for a in agents}
    caught_counts: dict[str, int] = {a: 0 for a in agents}
    caught_chairman_counts: dict[str, int] = {a: 0 for a in agents}
    auto_alert_counts: dict[str, int] = {a: 0 for a in agents}

    for row in rows:
        uploader = (row.uploader or "").strip() or None
        corrector = (row.corrector or "").strip() or None
        grade = (row.grade or "").strip() or None

        if uploader in agent_set:
            uploaded_counts[uploader] += 1
            # '대체됨'이 연결된 행은 등급이 아직 '확인됨'으로 남아 있더라도 유지 중이 아니라 내려간 행으로 집계
            if is_fact_row_downgraded(row):
                downgraded_counts[uploader] += 1
            elif grade == "확인됨":
                maintained_counts[uploader] += 1

            if corrector is not None and corrector != uploader:
                corrected_against_counts[uploader] += 1

            if is_fact_row_auto_alert_downgraded(row):
                auto_alert_counts[uploader] += 1

        if corrector in agent_set and uploader is not None and uploader != corrector:
            if uploader in agent_set:
                caught_counts[corrector] += 1
            elif uploader == SCOREBOARD_CHAIRMAN_ACTOR:
                caught_counts[corrector] += 1
                caught_chairman_counts[corrector] += 1

    # 대상 에이전트 고정 순서 유지 (순위·서열 정렬 금지)
    agent_rows: list[AgentScoreRow] = [
        AgentScoreRow(
            agent=agent,
            uploaded_count=uploaded_counts[agent] if can_uploaded else None,
            maintained_count=maintained_counts[agent] if can_retention else None,
            downgraded_count=downgraded_counts[agent] if can_retention else None,
            corrected_against_count=corrected_against_counts[agent] if can_corrected else None,
            caught_count=caught_counts[agent] if can_caught else None,
            auto_alert_count=auto_alert_counts[agent] if can_auto_alert else None,
            min_sample_size=min_sample_size,
            caught_chairman_count=caught_chairman_counts[agent] if can_caught else None,
        )
        for agent in agents
    ]

    return AgentScorecard(
        ok=True,
        total_ledger_rows=len(rows),
        agent_rows=agent_rows,
        missing_fields=tuple(missing_fields),
        is_incomplete=is_incomplete,
    )


def load_fact_ledger_scorecard(
    client: NotionClient,
    cache: LocalCache,
    *,
    parent_page_id: Optional[str] = None,
    configured_database_id: Optional[str] = None,
    configured_data_source_id: Optional[str] = None,
    parent_page_title: str = PARENT_PAGE_TITLE,
    db_title: str = FACT_LEDGER_DB_TITLE,
) -> AgentScorecard:
    """'📒 팩트 장부' DB를 읽기 전용으로 조회해 섹션 H 성적표를 생성한다.

    - DB를 생성하거나 스키마·행을 수정하지 않는다(create_if_missing=False).
    - 조회 실패 시 경쟁 기록 DB나 이전 캐시로 대체하지 않고 ok=False(집계 불가)를 반환한다.
    """
    try:
        init_res = ensure_fact_ledger_database(
            client,
            cache,
            parent_page_id=parent_page_id,
            configured_database_id=configured_database_id,
            configured_data_source_id=configured_data_source_id,
            parent_page_title=parent_page_title,
            db_title=db_title,
            create_if_missing=False,
        )
        ds_id = init_res.data_source_id
        ds_meta = client.retrieve_data_source(ds_id)
        raw_props = ds_meta.get("properties")
        available_properties: Optional[frozenset[str]] = (
            frozenset(raw_props.keys())
            if isinstance(raw_props, dict) and len(raw_props) > 0
            else None
        )
        pages, is_incomplete = client.query_data_source(ds_id)
    except ReadOnlyGuardError:
        raise
    except NotionAPIError as exc:
        return AgentScorecard(ok=False, error_message=str(exc))

    active_pages = [p for p in pages if not _is_trashed(p)]
    rows = [parse_fact_ledger_row(p) for p in active_pages]
    return compute_agent_scorecard(
        rows,
        available_properties=available_properties,
        is_incomplete=is_incomplete,
    )
