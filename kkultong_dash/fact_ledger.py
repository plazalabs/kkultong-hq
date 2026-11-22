"""P2 팩트 장부('📒 팩트 장부') DB 생성(멱등), 자동 재검증 및 충돌 판정 모듈.

핵심 규칙:
1. '📒 팩트 장부' DB는 '천하제일 꿀통대회' 아래에 멱등 생성한다(이미 존재하면 재생성 금지).
2. 앱은 절대 등급을 '확인됨'으로 올리지 않는다. 내리기와 표시만 수행한다.
3. 기존 DB/페이지(체험단 DB, 경쟁 기록 DB, 게시판, 허브, 정산판)는 절대 수정하지 않는다.
4. 재검증 시 동일 사이트 요청 사이에 충분한 지연(기본 3초)을 두고, robots.txt와 이용약관을 준수한다.
5. 로그인 필요 페이지는 우회하지 않고 '자동확인불가'로 전환하며 재시도 금지 목록에 기록한다.
"""

from __future__ import annotations

import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable, Optional

from . import (
    FACT_LEDGER_ACTORS,
    FACT_LEDGER_DB_TITLE,
    FACT_LEDGER_GRADES,
    FACT_LEDGER_REVALIDATE_TARGET_GRADES,
    FACT_LEDGER_REVALIDATION_RESULTS,
    KST,
    PARENT_PAGE_TITLE,
)
from .cache import LocalCache
from .models import FactAlert, FactLedgerRow
from .notion_client import NotionAPIError, NotionClient, ReadOnlyGuardError
from .parsers import (
    extract_block_plain_text,
    extract_database_title,
    parse_fact_ledger_row,
)
from .publisher import DashboardPublisher

DEFAULT_USER_AGENT = "kkultong-hq-fact-checker/0.2 (+https://github.com/plazalabs/kkultong-hq)"

# P2.5 Zendesk Help Center 공식 공개 JSON API 변환 대상 도메인 및 규칙
# - HTML 페이지는 일부 실행 환경에서 HTTP 403을 반환하지만, 아래 두 도메인의 글(articles/<글번호>)은
#   Zendesk 공식 공개 JSON 경로(/api/v2/help_center/<locale>/articles/<글번호>.json)로 조회 가능하다.
# - 다른 도메인은 기존 HTML fetch 경로를 그대로 유지한다.
ZENDESK_JSON_API_HOSTS: frozenset[str] = frozenset(
    {
        "help.medium.com",
        "support.patreon.com",
    }
)
ZENDESK_DEFAULT_LOCALE = "en-us"
SOURCE_UPDATED_MARKER = "원문 갱신됨"

_ZENDESK_HTML_ARTICLE_PATH_RE = re.compile(
    r"^/hc/(?:(?P<locale>[a-z]{2}(?:-[a-z0-9]{2,4})?)/)?articles/(?P<article_id>\d+)(?:-[^/?#]*)?/?$",
    re.IGNORECASE,
)
_ZENDESK_API_ARTICLE_PATH_RE = re.compile(
    r"^/api/v2/help_center/(?:(?P<locale>[a-z]{2}(?:-[a-z0-9]{2,4})?)/)?articles/(?P<article_id>\d+)\.json$",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")
_PASSWORD_INPUT_RE = re.compile(r"<input\b[^>]*\btype\s*=\s*['\"]?password['\"]?", re.IGNORECASE)

_LOGIN_URL_HOST_PREFIXES = ("login.", "signin.", "accounts.", "nid.")
_LOGIN_URL_PATH_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/auth/login",
    "/account/login",
    "/accounts/login",
    "/sso/login",
    "/nidlogin",
)
_LOGIN_GATE_PHRASES = (
    "로그인이 필요합니다",
    "로그인 후 이용",
    "로그인 후 확인",
    "로그인해 주세요",
    "로그인해주세요",
    "sign in to continue",
    "sign in to view",
    "log in to continue",
    "log in to view",
    "you must be logged in",
    "please sign in to continue",
    "please log in to continue",
    "member-only story",
    "create an account to read",
)


# ------------------------------------------------------------------
# 1) 팩트 장부 DB 스키마 정의 및 멱등 생성
# ------------------------------------------------------------------


def _select_options(names: tuple[str, ...]) -> dict[str, Any]:
    return {"select": {"options": [{"name": name} for name in names]}}


def build_fact_ledger_initial_properties() -> dict[str, Any]:
    """POST /v1/databases 의 initial_data_source.properties 스키마 (자기참조 relation 제외 12개)."""
    return {
        "주장": {"title": {}},
        "주제키": {"rich_text": {}},
        "값": {"rich_text": {}},
        "근거 URL": {"url": {}},
        "원문 발췌": {"rich_text": {}},
        "확인 시각": {"date": {}},
        "확인자": _select_options(FACT_LEDGER_ACTORS),
        "등급": _select_options(FACT_LEDGER_GRADES),
        "자동 재검증 시각": {"date": {}},
        "자동 재검증 결과": _select_options(FACT_LEDGER_REVALIDATION_RESULTS),
        "올린 이": _select_options(FACT_LEDGER_ACTORS),
        "정정한 이": _select_options(FACT_LEDGER_ACTORS),
    }


def build_fact_ledger_relation_properties(data_source_id: str) -> dict[str, Any]:
    """PATCH /v1/data_sources/{id} 로 추가하는 자기참조 relation 2개 (충돌, 대체됨).

    Notion API 2025-09-03 / 2026-03-11 공식 문서 기준:
    relation 쓰기 요청에는 database_id가 아니라 data_source_id만 지정한다.
    """
    return {
        "충돌": {
            "relation": {
                "data_source_id": data_source_id,
                "type": "single_property",
                "single_property": {},
            }
        },
        "대체됨": {
            "relation": {
                "data_source_id": data_source_id,
                "type": "single_property",
                "single_property": {},
            }
        },
    }


@dataclass(frozen=True)
class FactLedgerInitResult:
    parent_page_id: str
    database_id: str
    data_source_id: str
    created: bool
    relations_patched: bool
    db_title: str = FACT_LEDGER_DB_TITLE


def _is_trashed(obj: dict[str, Any]) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


