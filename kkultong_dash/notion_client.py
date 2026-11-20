"""Notion REST API 클라이언트 (API 버전 2026-03-11, 표준 라이브러리 전용).

공식 문서 근거(2026-11-20 확인):
- API 버전: https://developers.notion.com/reference/versioning (최신 2026-03-11)
- 버전별 변경: https://developers.notion.com/reference/changes-by-version
  - 2025-09-03: /v1/data_sources 도입
  - 2026-03-11: archived 제거(in_trash 사용), Append Block Children의 after -> position 객체 변경
- 요청 한도/재시도: https://developers.notion.com/reference/request-limits
  - 연결당 한도: Business/Enterprise 분당 600회(평균 초당 10회), 그 외 요금제 분당 180회(평균 초당 3회)
  - 데이터 소스 조회(query) 별도 한도: 연결당 초당 약 3회
  - 워크스페이스 공유 한도: 전체 연결 공유, Retry-After가 1분을 넘을 수 있음
  - 429(rate_limited) / 529(service_overload): Retry-After 헤더(정수 초) 준수 + 지터 재시도
  - 단, rate_limit_reason == "public_api_request_blocked" 인 429는 재시도하지 않음
  - 500/502/503/504: 멱등 요청(GET, DELETE 및 읽기 전용 query/search)만 재시도; 쓰기 503은 중복 쓰기 방지를 위해 재시도하지 않음

미확인 사항:
- 실제 워크스페이스 요금제 등급은 미확인 상태이므로 보수적으로 초당 3회 미만(요청 간 최소 0.35초)을 기본값으로 둔다.
- 실제 토큰으로의 실호출 응답은 토큰 미전달 상태이므로 미확인이다.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from . import (
    DASHBOARD_PAGE_TITLE,
    FACT_LEDGER_DB_TITLE,
    NOTION_API_BASE_URL,
    NOTION_API_VERSION,
)


class NotionAPIError(RuntimeError):
    """Notion API 호출 실패 예외 (실패 원인을 명확히 보존한다)."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        additional_data: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.additional_data = additional_data or {}


class NotionEmptyResponseError(NotionAPIError):
    """HTTP 본문이 비어 있거나 기대한 JSON 구조가 없을 때 발생하는 예외."""


class ReadOnlyGuardError(RuntimeError):
    """읽기 전용 원본 리소스에 대한 쓰기 시도를 차단할 때 발생하는 예외."""


@dataclass(frozen=True)
class HTTPResponse:
    """테스트 주입을 위한 경량 HTTP 응답 래퍼."""

    status: int
    headers: dict[str, str]
    body: str


TransportFn = Callable[[str, str, dict[str, str], Optional[bytes], float], HTTPResponse]


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> HTTPResponse:
    req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            resp_body = resp.read().decode("utf-8", errors="replace")
            return HTTPResponse(status=resp.status, headers=resp_headers, body=resp_body)
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        resp_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return HTTPResponse(status=exc.code, headers=resp_headers, body=resp_body)
    except urllib.error.URLError as exc:
        raise NotionAPIError(f"네트워크 오류: {exc.reason}") from exc
    except TimeoutError as exc:
        raise NotionAPIError(f"요청 타임아웃({timeout}초 초과)") from exc


