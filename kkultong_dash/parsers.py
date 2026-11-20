"""Notion API 페이지/블록 JSON 파서.

공통 규칙 준수:
- 규칙 4(추측 금지): 확인되지 않은 속성 형식이나 누락 값은 방어적으로 처리한다.
- 규칙 5(등급 상향 금지): 경쟁 기록 DB의 '확인' 등급은 절대 '확인됨'으로 올리지 않는다.
  원본에 '확인됨'이 명시된 경우만 '확인됨'으로 표시하고, 비어 있거나 알 수 없는 값은 '미확인'으로 내려 표시한다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from . import KST
from .models import CompetitionRow, ExperienceRow


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 타임스탬프를 timezone-aware datetime으로 파싱한다 (Python 3.9/3.10 Z 접미사 호환)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_notion_date(prop: Optional[dict[str, Any]]) -> Optional[date]:
    """Notion date 속성에서 KST 기준 날짜(date)를 추출한다."""
    if not isinstance(prop, dict):
        return None
    date_obj = prop.get("date")
    if not isinstance(date_obj, dict):
        return None
    start = date_obj.get("start")
    if not isinstance(start, str) or not start.strip():
        return None
    raw = start.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    dt = parse_iso_datetime(raw)
    if dt is None:
        return None
    return dt.astimezone(KST).date()


def extract_rich_text_plain(items: Any) -> str:
    """rich_text 또는 title 배열에서 평문 문자열을 원문 그대로 이어 붙인다."""
    if not isinstance(items, list):
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if isinstance(plain, str):
            chunks.append(plain)
            continue
        text_obj = item.get("text")
        if isinstance(text_obj, dict) and isinstance(text_obj.get("content"), str):
            chunks.append(text_obj["content"])
    return "".join(chunks)


def extract_title_property(properties: dict[str, Any], prop_name: str) -> str:
    """지정한 title 속성(없으면 type=='title' 속성 폴백)의 평문을 추출한다."""
    prop = properties.get(prop_name)
    if isinstance(prop, dict) and isinstance(prop.get("title"), list):
        return extract_rich_text_plain(prop["title"]).strip()
    for candidate in properties.values():
        if isinstance(candidate, dict) and candidate.get("type") == "title":
            return extract_rich_text_plain(candidate.get("title")).strip()
    return ""


def extract_text_property(properties: dict[str, Any], prop_name: str) -> str:
    """rich_text(text) 속성의 평문을 원문 그대로 추출한다."""
    prop = properties.get(prop_name)
    if not isinstance(prop, dict):
        return ""
    if isinstance(prop.get("rich_text"), list):
        return extract_rich_text_plain(prop["rich_text"])
    return ""


def extract_select_property(properties: dict[str, Any], prop_name: str) -> Optional[str]:
    """select 속성의 name 문자열을 추출한다 (없으면 None)."""
    prop = properties.get(prop_name)
    if not isinstance(prop, dict):
        return None
    select_obj = prop.get("select")
    if isinstance(select_obj, dict):
        name = select_obj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    # 방어적 처리: 혹시 status 타입으로 변경되었을 경우에도 읽기만 지원 (미확인 대비)
    status_obj = prop.get("status")
    if isinstance(status_obj, dict):
        name = status_obj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def extract_url_property(properties: dict[str, Any], prop_name: str) -> Optional[str]:
    """url 속성 문자열을 추출한다."""
    prop = properties.get(prop_name)
    if not isinstance(prop, dict):
        return None
    url = prop.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def extract_page_title(page: dict[str, Any]) -> str:
    """일반 Notion 페이지 객체에서 title 평문을 추출한다."""
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return ""
    return extract_title_property(properties, "title")


def extract_block_plain_text(block: dict[str, Any]) -> str:
    """Notion 블록 객체에서 판별용 평문 문자열을 추출한다."""
    block_type = block.get("type")
    if not isinstance(block_type, str):
        return ""
    body = block.get(block_type)
    if not isinstance(body, dict):
        return ""
    if block_type == "child_page":
        title = body.get("title")
        return title if isinstance(title, str) else ""
    if "rich_text" in body:
        return extract_rich_text_plain(body.get("rich_text"))
    return ""


def normalize_verify_grade(raw_grade: Optional[str]) -> str:
    """경쟁 기록 DB의 '확인' 등급을 정규화한다.

    규칙 5: 앱은 등급을 '확인됨'으로 올리지 않는다. 내리기와 표시만 한다.
    확인 등급이 비어 있는 행은 '미확인'에 합치지 않고 '빈 값'으로 별도 분류한다.
    """
    if raw_grade is None or not isinstance(raw_grade, str) or not raw_grade.strip():
        return "빈 값"
    cleaned = raw_grade.strip()
    if cleaned in {"확인됨", "원문대조필요", "미확인", "해당없음", "빈 값"}:
        return cleaned
    # 알 수 없는 비어 있지 않은 값은 절대 '확인됨'으로 상향하지 않고 '미확인'으로 내려 표시한다.
    return "미확인"


def parse_experience_row(page: dict[str, Any]) -> ExperienceRow:
    """체험단 신청·당첨 관리 DB 페이지 1건을 ExperienceRow로 변환한다."""
    props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    return ExperienceRow(
        page_id=str(page.get("id") or ""),
        campaign_name=extract_title_property(props, "캠페인명") or "(캠페인명 미입력)",
        site=extract_select_property(props, "사이트"),
        category=extract_select_property(props, "분류"),
        # 제공내역은 원문 그대로 보존 (금액 파싱/추정/합산 금지)
        reward_text=extract_text_property(props, "제공내역").strip(),
        applied_date=parse_notion_date(props.get("신청일")),
        announced_date=parse_notion_date(props.get("선정발표일")),
        review_due_date=parse_notion_date(props.get("리뷰마감일")),
        status=extract_select_property(props, "상태"),
        created_time=parse_iso_datetime(page.get("created_time")),
        last_edited_time=parse_iso_datetime(page.get("last_edited_time")),
    )


def parse_competition_row(page: dict[str, Any]) -> CompetitionRow:
    """경쟁 기록 DB 페이지 1건을 CompetitionRow로 변환한다."""
    props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    return CompetitionRow(
        page_id=str(page.get("id") or ""),
        content=extract_title_property(props, "내용") or "(내용 미입력)",
        record_date=parse_notion_date(props.get("날짜")),
        owner=extract_select_property(props, "담당"),
        category=extract_select_property(props, "구분"),
        verify_grade=normalize_verify_grade(extract_select_property(props, "확인")),
        link=extract_url_property(props, "링크"),
        chairman_verdict=extract_text_property(props, "회장님 판정").strip(),
        created_time=parse_iso_datetime(page.get("created_time")),
        last_edited_time=parse_iso_datetime(page.get("last_edited_time")),
    )