def _extract_primary_data_source_id(client: NotionClient, db_obj: dict[str, Any]) -> str:
    data_sources = db_obj.get("data_sources")
    if isinstance(data_sources, list) and data_sources:
        first = data_sources[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    db_id = str(db_obj.get("id") or "")
    if not db_id:
        raise NotionAPIError("팩트 장부 DB 응답에 id가 없습니다.")
    refreshed = client.retrieve_database(db_id)
    ref_sources = refreshed.get("data_sources")
    if isinstance(ref_sources, list) and ref_sources:
        first = ref_sources[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    raise NotionAPIError(
        f"팩트 장부 DB({db_id})에서 data_source_id를 찾을 수 없습니다."
    )


def _ensure_self_relations(client: NotionClient, data_source_id: str) -> bool:
    """'충돌', '대체됨' 자기참조 relation 속성이 누락되어 있을 때만 추가한다."""
    ds = client.retrieve_data_source(data_source_id)
    props = ds.get("properties") if isinstance(ds.get("properties"), dict) else {}
    missing_keys = [k for k in ("충돌", "대체됨") if k not in props]
    if not missing_keys:
        return False
    all_rels = build_fact_ledger_relation_properties(data_source_id)
    patch_props = {k: all_rels[k] for k in missing_keys}
    client.update_fact_ledger_schema(data_source_id, patch_props)
    return True


def _ensure_revalidation_result_options(
    client: NotionClient,
    data_source_id: str,
    required_options: tuple[str, ...] = FACT_LEDGER_REVALIDATION_RESULTS,
) -> bool:
    """'자동 재검증 결과' select 속성에 누락된 옵션('확인 불가' 등)이 있으면 기존 옵션을 보존하며 추가한다."""
    ds = client.retrieve_data_source(data_source_id)
    props = ds.get("properties") if isinstance(ds.get("properties"), dict) else {}
    prop = props.get("자동 재검증 결과") if isinstance(props.get("자동 재검증 결과"), dict) else {}
    select_obj = prop.get("select") if isinstance(prop.get("select"), dict) else {}
    raw_options = select_obj.get("options") if isinstance(select_obj.get("options"), list) else []

    existing_options: list[dict[str, Any]] = [
        dict(opt) for opt in raw_options if isinstance(opt, dict) and opt.get("name")
    ]
    existing_names = {str(opt["name"]) for opt in existing_options}
    missing_names: list[str] = []
    for name in required_options:
        if name and name not in existing_names and name not in missing_names:
            missing_names.append(name)

    if not missing_names:
        return False

    merged_options = [*existing_options, *[{"name": name} for name in missing_names]]
    client.update_fact_ledger_schema(
        data_source_id,
        {"자동 재검증 결과": {"select": {"options": merged_options}}},
    )
    return True


def ensure_fact_ledger_database(
    client: NotionClient,
    cache: LocalCache,
    *,
    parent_page_id: Optional[str] = None,
    configured_database_id: Optional[str] = None,
    configured_data_source_id: Optional[str] = None,
    parent_page_title: str = PARENT_PAGE_TITLE,
    db_title: str = FACT_LEDGER_DB_TITLE,
    create_if_missing: bool = True,
) -> FactLedgerInitResult:
    """'천하제일 꿀통대회' 아래에 '📒 팩트 장부' DB를 멱등하게 조회하거나 최초 1회 생성한다."""
    if db_title != FACT_LEDGER_DB_TITLE:
        raise ReadOnlyGuardError(
            f"허용되지 않은 팩트 장부 DB 제목입니다: {db_title!r} (허용: {FACT_LEDGER_DB_TITLE!r})"
        )

    publisher = DashboardPublisher(
        client,
        cache,
        parent_page_id=parent_page_id,
        parent_page_title=parent_page_title,
    )
    resolved_parent_id = publisher.resolve_parent_page_id()

    # 1) 명시적 데이터 소스 ID 설정 시 검증
    if configured_data_source_id:
        ds = client.retrieve_data_source(configured_data_source_id)
        if _is_trashed(ds):
            raise NotionAPIError(
                f"NOTION_FACT_LEDGER_DATA_SOURCE_ID({configured_data_source_id})가 휴지통 상태입니다."
            )
        actual_title = extract_database_title(ds)
        if actual_title and actual_title != db_title:
            raise ReadOnlyGuardError(
                f"대상 데이터 소스 제목({actual_title!r})이 '{db_title}'과 달라 쓰기를 차단했습니다."
            )
        ds_id = str(ds["id"])
        parent_obj = ds.get("parent") if isinstance(ds.get("parent"), dict) else {}
        db_id = str(parent_obj.get("database_id") or configured_database_id or "")
        client.register_fact_ledger_data_source(ds_id)
        cache.set_state("fact_ledger_data_source_id", ds_id)
        if db_id:
            cache.set_state("fact_ledger_database_id", db_id)
        patched = _ensure_self_relations(client, ds_id) if create_if_missing else False
        return FactLedgerInitResult(
            parent_page_id=resolved_parent_id,
            database_id=db_id,
            data_source_id=ds_id,
            created=False,
            relations_patched=patched,
        )

    # 2) 명시적 데이터베이스 ID 설정 시 검증
    if configured_database_id:
        db = client.retrieve_database(configured_database_id)
        if _is_trashed(db):
            raise NotionAPIError(
                f"NOTION_FACT_LEDGER_DATABASE_ID({configured_database_id})가 휴지통 상태입니다."
            )
        actual_title = extract_database_title(db)
        if actual_title != db_title:
            raise ReadOnlyGuardError(
                f"대상 DB 제목({actual_title!r})이 '{db_title}'과 달라 쓰기를 차단했습니다."
            )
        db_id = str(db["id"])
        ds_id = _extract_primary_data_source_id(client, db)
        client.register_fact_ledger_data_source(ds_id)
        cache.set_state("fact_ledger_database_id", db_id)
        cache.set_state("fact_ledger_data_source_id", ds_id)
        patched = _ensure_self_relations(client, ds_id) if create_if_missing else False
        return FactLedgerInitResult(
            parent_page_id=resolved_parent_id,
            database_id=db_id,
            data_source_id=ds_id,
            created=False,
            relations_patched=patched,
        )

    # 3) 로컬 SQLite 캐시 ID 검증
    cached_db_id = cache.get_state("fact_ledger_database_id")
    if cached_db_id:
        try:
            db = client.retrieve_database(cached_db_id)
            parent_obj = db.get("parent") if isinstance(db.get("parent"), dict) else {}
            same_parent = (
                parent_obj.get("type") == "page_id"
                and NotionClient._canonical_id(str(parent_obj.get("page_id") or ""))
                == NotionClient._canonical_id(resolved_parent_id)
            )
            if not _is_trashed(db) and extract_database_title(db) == db_title and same_parent:
                db_id = str(db["id"])
                ds_id = _extract_primary_data_source_id(client, db)
                client.register_fact_ledger_data_source(ds_id)
                cache.set_state("fact_ledger_data_source_id", ds_id)
                patched = _ensure_self_relations(client, ds_id) if create_if_missing else False
                return FactLedgerInitResult(
                    parent_page_id=resolved_parent_id,
                    database_id=db_id,
                    data_source_id=ds_id,
                    created=False,
                    relations_patched=patched,
                )
        except (NotionAPIError, ReadOnlyGuardError):
            pass

    # 4) 부모 페이지('천하제일 꿀통대회') 1단계 하위 child_database 직접 스캔
    parent_children = client.list_block_children(resolved_parent_id)
    matching_dbs = [
        b
        for b in parent_children
        if not _is_trashed(b)
        and b.get("type") == "child_database"
        and extract_block_plain_text(b).strip() == db_title
    ]
    if len(matching_dbs) == 1:
        db_id = str(matching_dbs[0]["id"])
        db = client.retrieve_database(db_id)
        ds_id = _extract_primary_data_source_id(client, db)
        client.register_fact_ledger_data_source(ds_id)
        cache.set_state("fact_ledger_database_id", db_id)
        cache.set_state("fact_ledger_data_source_id", ds_id)
        patched = _ensure_self_relations(client, ds_id) if create_if_missing else False
        return FactLedgerInitResult(
            parent_page_id=resolved_parent_id,
            database_id=db_id,
            data_source_id=ds_id,
            created=False,
            relations_patched=patched,
        )
    if len(matching_dbs) > 1:
        ids = ", ".join(str(b.get("id")) for b in matching_dbs)
        raise NotionAPIError(
            f"'{parent_page_title}' 아래에 '{db_title}' DB가 {len(matching_dbs)}개 중복 존재합니다(ID: {ids}). "
            "중복 DB를 정리하거나 .env에 NOTION_FACT_LEDGER_DATABASE_ID를 명시하세요."
        )

    if not create_if_missing:
        raise NotionAPIError(
            f"'{parent_page_title}' 아래에서 '{db_title}' DB를 찾을 수 없습니다. "
            "먼저 './kkultong-dash fact-init'으로 DB를 생성하거나 NOTION_FACT_LEDGER_DATABASE_ID를 지정하세요."
        )

    # 5) 없을 때만 최초 1회 생성 + 자기참조 relation(충돌, 대체됨) 추가
    created_db = client.create_fact_ledger_database(
        resolved_parent_id,
        build_fact_ledger_initial_properties(),
        title=db_title,
    )
    db_id = str(created_db["id"])
    ds_id = _extract_primary_data_source_id(client, created_db)
    client.register_fact_ledger_data_source(ds_id)
    client.update_fact_ledger_schema(ds_id, build_fact_ledger_relation_properties(ds_id))

    cache.set_state("fact_ledger_database_id", db_id)
    cache.set_state("fact_ledger_data_source_id", ds_id)
    return FactLedgerInitResult(
        parent_page_id=resolved_parent_id,
        database_id=db_id,
        data_source_id=ds_id,
        created=True,
        relations_patched=True,
    )


# ------------------------------------------------------------------
# 2) 예의 바른 웹 페처 (호스트별 지연 + robots.txt 준수 + 로그인 우회 금지)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RawWebResponse:
    """테스트 픽스처 주입용 경량 웹 응답."""

    status: int
    final_url: str
    headers: dict[str, str]
    body: str
    error: Optional[str] = None


WebTransportFn = Callable[[str, dict[str, str], float], RawWebResponse]


@dataclass(frozen=True)
class WebFetchResult:
    """외부 근거 URL 조회 및 전처리 결과."""

    kind: str  # "ok" | "login_required" | "open_failed" | "robots_blocked"
    requested_url: str
    final_url: str
    status_code: Optional[int] = None
    visible_text: str = ""
    raw_body: str = ""
    has_login_wall: bool = False
    detail: str = ""
    failure_category: Optional[str] = None  # "url_error" | "http_error" | "network_error" | "robots_blocked"
    fetch_mode: str = "html"  # "html" | "zendesk_json"
    article_updated_at: Optional[str] = None


@dataclass(frozen=True)
class ZendeskArticlePayload:
    """Zendesk Help Center 공개 JSON API 응답에서 추출한 글 본문 및 메타데이터."""

    title: str
    body_html: str
    visible_text: str
    updated_at: Optional[str] = None


def convert_to_zendesk_article_api_url(url: str) -> Optional[str]:
    """대상 도메인(help.medium.com, support.patreon.com)의 글 URL을 Zendesk 공개 JSON API 주소로 변환한다.

    변환 규칙:
    - 대상 호스트: ZENDESK_JSON_API_HOSTS ('help.medium.com', 'support.patreon.com')
    - HTML 글 경로 '/hc/<locale>/articles/<글번호>[-<슬러그>]' 또는 '/hc/articles/<글번호>[-<슬러그>]'
      -> 'https://<host>/api/v2/help_center/<locale>/articles/<글번호>.json' (locale 생략 시 'en-us')
    - 이미 '/api/v2/help_center/<locale>/articles/<글번호>.json' 형태인 경우도 정규화된 API 주소 반환
    - 그 외 도메인이나 비-글(article) 경로, 글번호 뒤에 비정상 꼬리가 붙은 URL은 None 반환 (기존 HTML 경로 유지)
    """
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ZENDESK_JSON_API_HOSTS:
        return None

    path = parsed.path or ""
    match = _ZENDESK_HTML_ARTICLE_PATH_RE.match(path) or _ZENDESK_API_ARTICLE_PATH_RE.match(path)
    if not match:
        return None

    locale = (match.group("locale") or ZENDESK_DEFAULT_LOCALE).lower()
    article_id = match.group("article_id")
    return f"https://{host}/api/v2/help_center/{locale}/articles/{article_id}.json"


class _VisibleTextExtractor(HTMLParser):
    """HTML에서 <script>/<style>/<noscript>를 제외한 가시 텍스트만 추출한다."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif t in {"p", "br", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t in {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        return html.unescape("".join(self._parts))


def extract_visible_text(raw_body: str) -> str:
    """HTML(또는 평문) 본문에서 가시 텍스트를 추출한다."""
    if not raw_body:
        return ""
    if "<" not in raw_body and ">" not in raw_body:
        return html.unescape(raw_body)
    parser = _VisibleTextExtractor()
    try:
        parser.feed(raw_body)
        parser.close()
        return parser.get_text()
    except Exception:
        return html.unescape(raw_body)


def parse_zendesk_article_json(raw_json: str) -> ZendeskArticlePayload:
    """Zendesk Help Center JSON 응답의 article.body(HTML)를 텍스트로 변환하고 article.updated_at을 추출한다."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 디코딩 오류: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("최상위 JSON 응답이 객체(dict)가 아닙니다")

    article = payload.get("article")
    if not isinstance(article, dict):
        raise ValueError("JSON 응답에 'article' 객체가 없습니다")

    raw_body = article.get("body")
    if raw_body is None:
        body_html = ""
    elif isinstance(raw_body, str):
        body_html = raw_body
    else:
        body_html = str(raw_body)

    raw_title = article.get("title")
    title = raw_title if isinstance(raw_title, str) else ""

    raw_updated_at = article.get("updated_at")
    updated_at = (
        raw_updated_at.strip()
        if isinstance(raw_updated_at, str) and raw_updated_at.strip()
        else None
    )

    visible_text = extract_visible_text(body_html)
    return ZendeskArticlePayload(
        title=title,
        body_html=body_html,
        visible_text=visible_text,
        updated_at=updated_at,
    )


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def is_login_url(url: str) -> bool:
    """리다이렉트된 URL이 로그인/인증 페이지인지 판별한다."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if any(host.startswith(prefix) for prefix in _LOGIN_URL_HOST_PREFIXES):
        return True
    if any(marker in path for marker in _LOGIN_URL_PATH_MARKERS):
        return True
    return False


def normalize_fetch_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """웹 요청 전 URL을 검증하고 비ASCII 문자를 안전하게 인코딩한다.

    반환값: (정규화된 ASCII URL 또는 None, 실패 사유 또는 None)
    """
    raw = (url or "").strip()
    if not raw:
        return None, "빈 URL"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return None, "URL에 제어 문자가 포함되어 있습니다"

    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        return None, f"URL 파싱 실패: {exc}"

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return None, "유효하지 않은 URL 형식"

    try:
        port = parsed.port
    except ValueError as exc:
        return None, f"유효하지 않은 포트: {exc}"

    host = parsed.hostname
    if any(ch.isspace() for ch in host):
        return None, "호스트에 공백이 포함되어 있습니다"
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        return None, f"호스트 인코딩 실패: {exc}"

    host_part = f"[{ascii_host}]" if ":" in ascii_host and not ascii_host.startswith("[") else ascii_host

    userinfo = ""
    if parsed.username is not None:
        user_enc = urllib.parse.quote(parsed.username, safe="-._~!$&'()*+,;=")
        if parsed.password is not None:
            pass_enc = urllib.parse.quote(parsed.password, safe="-._~!$&'()*+,;=")
            userinfo = f"{user_enc}:{pass_enc}@"
        else:
            userinfo = f"{user_enc}@"

    netloc = f"{userinfo}{host_part}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = urllib.parse.quote(parsed.path, safe="/:@!$&'()*+,;=-._~%")
    query = urllib.parse.quote(parsed.query, safe="/?:@!$&'()*+,;=-._~%")
    fragment = urllib.parse.quote(parsed.fragment, safe="/?:@!$&'()*+,;=-._~%")

    normalized = urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        return None, f"URL ASCII 인코딩 실패: {exc}"
    return normalized, None


def has_login_wall_signal(raw_body: str, visible_text: str) -> bool:
    """페이지 본문이 로그인 벽(비밀번호 입력 폼 또는 명시적 로그인 안내)인지 판별한다."""
    if raw_body and _PASSWORD_INPUT_RE.search(raw_body):
        return True
    lowered = normalize_whitespace(visible_text).lower()
    return any(phrase in lowered for phrase in _LOGIN_GATE_PHRASES)


def _default_web_transport(url: str, headers: dict[str, str], timeout: float) -> RawWebResponse:
    normalized_url, norm_err = normalize_fetch_url(url)
    if norm_err or not normalized_url:
        return RawWebResponse(
            status=0,
            final_url=url,
            headers={},
            body="",
            error=f"URL 정규화 실패: {norm_err or '유효하지 않은 URL'}",
        )
    try:
        req = urllib.request.Request(url=normalized_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read().decode("utf-8", errors="replace")
            return RawWebResponse(
                status=resp.status,
                final_url=resp.geturl() or normalized_url,
                headers=resp_headers,
                body=raw,
            )
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return RawWebResponse(
            status=exc.code,
            final_url=exc.geturl() or normalized_url,
            headers=resp_headers,
            body=raw,
        )
    except urllib.error.URLError as exc:
        return RawWebResponse(
            status=0,
            final_url=normalized_url,
            headers={},
            body="",
            error=f"네트워크 오류: {exc.reason}",
        )
    except TimeoutError:
        return RawWebResponse(
            status=0,
            final_url=normalized_url,
            headers={},
            body="",
            error=f"요청 타임아웃({timeout}초 초과)",
        )
    except (UnicodeError, ValueError, http.client.HTTPException, OSError) as exc:
        return RawWebResponse(
            status=0,
            final_url=normalized_url,
            headers={},
            body="",
            error=f"요청 처리 오류({type(exc).__name__}): {exc}",
        )


class PoliteWebFetcher:
    """동일 호스트 간 지연 + robots.txt 준수 + 로그인 우회 금지를 지키는 경량 HTTP 조회기."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        host_delay_sec: float = 3.0,
        timeout_sec: float = 20.0,
        transport: Optional[WebTransportFn] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.user_agent = user_agent
        self.host_delay_sec = max(0.0, float(host_delay_sec))
        self.timeout_sec = max(1.0, float(timeout_sec))
        self._transport = transport or _default_web_transport
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_host_at: dict[str, float] = {}
        self._robots_cache: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def _pace_host(self, host: str, *, extra_crawl_delay: Optional[float] = None) -> None:
        required_delay = self.host_delay_sec
        if extra_crawl_delay is not None and extra_crawl_delay > required_delay:
            required_delay = float(extra_crawl_delay)
        if required_delay <= 0.0:
            self._last_host_at[host] = self._monotonic()
            return
        now = self._monotonic()
        last = self._last_host_at.get(host)
        if last is not None:
            remaining = required_delay - (now - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_host_at[host] = self._monotonic()

    def _get_robots_parser(self, scheme: str, netloc: str) -> Optional[urllib.robotparser.RobotFileParser]:
        origin = f"{scheme}://{netloc}"
        if origin in self._robots_cache:
            return self._robots_cache[origin]

        robots_url = f"{origin}/robots.txt"
        headers = {"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.8"}
        try:
            resp = self._transport(robots_url, headers, self.timeout_sec)
        except Exception:
            self._robots_cache[origin] = None
            return None

        if resp.error or resp.status == 0:
            # robots.txt 조회 자체가 네트워크 오류이면 보수적으로 None 처리 (본 요청에서 오류 기록)
            self._robots_cache[origin] = None
            return None

        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        if 200 <= resp.status < 300:
            rp.parse(resp.body.splitlines())
        elif resp.status in {401, 403}:
            rp.disallow_all = True
        else:
            # RFC 9309: 404/410 등 미존재 시 허용
            rp.allow_all = True

        self._robots_cache[origin] = rp
        return rp

    def fetch(self, url: str) -> WebFetchResult:
        """단일 근거 URL을 검증·정규화한 뒤 robots.txt와 호스트 지연을 지켜 읽기 전용으로 조회한다.

        help.medium.com, support.patreon.com의 글 URL은 Zendesk 공식 공개 JSON API 주소로 변환해
        article.body(HTML)와 article.updated_at을 추출하며, 그 외 도메인은 기존 HTML 경로를 유지한다.
        """
        normalized_url, norm_err = normalize_fetch_url(url)
        if norm_err or not normalized_url:
            return WebFetchResult(
                kind="open_failed",
                requested_url=url,
                final_url=url,
                detail=norm_err or "유효하지 않은 URL 형식",
                failure_category="url_error",
            )

        zendesk_api_url = convert_to_zendesk_article_api_url(normalized_url)
        target_url = zendesk_api_url or normalized_url
        fetch_mode = "zendesk_json" if zendesk_api_url else "html"

        parsed = urllib.parse.urlsplit(target_url)
        host = (parsed.hostname or parsed.netloc).lower()
        rp = self._get_robots_parser(parsed.scheme, parsed.netloc)
        crawl_delay: Optional[float] = None
        if rp is not None:
            if not rp.can_fetch(self.user_agent, target_url):
                return WebFetchResult(
                    kind="robots_blocked",
                    requested_url=url,
                    final_url=target_url,
                    detail="robots.txt 정책에 따라 자동 접근을 중단함",
                    failure_category="robots_blocked",
                    fetch_mode=fetch_mode,
                )
            cd = rp.crawl_delay(self.user_agent)
            if isinstance(cd, (int, float)) and cd > 0:
                crawl_delay = float(cd)

        self._pace_host(host, extra_crawl_delay=crawl_delay)
        accept_header = (
            "application/json,text/plain;q=0.8,*/*;q=0.5"
            if fetch_mode == "zendesk_json"
            else "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8"
        )
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept_header,
        }
        try:
            resp = self._transport(target_url, headers, self.timeout_sec)
        except Exception as exc:
            return WebFetchResult(
                kind="open_failed",
                requested_url=url,
                final_url=target_url,
                detail=f"요청 중 예외 발생({type(exc).__name__}): {exc}",
                failure_category="network_error",
                fetch_mode=fetch_mode,
            )

        if resp.error or resp.status == 0:
            err_text = resp.error or "연결 실패"
            category = "url_error" if err_text.startswith("URL 정규화 실패:") else "network_error"
            return WebFetchResult(
                kind="open_failed",
                requested_url=url,
                final_url=resp.final_url or target_url,
                status_code=resp.status or None,
                detail=err_text,
                failure_category=category,
                fetch_mode=fetch_mode,
            )

        final_url = resp.final_url or target_url
        visible = extract_visible_text(resp.body)
        login_wall = has_login_wall_signal(resp.body, visible)

        if resp.status == 401:
            return WebFetchResult(
                kind="login_required",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                detail="HTTP 401 인증 필요",
                fetch_mode=fetch_mode,
            )

        if final_url != target_url and is_login_url(final_url):
            return WebFetchResult(
                kind="login_required",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                detail=f"로그인 페이지로 리다이렉트됨 ({final_url})",
                fetch_mode=fetch_mode,
            )

        if resp.status == 403:
            if "www-authenticate" in resp.headers or login_wall:
                return WebFetchResult(
                    kind="login_required",
                    requested_url=url,
                    final_url=final_url,
                    status_code=resp.status,
                    detail="HTTP 403 로그인/인증 필요",
                    fetch_mode=fetch_mode,
                )
            return WebFetchResult(
                kind="open_failed",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                detail="HTTP 403 접근 거부",
                failure_category="http_error",
                fetch_mode=fetch_mode,
            )

        if resp.status >= 400:
            return WebFetchResult(
                kind="open_failed",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                detail=f"HTTP {resp.status} 오류",
                failure_category="http_error",
                fetch_mode=fetch_mode,
            )

        if is_login_url(target_url) and login_wall:
            return WebFetchResult(
                kind="login_required",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                detail="로그인 전용 URL",
                fetch_mode=fetch_mode,
            )

        if fetch_mode == "zendesk_json":
            try:
                article = parse_zendesk_article_json(resp.body)
            except ValueError as exc:
                return WebFetchResult(
                    kind="open_failed",
                    requested_url=url,
                    final_url=final_url,
                    status_code=resp.status,
                    detail=f"Zendesk JSON 응답 파싱 실패: {exc}",
                    failure_category="network_error",
                    fetch_mode=fetch_mode,
                )
            article_login_wall = has_login_wall_signal(article.body_html, article.visible_text)
            return WebFetchResult(
                kind="ok",
                requested_url=url,
                final_url=final_url,
                status_code=resp.status,
                visible_text=article.visible_text,
                raw_body=article.body_html,
                has_login_wall=article_login_wall,
                detail="OK (Zendesk JSON API)",
                fetch_mode=fetch_mode,
                article_updated_at=article.updated_at,
            )

        return WebFetchResult(
            kind="ok",
            requested_url=url,
            final_url=final_url,
            status_code=resp.status,
            visible_text=visible,
            raw_body=resp.body,
            has_login_wall=login_wall,
            detail="OK",
            fetch_mode=fetch_mode,
        )


# ------------------------------------------------------------------
# 3) 재검증 판정 및 충돌 판정 (순수 함수)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RevalidationDecision:
    """단일 행의 근거 URL 자동 재검증 판정 결과."""

    page_id: str
    old_grade: Optional[str]
    new_grade: Optional[str]
    result: str  # "발췌 그대로" | "발췌 사라짐" | "확인 불가" | "열림 실패 (...)" | "로그인 필요"
    add_to_fact_alerts: bool
    add_to_skip_list: bool
    detail: str = ""
    source_updated: bool = False
    source_update_marker: Optional[str] = None  # 갱신 감지 시 "원문 갱신됨" (자동 재검증 결과와 별개)
    previous_article_updated_at: Optional[str] = None
    current_article_updated_at: Optional[str] = None


@dataclass(frozen=True)
class ConflictUpdate:
    """동일 주제키 + 다른 값 + 둘 다 '확인됨' 충돌 판정 결과."""

    page_id: str
    claim: str
    topic_key: str
    value: str
    source_url: Optional[str]
    old_grade: str
    new_grade: str  # "불일치"
    conflict_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConflictCandidate:
    """등급과 무관하게 동일 주제키에 서로 다른 값이 공존하는 충돌 후보 (표시 전용, 등급 불변)."""

    page_id: str
    claim: str
    topic_key: str
    value: str
    source_url: Optional[str]
    grade: Optional[str]
    peer_values: tuple[str, ...] = ()


def is_open_failure_result(result: str) -> bool:
    """자동 재검증 결과가 열림 실패 계열인지 판별한다."""
    return result == "페이지 열림 실패" or result.startswith("열림 실패 (")


def format_open_failure_result(fetch: Optional[WebFetchResult]) -> str:
    """열림 실패 원인을 구분해 행에 표시할 결과 문자열로 변환한다."""
    if fetch is None:
        return "열림 실패 (주소 오류)"
    if fetch.kind == "robots_blocked" or fetch.failure_category == "robots_blocked":
        return "열림 실패 (robots.txt 차단)"
    if fetch.failure_category == "url_error":
        return "열림 실패 (주소 오류)"
    if fetch.failure_category == "http_error" or (
        fetch.status_code is not None and fetch.status_code >= 400
    ):
        if fetch.status_code is not None and fetch.status_code > 0:
            return f"열림 실패 (HTTP {fetch.status_code})"
        return "열림 실패 (HTTP 오류)"
    return "열림 실패 (네트워크 오류)"


def excerpt_exists_verbatim(excerpt: str, *, visible_text: str, raw_body: str = "") -> bool:
    """원문 발췌가 페이지 본문에 그대로 남아 있는지 판별한다."""
    cleaned = excerpt.strip()
    if not cleaned:
        return False
    if cleaned in visible_text:
        return True
    norm_excerpt = normalize_whitespace(cleaned)
    if norm_excerpt and norm_excerpt in normalize_whitespace(visible_text):
        return True
    if raw_body and cleaned in raw_body:
        return True
    return False


def assert_no_grade_upgrade(old_grade: Optional[str], new_grade: Optional[str]) -> None:
    """절대 규칙: 앱은 절대 등급을 '확인됨'으로 상향하지 않는다."""
    if new_grade == "확인됨" and old_grade != "확인됨":
        raise ReadOnlyGuardError(
            f"등급 상향 금지 위반: {old_grade!r} → '확인됨' 전이는 허용되지 않습니다."
        )


def judge_revalidation(
    row: FactLedgerRow,
    fetch: Optional[WebFetchResult],
    *,
    already_in_skip_list: bool = False,
    previous_article_updated_at: Optional[str] = None,
) -> RevalidationDecision:
    """단일 팩트 장부 행과 웹 조회 결과로부터 재검증 결과 및 등급 변동을 판정한다."""
    old_grade = row.grade

    if already_in_skip_list:
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade="자동확인불가",
            result="로그인 필요",
            add_to_fact_alerts=False,
            add_to_skip_list=True,
            detail="재시도 금지 목록(로그인 필요)에 등록된 URL — 재요청 생략",
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    if fetch is None:
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade=old_grade,
            result=format_open_failure_result(None),
            add_to_fact_alerts=False,
            add_to_skip_list=False,
            detail="근거 URL이 비어 있습니다.",
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    if fetch.kind == "login_required":
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade="자동확인불가",
            result="로그인 필요",
            add_to_fact_alerts=False,
            add_to_skip_list=True,
            detail=fetch.detail or "로그인 필요",
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    if fetch.kind in {"open_failed", "robots_blocked"}:
        result_label = format_open_failure_result(fetch)
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade=old_grade,
            result=result_label,
            add_to_fact_alerts=False,
            add_to_skip_list=False,
            detail=fetch.detail or result_label,
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    # fetch.kind == "ok"
    # P2.5 문서 갱신 감시: JSON 응답의 article.updated_at이 직전 실행 값과 다르면
    # 자동 재검증 결과('발췌 그대로'/'발췌 사라짐'/'확인 불가' 등)와 별개로 '원문 갱신됨' 표시를 남긴다.
    prev_updated = (previous_article_updated_at or "").strip() or None
    curr_updated = (fetch.article_updated_at or "").strip() or None
    source_updated = bool(prev_updated and curr_updated and prev_updated != curr_updated)
    source_update_marker = SOURCE_UPDATED_MARKER if source_updated else None

    # 수정 1: 원문 발췌가 비어 있으면 찾을 문장이 없으므로 '발췌 사라짐'이 아닌 '확인 불가'로 기록 (등급 불변)
    if not (row.excerpt or "").strip():
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade=old_grade,
            result="확인 불가",
            add_to_fact_alerts=False,
            add_to_skip_list=False,
            detail="원문 발췌가 비어 있어 자동 대조 불가",
            source_updated=source_updated,
            source_update_marker=source_update_marker,
            previous_article_updated_at=prev_updated,
            current_article_updated_at=curr_updated,
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    if excerpt_exists_verbatim(row.excerpt, visible_text=fetch.visible_text, raw_body=fetch.raw_body):
        # 발췌 그대로: 등급 유지 (원문대조필요 -> 확인됨 상향 절대 금지!)
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade=old_grade,
            result="발췌 그대로",
            add_to_fact_alerts=False,
            add_to_skip_list=False,
            detail="원문 발췌 일치",
            source_updated=source_updated,
            source_update_marker=source_update_marker,
            previous_article_updated_at=prev_updated,
            current_article_updated_at=curr_updated,
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    # 발췌가 안 보이는 상황에서 페이지 자체가 로그인 벽인 경우 -> '로그인 필요' / '자동확인불가'
    if fetch.has_login_wall:
        decision = RevalidationDecision(
            page_id=row.page_id,
            old_grade=old_grade,
            new_grade="자동확인불가",
            result="로그인 필요",
            add_to_fact_alerts=False,
            add_to_skip_list=True,
            detail="본문에 로그인 벽 감지 — 우회 금지 및 재시도 제외",
            source_updated=source_updated,
            source_update_marker=source_update_marker,
            previous_article_updated_at=prev_updated,
            current_article_updated_at=curr_updated,
        )
        assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
        return decision

    # 발췌 사라짐: 확인됨이었다면 원문대조필요로 하향 + 대시보드 섹션 A '팩트 경보' 추가
    if old_grade == "확인됨":
        new_grade: Optional[str] = "원문대조필요"
        alert = True
    else:
        new_grade = old_grade
        alert = False

    decision = RevalidationDecision(
        page_id=row.page_id,
        old_grade=old_grade,
        new_grade=new_grade,
        result="발췌 사라짐",
        add_to_fact_alerts=alert,
        add_to_skip_list=False,
        detail="페이지 본문에서 원문 발췌를 찾을 수 없음",
        source_updated=source_updated,
        source_update_marker=source_update_marker,
        previous_article_updated_at=prev_updated,
        current_article_updated_at=curr_updated,
    )
    assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
    return decision


def detect_fact_conflicts(rows: list[FactLedgerRow]) -> list[ConflictUpdate]:
    """같은 주제키에 값이 다른 행이 2건 이상이고 둘 다 등급 '확인됨'이면 모두 '불일치'로 하향하고 충돌 relation으로 연결한다."""
    by_topic: dict[str, list[FactLedgerRow]] = defaultdict(list)
    for row in rows:
        key = row.topic_key.strip()
        if not key:
            continue
        if row.grade == "확인됨":
            by_topic[key].append(row)

    updates: list[ConflictUpdate] = []
    for topic_key, confirmed_rows in sorted(by_topic.items()):
        if len(confirmed_rows) < 2:
            continue
        distinct_values = {r.value.strip() for r in confirmed_rows}
        if len(distinct_values) < 2:
            continue

        for row in confirmed_rows:
            row_val = row.value.strip()
            peer_ids = [
                other.page_id
                for other in confirmed_rows
                if other.page_id != row.page_id and other.value.strip() != row_val
            ]
            merged: list[str] = []
            seen: set[str] = set()
            for pid in (*row.conflict_ids, *peer_ids):
                if pid and pid != row.page_id and pid not in seen:
                    seen.add(pid)
                    merged.append(pid)
            updates.append(
                ConflictUpdate(
                    page_id=row.page_id,
                    claim=row.claim,
                    topic_key=topic_key,
                    value=row.value,
                    source_url=row.source_url,
                    old_grade="확인됨",
                    new_grade="불일치",
                    conflict_ids=tuple(merged),
                )
            )
    return updates


def detect_fact_conflict_candidates(rows: list[FactLedgerRow]) -> list[ConflictCandidate]:
    """등급과 무관하게 같은 주제키에 서로 다른 값이 공존하는 충돌 후보를 탐지한다.

    - 표시 전용이며 등급을 강등하지 않는다.
    - 이미 '불일치'로 판정됐거나 '대체됨' relation이 연결된 행은 후보에서 제외한다.
    """
    by_topic: dict[str, list[FactLedgerRow]] = defaultdict(list)
    for row in rows:
        key = row.topic_key.strip()
        val = row.value.strip()
        if not key or not val:
            continue
        if row.grade == "불일치":
            continue
        if row.superseded_ids:
            continue
        by_topic[key].append(row)

    candidates: list[ConflictCandidate] = []
    for topic_key, topic_rows in sorted(by_topic.items()):
        if len(topic_rows) < 2:
            continue
        distinct_values = {r.value.strip() for r in topic_rows}
        if len(distinct_values) < 2:
            continue

        for row in topic_rows:
            row_val = row.value.strip()
            peers = tuple(sorted({other.value.strip() for other in topic_rows if other.value.strip() != row_val}))
            candidates.append(
                ConflictCandidate(
                    page_id=row.page_id,
                    claim=row.claim,
                    topic_key=topic_key,
                    value=row.value,
                    source_url=row.source_url,
                    grade=row.grade,
                    peer_values=peers,
                )
            )
    return candidates


# ------------------------------------------------------------------
# 4) 자동 재검증 실행 오케스트레이터
# ------------------------------------------------------------------


@dataclass(frozen=True)
class RevalidationOutcome:
    database_id: str
    data_source_id: str
    checked_at_kst: datetime
    total_rows: int
    evaluated_rows: int
    decisions: list[RevalidationDecision]
    conflicts: list[ConflictUpdate]
    fact_alerts: list[FactAlert]
    updated_page_count: int
    skipped_login_count: int
    summary_text: str
    success_count: int = 0
    failure_count: int = 0
    row_exception_count: int = 0
    update_failed_count: int = 0
    conflict_candidates: list[ConflictCandidate] = field(default_factory=list)
    source_updated_count: int = 0
    grade_snapshot_count: int = 0


def revalidate_fact_ledger(
    client: NotionClient,
    cache: LocalCache,
    *,
    parent_page_id: Optional[str] = None,
    configured_database_id: Optional[str] = None,
    configured_data_source_id: Optional[str] = None,
    parent_page_title: str = PARENT_PAGE_TITLE,
    db_title: str = FACT_LEDGER_DB_TITLE,
    now_kst: Optional[datetime] = None,
    fetcher: Optional[PoliteWebFetcher] = None,
    host_delay_sec: float = 3.0,
    dry_run: bool = False,
) -> RevalidationOutcome:
    """'📒 팩트 장부'의 확인됨/원문대조필요 행을 재검증하고 충돌을 판정해 반영한다."""
    current_kst = (now_kst or datetime.now(KST)).astimezone(KST)
    current_iso = current_kst.isoformat(timespec="seconds")
    detected_at_str = current_kst.strftime("%Y-%m-%d %H:%M:%S KST")

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
    pages, _ = client.query_data_source(ds_id)
    active_pages = [p for p in pages if not _is_trashed(p)]
    rows = [parse_fact_ledger_row(p) for p in active_pages]

    web_fetcher = fetcher or PoliteWebFetcher(host_delay_sec=host_delay_sec)

    decisions: list[RevalidationDecision] = []
    pending_props: dict[str, dict[str, Any]] = {}
    effective_grades: dict[str, Optional[str]] = {r.page_id: r.grade for r in rows}
    effective_reval_results: dict[str, Optional[str]] = {
        r.page_id: r.auto_revalidation_result for r in rows
    }
    alerts: list[FactAlert] = []
    skipped_login_count = 0
    row_exception_count = 0
    source_updated_count = 0

    for row in rows:
        if row.grade not in FACT_LEDGER_REVALIDATE_TARGET_GRADES:
            continue

        try:
            if not row.source_url:
                decision = judge_revalidation(row, None)
            elif cache.is_url_in_skip_list(row.source_url):
                skipped_login_count += 1
                decision = judge_revalidation(row, None, already_in_skip_list=True)
            else:
                prev_updated_at = cache.get_fact_source_updated_at(row.page_id)
                fetch_res = web_fetcher.fetch(row.source_url)
                decision = judge_revalidation(
                    row,
                    fetch_res,
                    previous_article_updated_at=prev_updated_at,
                )
                if fetch_res.kind == "ok" and fetch_res.article_updated_at and not dry_run:
                    cache.save_fact_source_updated_at(
                        row.page_id,
                        source_url=row.source_url,
                        article_updated_at=fetch_res.article_updated_at,
                        checked_at_kst=current_kst,
                    )

            if decision.add_to_skip_list and row.source_url and not dry_run:
                cache.add_url_to_skip_list(
                    row.source_url,
                    page_id=row.page_id,
                    reason="로그인 필요",
                    added_at_kst=current_kst,
                )

            props: dict[str, Any] = {
                "자동 재검증 시각": {"date": {"start": current_iso}},
                "자동 재검증 결과": {"select": {"name": decision.result}},
            }
            if decision.new_grade and decision.new_grade != decision.old_grade:
                assert_no_grade_upgrade(decision.old_grade, decision.new_grade)
                props["등급"] = {"select": {"name": decision.new_grade}}
        except ReadOnlyGuardError:
            raise
        except Exception as exc:
            row_exception_count += 1
            decision = RevalidationDecision(
                page_id=row.page_id,
                old_grade=row.grade,
                new_grade=row.grade,
                result="열림 실패 (네트워크 오류)",
                add_to_fact_alerts=False,
                add_to_skip_list=False,
                detail=f"행 처리 예외 격리({type(exc).__name__}): {exc}",
            )
            props = {
                "자동 재검증 시각": {"date": {"start": current_iso}},
                "자동 재검증 결과": {"select": {"name": decision.result}},
            }

        decisions.append(decision)
        effective_grades[row.page_id] = decision.new_grade
        effective_reval_results[row.page_id] = decision.result
        pending_props[row.page_id] = props

        if decision.add_to_fact_alerts:
            alerts.append(
                FactAlert(
                    page_id=row.page_id,
                    claim=row.claim,
                    topic_key=row.topic_key,
                    reason="발췌 사라짐 (원문 재대조 필요)",
                    url=row.source_url,
                    old_grade=decision.old_grade,
                    new_grade=decision.new_grade,
                    detected_at_kst=detected_at_str,
                )
            )

        if decision.source_updated:
            source_updated_count += 1
            alerts.append(
                FactAlert(
                    page_id=row.page_id,
                    claim=row.claim,
                    topic_key=row.topic_key,
                    reason=(
                        f"{SOURCE_UPDATED_MARKER} "
                        f"({decision.previous_article_updated_at} → {decision.current_article_updated_at})"
                    ),
                    url=row.source_url,
                    old_grade=decision.old_grade,
                    new_grade=decision.new_grade,
                    detected_at_kst=detected_at_str,
                )
            )

    # URL 재검증 반영 후 상태 기준으로 충돌 판정 수행 (둘 다 '확인됨'일 때만 '불일치' 강등)
    post_rows = [
        FactLedgerRow(
            page_id=r.page_id,
            claim=r.claim,
            topic_key=r.topic_key,
            value=r.value,
            source_url=r.source_url,
            excerpt=r.excerpt,
            verified_at=r.verified_at,
            verifier=r.verifier,
            grade=effective_grades.get(r.page_id, r.grade),
            auto_revalidated_at=r.auto_revalidated_at,
            auto_revalidation_result=effective_reval_results.get(
                r.page_id, r.auto_revalidation_result
            ),
            conflict_ids=r.conflict_ids,
            superseded_ids=r.superseded_ids,
            uploader=r.uploader,
            corrector=r.corrector,
        )
        for r in rows
    ]
    conflicts = detect_fact_conflicts(post_rows)

    post_conflict_grades = dict(effective_grades)
    for cu in conflicts:
        assert_no_grade_upgrade(cu.old_grade, cu.new_grade)
        post_conflict_grades[cu.page_id] = cu.new_grade
        props = pending_props.setdefault(cu.page_id, {})
        props["등급"] = {"select": {"name": cu.new_grade}}
        props["충돌"] = {"relation": [{"id": cid} for cid in cu.conflict_ids]}
        alerts.append(
            FactAlert(
                page_id=cu.page_id,
                claim=cu.claim,
                topic_key=cu.topic_key,
                reason=f"동일 주제키 값 충돌 (값='{cu.value}')",
                url=cu.source_url,
                old_grade=cu.old_grade,
                new_grade=cu.new_grade,
                detected_at_kst=detected_at_str,
            )
        )

    # 수정 3: 등급과 무관하게 같은 주제키에 다른 값이 있으면 대시보드 '충돌 후보' 표시 (등급 불변)
    post_conflict_rows = [
        FactLedgerRow(
            page_id=r.page_id,
            claim=r.claim,
            topic_key=r.topic_key,
            value=r.value,
            source_url=r.source_url,
            excerpt=r.excerpt,
            verified_at=r.verified_at,
            verifier=r.verifier,
            grade=post_conflict_grades.get(r.page_id, r.grade),
            auto_revalidated_at=r.auto_revalidated_at,
            auto_revalidation_result=effective_reval_results.get(
                r.page_id, r.auto_revalidation_result
            ),
            conflict_ids=r.conflict_ids,
            superseded_ids=r.superseded_ids,
            uploader=r.uploader,
            corrector=r.corrector,
        )
        for r in rows
    ]
    conflict_candidates = detect_fact_conflict_candidates(post_conflict_rows)
    for cand in conflict_candidates:
        alerts.append(
            FactAlert(
                page_id=cand.page_id,
                claim=cand.claim,
                topic_key=cand.topic_key,
                reason=f"충돌 후보 (값='{cand.value}')",
                url=cand.source_url,
                old_grade=cand.grade,
                new_grade=cand.grade,
                detected_at_kst=detected_at_str,
            )
        )

    updated_count = 0
    update_failed_count = 0
    grade_snapshot_count = 0
    if not dry_run:
        needed_options = tuple(
            dict.fromkeys([*FACT_LEDGER_REVALIDATION_RESULTS, *(d.result for d in decisions)])
        )
        _ensure_revalidation_result_options(client, ds_id, needed_options)
        for page_id, props in pending_props.items():
            try:
                client.update_fact_ledger_page(page_id, props, data_source_id=ds_id)
                updated_count += 1
            except ReadOnlyGuardError:
                raise
            except Exception:
                update_failed_count += 1
        cache.replace_fact_alerts(alerts, checked_at_kst=current_kst)
        # P2.5 등급 이력 스냅샷: 재검증 실행마다 전체 행(비대상 행 포함)의 최종 상태를 누적 기록
        snapshot_rows: list[tuple[str, Optional[str], Optional[str]]] = [
            (
                r.page_id,
                post_conflict_grades.get(r.page_id, r.grade),
                effective_reval_results.get(r.page_id, r.auto_revalidation_result),
            )
            for r in rows
        ]
        grade_snapshot_count = cache.save_fact_grade_snapshot(
            snapshot_rows,
            ran_at_kst=current_kst,
        )

    result_counts: dict[str, int] = defaultdict(int)
    open_failure_breakdown: dict[str, int] = defaultdict(int)
    for d in decisions:
        result_counts[d.result] += 1
        if is_open_failure_result(d.result):
            open_failure_breakdown[d.result] += 1

    failure_count = sum(open_failure_breakdown.values())
    success_count = len(decisions) - failure_count

    open_fail_detail = ""
    if open_failure_breakdown:
        parts = ", ".join(f"{k} {v}건" for k, v in sorted(open_failure_breakdown.items()))
        open_fail_detail = f"({parts})"

    lines = [
        f"[팩트 장부 재검증] 실행 시각: {detected_at_str}",
        f"대상 DB: {db_title} (data_source_id={ds_id})",
        f"전체 행: {len(rows)}건 | 재검증 대상(확인됨/원문대조필요): {len(decisions)}건",
        (
            "판정 요약: "
            f"발췌 그대로 {result_counts['발췌 그대로']}건 | "
            f"발췌 사라짐 {result_counts['발췌 사라짐']}건 | "
            f"확인 불가 {result_counts['확인 불가']}건 | "
            f"열림 실패 {failure_count}건{open_fail_detail} | "
            f"로그인 필요 {result_counts['로그인 필요']}건(목록 스킵 {skipped_login_count}건)"
        ),
        (
            "행 처리 요약: "
            f"성공 {success_count}건 | 실패 {failure_count}건 "
            f"(예외 격리 {row_exception_count}건, 노션 갱신 실패 {update_failed_count}건) | "
            f"{SOURCE_UPDATED_MARKER} {source_updated_count}건"
        ),
        (
            f"충돌(불일치) 판정: {len(conflicts)}건 | "
            f"충돌 후보: {len(conflict_candidates)}건 | "
            f"팩트 경보: {len(alerts)}건 | 노션 갱신: {updated_count}건 | "
            f"등급 이력 스냅샷: {grade_snapshot_count}건"
        ),
    ]
    if dry_run:
        lines.append("[dry-run] 노션 DB 및 로컬 경보·이력 스냅샷 쓰기를 생략했습니다.")

    return RevalidationOutcome(
        database_id=init_res.database_id,
        data_source_id=ds_id,
        checked_at_kst=current_kst,
        total_rows=len(rows),
        evaluated_rows=len(decisions),
        decisions=decisions,
        conflicts=conflicts,
        fact_alerts=alerts,
        updated_page_count=updated_count,
        skipped_login_count=skipped_login_count,
        summary_text="\n".join(lines),
        success_count=success_count,
        failure_count=failure_count,
        row_exception_count=row_exception_count,
        update_failed_count=update_failed_count,
        conflict_candidates=conflict_candidates,
        source_updated_count=source_updated_count,
        grade_snapshot_count=grade_snapshot_count,
    )