class NotionClient:
    """읽기 전용 원본 + 대시보드 페이지 및 팩트 장부 DB 한정 쓰기를 강제하는 Notion API 클라이언트."""

    def __init__(
        self,
        token: str,
        *,
        api_version: str = NOTION_API_VERSION,
        base_url: str = NOTION_API_BASE_URL,
        min_interval_sec: float = 0.35,
        timeout_sec: float = 30.0,
        max_attempts: int = 6,
        max_retry_after_sec: float = 120.0,
        protected_ids: Iterable[str] = (),
        transport: Optional[TransportFn] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        jitter_fn: Callable[[], float] = lambda: random.uniform(0.0, 0.25),
    ) -> None:
        if not token or not token.strip():
            raise ValueError("NOTION_TOKEN이 비어 있습니다.")
        self._token = token.strip()
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.max_attempts = max(1, int(max_attempts))
        self.max_retry_after_sec = max(1.0, float(max_retry_after_sec))
        self._protected_ids = {self._canonical_id(i) for i in protected_ids if i}
        self._allowed_fact_ledger_ds_ids: set[str] = set()
        self._transport = transport or _default_transport
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._jitter = jitter_fn
        self._last_request_at: Optional[float] = None

    @staticmethod
    def _canonical_id(notion_id: str) -> str:
        return notion_id.replace("-", "").strip().lower()

    def register_protected_id(self, notion_id: str) -> None:
        """절대 수정/삭제해서는 안 되는 페이지/데이터 소스 ID를 보호 목록에 등록한다."""
        if notion_id:
            cid = self._canonical_id(notion_id)
            self._protected_ids.add(cid)
            self._allowed_fact_ledger_ds_ids.discard(cid)

    def register_fact_ledger_data_source(self, data_source_id: str) -> None:
        """검증된 '📒 팩트 장부' 데이터 소스 ID만 쓰기 허용 목록에 등록한다."""
        if not data_source_id:
            return
        self._assert_not_protected(data_source_id, "register_fact_ledger_data_source")
        self._allowed_fact_ledger_ds_ids.add(self._canonical_id(data_source_id))

    def _assert_not_protected(self, target_id: str, operation: str) -> None:
        if self._canonical_id(target_id) in self._protected_ids:
            raise ReadOnlyGuardError(
                f"읽기 전용 보호 위반 차단: {operation} 대상({target_id})은 보호된 기존 리소스입니다."
            )

    def _assert_allowed_fact_ledger_ds(self, data_source_id: str, operation: str) -> None:
        self._assert_not_protected(data_source_id, operation)
        if self._canonical_id(data_source_id) not in self._allowed_fact_ledger_ds_ids:
            raise ReadOnlyGuardError(
                f"쓰기 허용 대상 위반 차단: {operation} 대상({data_source_id})은 "
                f"검증된 '{FACT_LEDGER_DB_TITLE}' 데이터 소스로 등록되지 않았습니다."
            )

    @staticmethod
    def _assert_no_confirmed_grade_write(properties: dict[str, Any], operation: str) -> None:
        """절대 규칙: 앱은 등급을 '확인됨'으로 올리거나 자동 설정하지 않는다."""
        grade_prop = properties.get("등급")
        if isinstance(grade_prop, dict):
            sel = grade_prop.get("select")
            if isinstance(sel, dict) and str(sel.get("name") or "").strip() == "확인됨":
                raise ReadOnlyGuardError(
                    f"등급 상향 금지 위반 차단({operation}): 앱은 등급을 '확인됨'으로 설정할 수 없습니다."
                )

    def _pace(self) -> None:
        if self.min_interval_sec <= 0.0:
            return
        now = self._monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self.min_interval_sec - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        method_upper = method.upper()
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self.api_version,
            "Accept": "application/json",
        }
        body_bytes: Optional[bytes] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

        is_idempotent = idempotent or method_upper in {"GET", "DELETE"}

        for attempt in range(self.max_attempts):
            self._pace()
            resp = self._transport(method_upper, url, headers, body_bytes, self.timeout_sec)

            if 200 <= resp.status < 300:
                return self._parse_json_object(resp)

            error_payload = self._try_parse_json(resp.body)
            error_code = str(error_payload.get("code") or "") if isinstance(error_payload, dict) else ""
            error_msg = str(error_payload.get("message") or "") if isinstance(error_payload, dict) else ""
            additional_data = (
                error_payload.get("additional_data")
                if isinstance(error_payload, dict) and isinstance(error_payload.get("additional_data"), dict)
                else {}
            )

            # 공식 문서: 429 중 public_api_request_blocked 는 재시도해도 해결되지 않으므로 즉시 중단
            if resp.status == 429 and additional_data.get("rate_limit_reason") == "public_api_request_blocked":
                raise NotionAPIError(
                    f"HTTP 429 (public_api_request_blocked): API 접근이 제한되었습니다. {error_msg}".strip(),
                    status=429,
                    code=error_code or "rate_limited",
                    additional_data=additional_data,
                )

            retryable = resp.status in {429, 529} or (
                is_idempotent and resp.status in {500, 502, 503, 504}
            )

            if retryable and attempt < self.max_attempts - 1:
                delay = self._compute_retry_delay(resp.headers, additional_data, attempt)
                if delay > self.max_retry_after_sec:
                    raise NotionAPIError(
                        f"HTTP {resp.status} ({error_code or 'rate_limited'}): "
                        f"Retry-After({delay:.0f}초)가 허용 상한({self.max_retry_after_sec:.0f}초)을 초과했습니다.",
                        status=resp.status,
                        code=error_code or None,
                        additional_data=additional_data,
                    )
                self._sleep(delay + self._jitter())
                continue

            raise self._build_http_error(resp.status, error_code, error_msg, additional_data, resp.body)

        raise NotionAPIError("예상치 못한 재시도 루프 종료")

    @staticmethod
    def _parse_json_object(resp: HTTPResponse) -> dict[str, Any]:
        if not resp.body or not resp.body.strip():
            raise NotionEmptyResponseError(
                f"빈 응답 수신 (HTTP {resp.status}, 본문 비어 있음)",
                status=resp.status,
                code="empty_response",
            )
        try:
            parsed = json.loads(resp.body)
        except json.JSONDecodeError as exc:
            raise NotionEmptyResponseError(
                f"응답 JSON 파싱 실패 (HTTP {resp.status}): {exc}",
                status=resp.status,
                code="invalid_json_response",
            ) from exc
        if not isinstance(parsed, dict):
            raise NotionEmptyResponseError(
                f"예상 외 응답 형식 (HTTP {resp.status}): 최상위 객체가 dict가 아님",
                status=resp.status,
                code="unexpected_response_type",
            )
        return parsed

    @staticmethod
    def _try_parse_json(raw: str) -> Optional[dict[str, Any]]:
        if not raw or not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _compute_retry_delay(
        headers: dict[str, str],
        additional_data: dict[str, Any],
        attempt: int,
    ) -> float:
        raw_retry_after = headers.get("retry-after")
        if raw_retry_after is None and "retry_after" in additional_data:
            raw_retry_after = str(additional_data["retry_after"])
        if raw_retry_after is not None:
            try:
                parsed = float(raw_retry_after)
                if parsed >= 0:
                    return parsed
            except ValueError:
                pass
        return float(min(2**attempt, 30))

    def _build_http_error(
        self,
        status: int,
        code: str,
        message: str,
        additional_data: dict[str, Any],
        raw_body: str,
    ) -> NotionAPIError:
        hints = {
            401: "인증 실패(NOTION_TOKEN 확인 필요)",
            403: "권한 없음(통합 기능/공유 범위 또는 워크스페이스 블록 한도 확인 필요)",
            404: "대상 없음 또는 통합 공유 미연결(노션 페이지/DB의 '연결 추가' 확인 필요)",
            429: "요청 한도 초과(재시도 한도 소진)",
            529: "노션 서비스 과부하(재시도 한도 소진)",
        }
        hint = hints.get(status, "")
        detail = message or (raw_body[:200].strip() if raw_body else "응답 본문 없음")
        if self._token and self._token in detail:
            detail = detail.replace(self._token, "[REDACTED]")
        parts = [f"HTTP {status}"]
        if code:
            parts.append(f"[{code}]")
        if hint:
            parts.append(hint)
        parts.append(detail)
        if status == 503 and additional_data.get("committed_resource_id"):
            parts.append(
                f"(주의: 쓰기가 이미 반영되었을 수 있음 committed_resource_id={additional_data['committed_resource_id']})"
            )
        return NotionAPIError(
            " — ".join(parts),
            status=status,
            code=code or None,
            additional_data=additional_data,
        )

    # ------------------------------------------------------------------
    # 읽기 전용 메서드
    # ------------------------------------------------------------------

    def query_data_source(
        self,
        data_source_id: str,
        *,
        last_edited_on_or_after: Optional[str] = None,
        page_size: int = 100,
    ) -> tuple[list[dict[str, Any]], bool]:
        """POST /v1/data_sources/{data_source_id}/query 로 전체 페이지를 순회한다.

        Returns:
            (pages, is_incomplete): pages 목록과 10,000건 상한(request_status.type == "incomplete") 여부
        """
        pages: list[dict[str, Any]] = []
        is_incomplete = False
        next_cursor: Optional[str] = None
        seen_cursors: set[str] = set()

        while True:
            payload: dict[str, Any] = {
                "page_size": min(max(1, page_size), 100),
                "result_type": "page",
                "archive_status": "active",
            }
            if last_edited_on_or_after:
                # 공식 문서: https://developers.notion.com/reference/filter-data-source-entries
                # timestamp 필터에는 property 키를 넣지 않는다.
                payload["filter"] = {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": last_edited_on_or_after},
                }
            if next_cursor is not None:
                payload["start_cursor"] = next_cursor

            data = self._request(
                "POST",
                f"/data_sources/{data_source_id}/query",
                json_body=payload,
                idempotent=True,
            )

            results = data.get("results")
            if not isinstance(results, list):
                raise NotionEmptyResponseError(
                    f"데이터 소스({data_source_id}) 응답에 results 배열이 없습니다.",
                    code="missing_results",
                )

            req_status = data.get("request_status")
            if isinstance(req_status, dict) and req_status.get("type") == "incomplete":
                is_incomplete = True

            for item in results:
                if isinstance(item, dict) and item.get("object") == "page":
                    pages.append(item)

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

        return pages, is_incomplete

    def search_pages(self, query: str) -> list[dict[str, Any]]:
        """POST /v1/search 로 제목 기준 페이지를 검색한다."""
        pages: list[dict[str, Any]] = []
        next_cursor: Optional[str] = None
        seen_cursors: set[str] = set()

        while True:
            payload: dict[str, Any] = {
                "query": query,
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if next_cursor is not None:
                payload["start_cursor"] = next_cursor

            data = self._request("POST", "/search", json_body=payload, idempotent=True)
            results = data.get("results")
            if not isinstance(results, list):
                raise NotionEmptyResponseError("검색 응답에 results 배열이 없습니다.", code="missing_results")

            for item in results:
                if isinstance(item, dict) and item.get("object") == "page":
                    pages.append(item)

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

        return pages

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        """GET /v1/pages/{page_id} 로 페이지 메타데이터를 조회한다."""
        return self._request("GET", f"/pages/{page_id}", idempotent=True)

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        """GET /v1/databases/{database_id} 로 데이터베이스 메타데이터(하위 data_sources 포함)를 조회한다."""
        return self._request("GET", f"/databases/{database_id}", idempotent=True)

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        """GET /v1/data_sources/{data_source_id} 로 데이터 소스 스키마를 조회한다."""
        return self._request("GET", f"/data_sources/{data_source_id}", idempotent=True)

    def search_data_sources(self, query: str) -> list[dict[str, Any]]:
        """POST /v1/search 로 제목 기준 데이터 소스를 검색한다 (API 2025-09-03+ data_source 필터)."""
        sources: list[dict[str, Any]] = []
        next_cursor: Optional[str] = None
        seen_cursors: set[str] = set()

        while True:
            payload: dict[str, Any] = {
                "query": query,
                "filter": {"property": "object", "value": "data_source"},
                "page_size": 100,
            }
            if next_cursor is not None:
                payload["start_cursor"] = next_cursor

            data = self._request("POST", "/search", json_body=payload, idempotent=True)
            results = data.get("results")
            if not isinstance(results, list):
                raise NotionEmptyResponseError("검색 응답에 results 배열이 없습니다.", code="missing_results")

            for item in results:
                if isinstance(item, dict) and item.get("object") == "data_source":
                    sources.append(item)

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

        return sources

    def list_block_children(self, block_id: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        """GET /v1/blocks/{block_id}/children 으로 1단계 하위 블록을 모두 조회한다."""
        blocks: list[dict[str, Any]] = []
        next_cursor: Optional[str] = None
        seen_cursors: set[str] = set()

        while True:
            params: dict[str, Any] = {"page_size": min(max(1, page_size), 100)}
            if next_cursor is not None:
                params["start_cursor"] = next_cursor

            data = self._request(
                "GET",
                f"/blocks/{block_id}/children",
                params=params,
                idempotent=True,
            )
            results = data.get("results")
            if not isinstance(results, list):
                raise NotionEmptyResponseError(
                    f"블록({block_id}) children 응답에 results 배열이 없습니다.",
                    code="missing_results",
                )
            for item in results:
                if isinstance(item, dict):
                    blocks.append(item)

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

        return blocks

    # ------------------------------------------------------------------
    # 대시보드 페이지 전용 쓰기 메서드 (기존 DB/페이지 수정 메서드는 의도적으로 미구현)
    # ------------------------------------------------------------------

    def create_dashboard_page(
        self,
        parent_page_id: str,
        title: str = DASHBOARD_PAGE_TITLE,
    ) -> dict[str, Any]:
        """'천하제일 꿀통대회' 아래에 대시보드 페이지 1장을 최초 1회 생성한다."""
        if title != DASHBOARD_PAGE_TITLE:
            raise ReadOnlyGuardError(
                f"허용되지 않은 페이지 생성 제목입니다: {title!r} (허용: {DASHBOARD_PAGE_TITLE!r})"
            )
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [
                        {
                            "type": "text",
                            "text": {"content": title},
                        }
                    ]
                }
            },
        }
        return self._request("POST", "/pages", json_body=payload, idempotent=False)

    def append_block_children(
        self,
        block_id: str,
        children: list[dict[str, Any]],
        *,
        position: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """PATCH /v1/blocks/{block_id}/children 으로 블록을 추가한다 (1회 최대 100개).

        공식 문서(2026-03-11): after 파라미터는 제거되었고 position 객체를 사용한다.
        """
        self._assert_not_protected(block_id, "append_block_children")
        if not children:
            return []
        if len(children) > 100:
            raise ValueError(f"Notion API 단일 요청 블록 상한(100개) 초과: {len(children)}개")

        payload: dict[str, Any] = {"children": children}
        if position is not None:
            payload["position"] = position

        data = self._request(
            "PATCH",
            f"/blocks/{block_id}/children",
            json_body=payload,
            idempotent=False,
        )
        results = data.get("results")
        return results if isinstance(results, list) else []

    def delete_block(self, block_id: str) -> dict[str, Any]:
        """DELETE /v1/blocks/{block_id} 로 대시보드 내부 자동 갱신 블록을 휴지통으로 보낸다."""
        self._assert_not_protected(block_id, "delete_block")
        return self._request("DELETE", f"/blocks/{block_id}", idempotent=True)

    # ------------------------------------------------------------------
    # P2 팩트 장부 DB 전용 쓰기 메서드 (보호된 기존 DB/페이지 쓰기 및 '확인됨' 상향 원천 차단)
    # ------------------------------------------------------------------

    def create_fact_ledger_database(
        self,
        parent_page_id: str,
        initial_properties: dict[str, Any],
        *,
        title: str = FACT_LEDGER_DB_TITLE,
    ) -> dict[str, Any]:
        """'천하제일 꿀통대회' 아래에 '📒 팩트 장부' DB를 생성한다 (API 2026-03-11 initial_data_source)."""
        if title != FACT_LEDGER_DB_TITLE:
            raise ReadOnlyGuardError(
                f"허용되지 않은 DB 생성 제목입니다: {title!r} (허용: {FACT_LEDGER_DB_TITLE!r})"
            )
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {
                "properties": initial_properties,
            },
        }
        return self._request("POST", "/databases", json_body=payload, idempotent=False)

    def update_fact_ledger_schema(
        self,
        data_source_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """검증된 팩트 장부 데이터 소스의 스키마(자기참조 relation 등)를 갱신한다."""
        self._assert_allowed_fact_ledger_ds(data_source_id, "update_fact_ledger_schema")
        payload = {"properties": properties}
        return self._request(
            "PATCH",
            f"/data_sources/{data_source_id}",
            json_body=payload,
            idempotent=False,
        )

    def create_fact_ledger_page(
        self,
        data_source_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """검증된 '📒 팩트 장부' 데이터 소스에 새 행(페이지)을 추가한다 (등급 '확인됨' 자동 설정 금지)."""
        self._assert_allowed_fact_ledger_ds(data_source_id, "create_fact_ledger_page")
        self._assert_no_confirmed_grade_write(properties, "create_fact_ledger_page")
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._request("POST", "/pages", json_body=payload, idempotent=False)

    def update_fact_ledger_page(
        self,
        page_id: str,
        properties: dict[str, Any],
        *,
        data_source_id: str,
    ) -> dict[str, Any]:
        """검증된 '📒 팩트 장부' 소속 행의 속성을 갱신한다 (등급 '확인됨' 상향 금지)."""
        self._assert_not_protected(page_id, "update_fact_ledger_page")
        self._assert_allowed_fact_ledger_ds(data_source_id, "update_fact_ledger_page")
        self._assert_no_confirmed_grade_write(properties, "update_fact_ledger_page")
        payload = {"properties": properties}
        return self._request("PATCH", f"/pages/{page_id}", json_body=payload, idempotent=False)
