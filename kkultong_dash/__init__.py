"""kkultong-dash P1 (MVP): 꿀통 대시보드 자동 갱신 패키지."""

from __future__ import annotations

from datetime import timedelta, timezone

__version__ = "0.1.0"

# 공식 문서 확인(2026-11-20):
# - Versioning: https://developers.notion.com/reference/versioning
# - Changes by version: https://developers.notion.com/reference/changes-by-version
#   /v1/data_sources 엔드포인트는 2025-09-03에 도입되었고 현행 최신 버전은 2026-03-11이다.
NOTION_API_VERSION = "2026-03-11"
NOTION_API_BASE_URL = "https://api.notion.com/v1"

# 클실장이 2026-11-20 확인한 데이터 소스 ID
# 미확인: collection:// UUID가 실제 POST /v1/data_sources/{id}/query 에서 그대로 수락되는지는
# 토큰 전달 후 첫 실호출 시 검증 필요 (환경변수로 오버라이드 가능).
DEFAULT_EXPERIENCE_DATA_SOURCE_ID = "41a89944-f230-8091-9e54-000b1d8dea3c"
DEFAULT_COMPETITION_DATA_SOURCE_ID = "41b89944-f230-814b-a322-000bfaf7e533"

PARENT_PAGE_TITLE = "천하제일 꿀통대회"
DASHBOARD_PAGE_TITLE = "📊 꿀통 대시보드 (자동 갱신)"

# 섹션 E 수동 입력 보존 구간 마커
MANUAL_E_START_MARKER = "[KKULTONG-MANUAL-E:START]"
MANUAL_E_END_MARKER = "[KKULTONG-MANUAL-E:END]"

# 한국 표준시 (UTC+9, 서머타임 없음 — tzdata 미설치 리눅스 환경에서도 동작하도록 고정 오프셋 사용)
KST = timezone(timedelta(hours=9), name="KST")

# 체험단 DB 상태 분류
EXPERIENCE_STATUS_ORDER = (
    "신청완료",
    "발표대기",
    "당첨",
    "리뷰대기",
    "리뷰완료",
    "미당첨",
)
EXPERIENCE_WON_STATUSES = frozenset({"당첨", "리뷰대기", "리뷰완료"})
EXPERIENCE_ANNOUNCED_STATUSES = frozenset({"당첨", "리뷰대기", "리뷰완료", "미당첨"})
EXPERIENCE_DEADLINE_STATUSES = frozenset({"당첨", "리뷰대기"})

# 경쟁 기록 DB 알려진 선택지 (기존 DB 수정 금지 — 읽기 집계 표시 순서용)
COMPETITION_OWNERS = ("뮤즈", "피클", "큐", "인스팅트")
COMPETITION_CATEGORIES = (
    "꿀통",
    "BM 제안",
    "점검",
    "검증",
    "실적",
    "자료",
    "인스팅트 완료",
)
COMPETITION_VERIFY_GRADES = ("확인됨", "원문대조필요", "미확인", "해당없음")
