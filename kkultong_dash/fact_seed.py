"""P2 팩트 장부 초기 시드 입력 모듈.

지시 사항(섹션 6-4):
- 두 노션 페이지('C 갈래 영어 시범 — 작업 분할 v1', '원소스 멀티유즈 워크플로우 v2')를 읽기 전용으로 조회한다.
- 대상 플랫폼 5종(Gumroad, Patreon, Medium, 네이버 프리미엄콘텐츠, Substack)의 수치를 팩트 장부 행으로 변환한다.
- 근거 URL은 반드시 v2 페이지에 적힌 URL만 사용한다(v1 URL 또는 외부 추측 URL 사용 금지).
- 근거 URL이 없는 수치는 등급 '미확인'으로 입력하며, 앱은 절대 등급을 '확인됨'으로 설정하지 않는다.
- 페이지를 찾을 수 없거나 접근할 수 없으면 행을 추측·생성하지 않고 실패 원인을 명확히 보고한다.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from . import (
    DEFAULT_SEED_V1_PAGE_TITLE,
    DEFAULT_SEED_V2_PAGE_TITLE,
    FACT_LEDGER_DB_TITLE,
    PARENT_PAGE_TITLE,
)
from .cache import LocalCache
from .config import normalize_notion_id
from .fact_ledger import ensure_fact_ledger_database
from .models import FactSeedCandidate
from .notion_client import NotionAPIError, NotionClient
from .parsers import (
    extract_block_plain_text,
    extract_page_title,
    extract_rich_text_links,
    extract_rich_text_plain,
    parse_fact_ledger_row,
)

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`,;“”‘’「」『』（）【】《》]+")
_UUID_LIKE_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
# 수치 패턴: 숫자 + 단위(%, $, 원, 달러, USD, KRW, 일, 개월, 명, 건, 회, EUR 등) 또는 화폐기호 + 숫자
_FIGURE_RE = re.compile(
    r"(?:[$€£₩]\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:%|퍼센트|원|만원|천원|억|달러|불|USD|KRW|EUR|GBP|일|주|개월|년|명|건|회|편|개|MB|GB|TB))",
    re.IGNORECASE,
)

_INTERNAL_NOTION_HOSTS = frozenset({"notion.so", "www.notion.so", "app.notion.com", "notion.site"})

_PLATFORM_SPECS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Gumroad",
        "gumroad",
        ("gumroad", "검로드"),
        ("gumroad.com",),
    ),
    (
        "Patreon",
        "patreon",
        ("patreon", "패트리온", "패트레온"),
        ("patreon.com",),
    ),
    (
        "Medium",
        "medium",
        ("medium", "미디엄"),
        ("medium.com",),
    ),
    (
        "네이버 프리미엄콘텐츠",
        "naver_premium",
        ("네이버 프리미엄콘텐츠", "네이버 프리미엄 콘텐츠", "프리미엄콘텐츠", "네프콘", "naver premium"),
        ("contents.premium.naver.com", "premium.naver.com", "help.naver.com", "naver.com"),
    ),
    (
        "Substack",
        "substack",
        ("substack", "서브스택"),
        ("substack.com",),
    ),
)

_METRIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("payment_fee", ("결제 수수료", "pg 수수료", "stripe", "카드 수수료", "payment fee", "processing fee")),
    ("fee", ("수수료", "fee", "commission", "플랫폼 수수료", "매출 수수료")),
    ("payout", ("정산", "지급", "출금", "payout", "withdrawal", "최소 정산")),
    ("membership", ("멤버십", "구독료", "월정액", "유료 구독", "membership", "subscription", "mpp")),
    ("pricing", ("가격", "단가", "최소 가격", "최대 가격", "pricing", "price")),
    ("revenue_share", ("수익 배분", "배분", "분배", "revenue share", "수익률")),
    ("tax", ("세금", "원천징수", "부가세", "vat", "tax")),
    ("requirement", ("조건", "자격", "요건", "기준", "팔로워", "구독자", "requirement", "eligibility")),
)

_RECURSIVE_BLOCK_TYPES = frozenset(
    {
        "table",
        "toggle",
        "callout",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "column_list",
        "column",
        "synced_block",
    }
)


class SeedPageAccessError(RuntimeError):
    """시드 입력 원본 페이지를 찾거나 읽을 수 없을 때 발생하는 예외."""


@dataclass(frozen=True)
class DocumentUnit:
    """노션 페이지에서 평탄화해 추출한 단위 블록(문단/불릿/표 행)."""

    kind: str  # "heading" | "text" | "table_row"
    text: str
    heading_context: str = ""
    table_headers: tuple[str, ...] = ()
    table_cells: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactSeedOutcome:
    ok: bool
    v1_page_id: Optional[str]
    v2_page_id: Optional[str]
    v1_title: str
    v2_title: str
    candidates: list[FactSeedCandidate]
    created_count: int
    skipped_existing_count: int
    error_message: Optional[str] = None
    summary_text: str = ""


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


_LEADING_URL_WRAP = "([<{`'\"“‘「『（【《"
_TRAILING_URL_PUNCT = ".,;:!?`'\"”’」』）】》"
_MATCHED_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}
_TRAILING_KOREAN_PARTICLES: tuple[str, ...] = (
    "에서는",
    "에서도",
    "으로는",
    "으로서",
    "으로써",
    "이라고",
    "이라는",
    "이라도",
    "에서",
    "에게",
    "으로",
    "부터",
    "까지",
    "처럼",
    "보다",
    "마다",
    "라고",
    "라는",
    "이며",
    "이고",
    "이나",
    "라도",
    "이다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "와",
    "과",
    "도",
    "만",
    "로",
)


def _is_hangul(ch: str) -> bool:
    code = ord(ch)
    return (0xAC00 <= code <= 0xD7A3) or (0x1100 <= code <= 0x11FF) or (0x3130 <= code <= 0x318F)


def _clean_url(raw: str) -> str:
    """URL 끝부분의 한국어 조사, 괄호, 따옴표, 마침표 등을 반복 제거한다."""
    cleaned = (raw or "").strip().lstrip(_LEADING_URL_WRAP)
    while cleaned:
        prev = cleaned
        cleaned = cleaned.rstrip(_TRAILING_URL_PUNCT)
        while cleaned and cleaned[-1] in _MATCHED_CLOSERS:
            closer = cleaned[-1]
            opener = _MATCHED_CLOSERS[closer]
            if cleaned.count(closer) > cleaned.count(opener):
                cleaned = cleaned[:-1]
            else:
                break
        for particle in _TRAILING_KOREAN_PARTICLES:
            if cleaned.endswith(particle) and len(cleaned) > len(particle):
                prefix_char = cleaned[-len(particle) - 1]
                if not _is_hangul(prefix_char):
                    cleaned = cleaned[: -len(particle)]
                    break
        if cleaned == prev:
            break
    return cleaned


def _is_external_evidence_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host or "." not in host or host.startswith(".") or host.endswith(".") or ".." in host:
        return False
    try:
        _ = parsed.port
        host.encode("idna")
    except (ValueError, UnicodeError):
        return False
    if host in _INTERNAL_NOTION_HOSTS or host.endswith(".notion.site"):
        return False
    # 경로가 비어 있거나 '/'만 있는 루트 도메인 URL은 근거로 채택하지 않는다
    if not parsed.path.strip("/") and not parsed.query:
        return False
    return True


def sanitize_evidence_url(raw: str) -> Optional[str]:
    """URL 후보를 정리·파싱 검증한 뒤 근거로 채택 가능한 URL만 반환한다."""
    cleaned = _clean_url(raw)
    if not _is_external_evidence_url(cleaned):
        return None
    return cleaned


def _extract_urls_from_text_and_rt(text: str, rich_text: Any = None) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for link in extract_rich_text_links(rich_text):
        cleaned = sanitize_evidence_url(link)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    for match in _URL_RE.findall(text or ""):
        cleaned = sanitize_evidence_url(match)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    return tuple(found)


def _strip_urls(text: str) -> str:
    return re.sub(r"\s+", " ", _URL_RE.sub("", text)).strip(" -|()[]")


def _looks_like_page_id(identifier: str) -> Optional[str]:
    cleaned = identifier.strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        norm = normalize_notion_id(cleaned)
        if norm and _UUID_LIKE_RE.match(norm):
            return norm
        return None
    if _UUID_LIKE_RE.match(cleaned):
        return normalize_notion_id(cleaned)
    return None


def resolve_seed_page(client: NotionClient, identifier: str, *, label: str) -> tuple[str, str]:
    """페이지 ID(또는 Notion URL) 또는 제목 정확 일치로 시드 원본 페이지를 찾는다."""
    raw = (identifier or "").strip()
    if not raw:
        raise SeedPageAccessError(f"{label} 페이지 식별자(ID 또는 제목)가 비어 있습니다.")

    maybe_id = _looks_like_page_id(raw)
    if maybe_id:
        try:
            page = client.retrieve_page(maybe_id)
        except NotionAPIError as exc:
            raise SeedPageAccessError(f"{label} 페이지({maybe_id}) 조회 실패: {exc}") from exc
        if _is_trashed(page):
            raise SeedPageAccessError(f"{label} 페이지({maybe_id})가 휴지통 상태입니다.")
        pid = str(page["id"])
        client.register_protected_id(pid)
        return pid, extract_page_title(page) or raw

    try:
        candidates = client.search_pages(raw)
    except NotionAPIError as exc:
        raise SeedPageAccessError(f"{label} 페이지('{raw}') 검색 실패: {exc}") from exc

    exact = [p for p in candidates if not _is_trashed(p) and extract_page_title(p) == raw]
    if len(exact) == 0:
        raise SeedPageAccessError(
            f"{label} 페이지('{raw}')를 찾을 수 없습니다. "
            "통합 공유 권한(Add connections) 또는 페이지 ID 인자를 확인하세요."
        )
    if len(exact) > 1:
        ids = ", ".join(str(p.get("id")) for p in exact)
        raise SeedPageAccessError(
            f"{label} 페이지('{raw}')가 {len(exact)}건 중복 검색되어 식별할 수 없습니다 (후보 ID: {ids}). "
            "정확한 페이지 ID를 인자로 전달하세요."
        )

    pid = str(exact[0]["id"])
    client.register_protected_id(pid)
    return pid, extract_page_title(exact[0]) or raw


def read_page_units(
    client: NotionClient,
    page_id: str,
    *,
    max_depth: int = 4,
) -> list[DocumentUnit]:
    """노션 페이지의 블록 트리를 읽기 전용으로 순회하여 DocumentUnit 목록으로 평탄화한다."""
    units: list[DocumentUnit] = []
    heading_stack: list[str] = [""]

    def _walk(block_id: str, depth: int) -> None:
        blocks = client.list_block_children(block_id)
        for blk in blocks:
            if _is_trashed(blk):
                continue
            btype = str(blk.get("type") or "")
            body = blk.get(btype) if isinstance(blk.get(btype), dict) else {}

            if btype in {"heading_1", "heading_2", "heading_3"}:
                rt = body.get("rich_text")
                text = extract_rich_text_plain(rt).strip()
                urls = _extract_urls_from_text_and_rt(text, rt)
                if text:
                    heading_stack[0] = text
                    units.append(DocumentUnit(kind="heading", text=text, heading_context=text, urls=urls))
                continue

            if btype == "table" and blk.get("id"):
                has_col_header = bool(body.get("has_column_header", True))
                row_blocks = [
                    rb
                    for rb in client.list_block_children(str(blk["id"]))
                    if not _is_trashed(rb) and rb.get("type") == "table_row"
                ]
                headers: tuple[str, ...] = ()
                for r_idx, rb in enumerate(row_blocks):
                    cells_raw = (rb.get("table_row") or {}).get("cells") or []
                    cell_texts = tuple(extract_rich_text_plain(c).strip() for c in cells_raw)
                    cell_urls: list[str] = []
                    for c_raw, c_txt in zip(cells_raw, cell_texts):
                        for u in _extract_urls_from_text_and_rt(c_txt, c_raw):
                            if u not in cell_urls:
                                cell_urls.append(u)
                    if r_idx == 0 and has_col_header:
                        headers = cell_texts
                        continue
                    units.append(
                        DocumentUnit(
                            kind="table_row",
                            text=" | ".join(cell_texts),
                            heading_context=heading_stack[0],
                            table_headers=headers,
                            table_cells=cell_texts,
                            urls=tuple(cell_urls),
                        )
                    )
                continue

            if "rich_text" in body:
                rt = body.get("rich_text")
                text = extract_rich_text_plain(rt).strip()
                urls = _extract_urls_from_text_and_rt(text, rt)
                if text or urls:
                    units.append(
                        DocumentUnit(
                            kind="text",
                            text=text,
                            heading_context=heading_stack[0],
                            urls=urls,
                        )
                    )
            elif btype == "bookmark":
                raw_url = str(body.get("url") or "").strip()
                cleaned_url = sanitize_evidence_url(raw_url)
                caption = extract_rich_text_plain(body.get("caption")).strip()
                urls = (cleaned_url,) if cleaned_url else ()
                if urls or caption:
                    units.append(
                        DocumentUnit(
                            kind="text",
                            text=caption or (cleaned_url or raw_url),
                            heading_context=heading_stack[0],
                            urls=urls,
                        )
                    )

            if depth < max_depth and blk.get("has_children") and btype in _RECURSIVE_BLOCK_TYPES and blk.get("id"):
                _walk(str(blk["id"]), depth + 1)

    _walk(page_id, 0)
    return units


# ------------------------------------------------------------------
# 플랫폼 식별, v2 근거 URL 매핑, 수치 추출
# ------------------------------------------------------------------


def _detect_platforms_in_text(text: str) -> list[str]:
    lowered = (text or "").lower()
    matched: list[str] = []
    for canonical, _slug, aliases, _domains in _PLATFORM_SPECS:
        if any(alias.lower() in lowered for alias in aliases):
            matched.append(canonical)
    return matched


def _detect_platform_by_url(url: str) -> Optional[str]:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    for canonical, _slug, _aliases, domains in _PLATFORM_SPECS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return canonical
    return None


def _platform_slug(platform: str) -> str:
    for canonical, slug, _aliases, _domains in _PLATFORM_SPECS:
        if canonical == platform:
            return slug
    return re.sub(r"[^a-z0-9]+", "_", platform.lower()).strip("_") or "platform"


def _infer_metric_slug(label: str) -> str:
    lowered = (label or "").lower()
    for slug, keywords in _METRIC_RULES:
        if any(kw in lowered for kw in keywords):
            return slug
    digest = hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:6]
    return f"metric_{digest}"


def _contains_figure(text: str) -> bool:
    return bool(_FIGURE_RE.search(text or ""))


def extract_v2_platform_urls(v2_units: list[DocumentUnit]) -> dict[str, list[str]]:
    """v2 페이지에서만 플랫폼별 근거 URL 목록을 수집한다 (v1 URL은 절대 사용하지 않음)."""
    by_platform: dict[str, list[str]] = {spec[0]: [] for spec in _PLATFORM_SPECS}

    for unit in v2_units:
        if not unit.urls:
            continue
        text_platforms = _detect_platforms_in_text(unit.text)
        heading_platforms = _detect_platforms_in_text(unit.heading_context)
        context_platforms = text_platforms or heading_platforms

        for raw_url in unit.urls:
            url = sanitize_evidence_url(raw_url)
            if not url:
                continue
            by_domain = _detect_platform_by_url(url)
            targets = [by_domain] if by_domain else context_platforms
            for plat in targets:
                if plat in by_platform and url not in by_platform[plat]:
                    by_platform[plat].append(url)
    return by_platform


def _pick_v2_source_url(
    platform: str,
    *,
    is_from_v2: bool,
    unit_urls: tuple[str, ...],
    v2_platform_urls: dict[str, list[str]],
    metric_hint: str,
) -> Optional[str]:
    """v2 페이지에 기재된 근거 URL만 선택한다. 근거가 없으면 None을 반환한다."""
    if is_from_v2 and unit_urls:
        sanitized = [u for raw in unit_urls if (u := sanitize_evidence_url(raw))]
        if sanitized:
            # 같은 줄/행에 해당 플랫폼 도메인 URL이 있으면 최우선
            for u in sanitized:
                if _detect_platform_by_url(u) == platform:
                    return u
            return sanitized[0]

    candidates = v2_platform_urls.get(platform) or []
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # 여러 개일 때는 메트릭 힌트와 URL 경로가 일치하는 항목을 우선 선택, 모호하면 첫 번째 v2 URL
    hint_lower = metric_hint.lower()
    for u in candidates:
        path = urllib.parse.urlsplit(u).path.lower()
        if any(tok and tok in path for tok in hint_lower.split("_")):
            return u
    return candidates[0]


def _extract_from_units(
    units: list[DocumentUnit],
    *,
    is_from_v2: bool,
    v2_platform_urls: dict[str, list[str]],
    source_page_label: str,
    uploader: Optional[str],
) -> list[FactSeedCandidate]:
    extracted: list[FactSeedCandidate] = []

    for unit in units:
        if unit.kind == "heading":
            continue

        if unit.kind == "table_row":
            cells = unit.table_cells
            headers = unit.table_headers
            if not cells:
                continue
            row_platforms = _detect_platforms_in_text(" ".join(cells)) or _detect_platforms_in_text(
                unit.heading_context
            )
            if not row_platforms:
                continue
            # 표의 각 행은 보통 1개 플랫폼에 대응
            platform = row_platforms[0]
            for idx, cell in enumerate(cells):
                header = headers[idx] if idx < len(headers) else ""
                cleaned_cell = _strip_urls(cell)
                if not cleaned_cell or not _contains_figure(cleaned_cell):
                    continue
                # 플랫폼명 셀 자체는 건너뜀
                if cleaned_cell.lower() in {a.lower() for _, _, aliases, _ in _PLATFORM_SPECS for a in aliases}:
                    continue
                metric_label = header or "수치"
                metric_slug = _infer_metric_slug(f"{metric_label} {cleaned_cell}")
                topic_key = f"{_platform_slug(platform)}.{metric_slug}"
                source_url = _pick_v2_source_url(
                    platform,
                    is_from_v2=is_from_v2,
                    unit_urls=unit.urls,
                    v2_platform_urls=v2_platform_urls,
                    metric_hint=metric_slug,
                )
                grade = "원문대조필요" if source_url else "미확인"
                claim = f"[{platform}] {metric_label}: {cleaned_cell}"
                extracted.append(
                    FactSeedCandidate(
                        platform=platform,
                        claim=claim,
                        topic_key=topic_key,
                        value=cleaned_cell,
                        source_url=source_url,
                        grade=grade,
                        excerpt="",
                        uploader=uploader,
                        source_page_label=source_page_label,
                    )
                )
            continue

        # 일반 문단 / 불릿 / 콜아웃 텍스트
        cleaned_text = _strip_urls(unit.text)
        if not cleaned_text or not _contains_figure(cleaned_text):
            continue

        line_platforms = _detect_platforms_in_text(cleaned_text)
        heading_platforms = _detect_platforms_in_text(unit.heading_context)
        platforms = line_platforms or heading_platforms
        if not platforms:
            continue

        # 한 줄에 여러 플랫폼이 나열된 경우(예: "Gumroad 10%, Patreon 8%")는 쉼표/슬래시로 세그먼트 분리 시도
        if len(platforms) > 1:
            segments = [seg.strip() for seg in re.split(r"[,;/·]\s*", cleaned_text) if seg.strip()]
            for seg in segments:
                seg_plats = _detect_platforms_in_text(seg)
                if len(seg_plats) == 1 and _contains_figure(seg):
                    plat = seg_plats[0]
                    metric_slug = _infer_metric_slug(f"{unit.heading_context} {seg}")
                    topic_key = f"{_platform_slug(plat)}.{metric_slug}"
                    val = _extract_value_portion(seg, plat)
                    source_url = _pick_v2_source_url(
                        plat,
                        is_from_v2=is_from_v2,
                        unit_urls=unit.urls,
                        v2_platform_urls=v2_platform_urls,
                        metric_hint=metric_slug,
                    )
                    grade = "원문대조필요" if source_url else "미확인"
                    extracted.append(
                        FactSeedCandidate(
                            platform=plat,
                            claim=f"[{plat}] {seg}",
                            topic_key=topic_key,
                            value=val,
                            source_url=source_url,
                            grade=grade,
                            excerpt="",
                            uploader=uploader,
                            source_page_label=source_page_label,
                        )
                    )
            continue

        plat = platforms[0]
        metric_slug = _infer_metric_slug(f"{unit.heading_context} {cleaned_text}")
        topic_key = f"{_platform_slug(plat)}.{metric_slug}"
        val = _extract_value_portion(cleaned_text, plat)
        source_url = _pick_v2_source_url(
            plat,
            is_from_v2=is_from_v2,
            unit_urls=unit.urls,
            v2_platform_urls=v2_platform_urls,
            metric_hint=metric_slug,
        )
        grade = "원문대조필요" if source_url else "미확인"
        claim = cleaned_text if cleaned_text.startswith(f"[{plat}]") else f"[{plat}] {cleaned_text}"
        extracted.append(
            FactSeedCandidate(
                platform=plat,
                claim=claim,
                topic_key=topic_key,
                value=val,
                source_url=source_url,
                grade=grade,
                excerpt="",
                uploader=uploader,
                source_page_label=source_page_label,
            )
        )

    return extracted


def _extract_value_portion(text: str, platform: str) -> str:
    """'항목: 값' 형태면 콜론 뒤 값을, 그렇지 않으면 원문에서 수치 구절/정리 문자열을 반환한다."""
    if ":" in text:
        right = text.split(":", 1)[1].strip()
        if right and _contains_figure(right):
            return right
    # 플랫폼 접두어만 제거하고 원문 수치 표현 유지
    cleaned = text.strip()
    for canonical, _slug, aliases, _domains in _PLATFORM_SPECS:
        if canonical == platform:
            for alias in sorted(aliases, key=len, reverse=True):
                cleaned = re.sub(rf"^{re.escape(alias)}\s*[:\-—–]?\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or text.strip()


def extract_platform_seed_candidates(
    v1_units: list[DocumentUnit],
    v2_units: list[DocumentUnit],
    *,
    uploader: Optional[str] = "뮤즈",
    v1_label: str = DEFAULT_SEED_V1_PAGE_TITLE,
    v2_label: str = DEFAULT_SEED_V2_PAGE_TITLE,
) -> list[FactSeedCandidate]:
    """v1·v2 두 페이지의 블록 목록에서 플랫폼 5종 수치를 추출하고 v2 근거 URL만 결합한다."""
    v2_platform_urls = extract_v2_platform_urls(v2_units)

    raw_v2 = _extract_from_units(
        v2_units,
        is_from_v2=True,
        v2_platform_urls=v2_platform_urls,
        source_page_label=v2_label,
        uploader=uploader,
    )
    raw_v1 = _extract_from_units(
        v1_units,
        is_from_v2=False,
        v2_platform_urls=v2_platform_urls,
        source_page_label=v1_label,
        uploader=uploader,
    )

    # 중복 제거: v2 우선, 동일 (platform, topic_key, normalized_value)는 1건만 유지
    deduped: list[FactSeedCandidate] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for cand in [*raw_v2, *raw_v1]:
        norm_val = re.sub(r"\s+", " ", cand.value).strip().lower()
        key = (cand.platform, cand.topic_key, norm_val)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        # 방어적 불변조건: 시드 입력은 절대 '확인됨'으로 들어갈 수 없다
        assert cand.grade != "확인됨", "시드 입력은 절대 등급 '확인됨'을 부여할 수 없습니다."
        deduped.append(cand)
    return deduped


def build_seed_page_properties(candidate: FactSeedCandidate) -> dict[str, Any]:
    """FactSeedCandidate 1건을 Notion POST /v1/pages properties 객체로 변환한다."""
    if candidate.grade == "확인됨":
        raise ValueError("등급 상향 금지 위반: 시드 행에는 '확인됨'을 지정할 수 없습니다.")

    props: dict[str, Any] = {
        "주장": {"title": [{"type": "text", "text": {"content": candidate.claim[:2000]}}]},
        "주제키": {"rich_text": [{"type": "text", "text": {"content": candidate.topic_key[:2000]}}]},
        "값": {"rich_text": [{"type": "text", "text": {"content": candidate.value[:2000]}}]},
        "원문 발췌": {"rich_text": [{"type": "text", "text": {"content": candidate.excerpt[:2000]}}]},
        "등급": {"select": {"name": candidate.grade}},
    }
    if candidate.source_url:
        props["근거 URL"] = {"url": candidate.source_url}
    if candidate.uploader:
        props["올린 이"] = {"select": {"name": candidate.uploader}}
    return props


def run_fact_seed(
    client: NotionClient,
    cache: LocalCache,
    *,
    v1_identifier: str = DEFAULT_SEED_V1_PAGE_TITLE,
    v2_identifier: str = DEFAULT_SEED_V2_PAGE_TITLE,
    uploader: Optional[str] = "뮤즈",
    parent_page_id: Optional[str] = None,
    configured_database_id: Optional[str] = None,
    configured_data_source_id: Optional[str] = None,
    parent_page_title: str = PARENT_PAGE_TITLE,
    db_title: str = FACT_LEDGER_DB_TITLE,
    dry_run: bool = False,
) -> FactSeedOutcome:
    """v1·v2 두 노션 페이지를 읽어 플랫폼 수치 시드 행을 '📒 팩트 장부'에 입력한다.

    어느 한 페이지라도 접근할 수 없으면 행을 추측하지 않고 즉시 실패를 보고한다.
    """
    try:
        v1_id, v1_title = resolve_seed_page(client, v1_identifier, label="v1")
        v2_id, v2_title = resolve_seed_page(client, v2_identifier, label="v2")
    except SeedPageAccessError as exc:
        err = f"[시드 입력 실패] 원본 페이지 접근 불가 (추측 생성 금지): {exc}"
        return FactSeedOutcome(
            ok=False,
            v1_page_id=None,
            v2_page_id=None,
            v1_title=v1_identifier,
            v2_title=v2_identifier,
            candidates=[],
            created_count=0,
            skipped_existing_count=0,
            error_message=err,
            summary_text=err,
        )

    try:
        v1_units = read_page_units(client, v1_id)
        v2_units = read_page_units(client, v2_id)
    except NotionAPIError as exc:
        err = f"[시드 입력 실패] 원본 페이지 본문 읽기 오류 (추측 생성 금지): {exc}"
        return FactSeedOutcome(
            ok=False,
            v1_page_id=v1_id,
            v2_page_id=v2_id,
            v1_title=v1_title,
            v2_title=v2_title,
            candidates=[],
            created_count=0,
            skipped_existing_count=0,
            error_message=err,
            summary_text=err,
        )

    candidates = extract_platform_seed_candidates(
        v1_units,
        v2_units,
        uploader=uploader,
        v1_label=v1_title,
        v2_label=v2_title,
    )

    if dry_run:
        lines = [
            f"[팩트 장부 시드 입력 — dry-run]",
            f"v1 페이지: {v1_title} ({v1_id})",
            f"v2 페이지: {v2_title} ({v2_id})",
            f"추출된 시드 후보: {len(candidates)}건 (노션 쓰기 생략)",
        ]
        for c in candidates:
            lines.append(
                f"  - [{c.grade}] {c.topic_key} = {c.value} | URL={c.source_url or '(없음)'}"
            )
        return FactSeedOutcome(
            ok=True,
            v1_page_id=v1_id,
            v2_page_id=v2_id,
            v1_title=v1_title,
            v2_title=v2_title,
            candidates=candidates,
            created_count=0,
            skipped_existing_count=0,
            summary_text="\n".join(lines),
        )

    init_res = ensure_fact_ledger_database(
        client,
        cache,
        parent_page_id=parent_page_id,
        configured_database_id=configured_database_id,
        configured_data_source_id=configured_data_source_id,
        parent_page_title=parent_page_title,
        db_title=db_title,
        create_if_missing=True,
    )
    ds_id = init_res.data_source_id
    existing_pages, _ = client.query_data_source(ds_id)
    existing_keys = {
        (
            r.topic_key.strip(),
            re.sub(r"\s+", " ", r.value).strip().lower(),
        )
        for r in (parse_fact_ledger_row(p) for p in existing_pages if not _is_trashed(p))
    }

    created_count = 0
    skipped_existing = 0
    for cand in candidates:
        dedup_key = (cand.topic_key.strip(), re.sub(r"\s+", " ", cand.value).strip().lower())
        if dedup_key in existing_keys:
            skipped_existing += 1
            continue
        props = build_seed_page_properties(cand)
        client.create_fact_ledger_page(ds_id, props)
        existing_keys.add(dedup_key)
        created_count += 1

    lines = [
        f"[팩트 장부 시드 입력 완료]",
        f"v1 페이지: {v1_title} ({v1_id})",
        f"v2 페이지: {v2_title} ({v2_id})",
        f"대상 DB: {db_title} (data_source_id={ds_id})",
        f"추출 후보: {len(candidates)}건 | 신규 생성: {created_count}건 | 기존 중복 생략: {skipped_existing}건",
    ]
    return FactSeedOutcome(
        ok=True,
        v1_page_id=v1_id,
        v2_page_id=v2_id,
        v1_title=v1_title,
        v2_title=v2_title,
        candidates=candidates,
        created_count=created_count,
        skipped_existing_count=skipped_existing,
        summary_text="\n".join(lines),
    )
