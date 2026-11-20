# 꿀통 대시보드 P1 (MVP) 착수 보고 초안

- 작성일: 2026-11-20
- 대상 저장소: `plazalabs/kkultong-hq` (`main`)
- 작업 범위: P1 (MVP) — 노션 DB 2종 읽기 전용 집계 및 `'📊 꿀통 대시보드 (자동 갱신)'` 페이지 자동 갱신 스크립트 (P2·P3 미포함)

---

## 1. 착수 전 필수 점검 항목 현황

| 항목 | 상태 | 상세 내용 |
|---|---|---|
| 노션 API 토큰 (`NOTION_TOKEN`) | **대기 중 — 토큰 미전달** | 현재 개발 세션에는 노션 토큰이 없어 실호출 및 대시보드 페이지 생성을 수행하지 않음. 뮤즈의 Ubuntu 실행 환경(`.env` 또는 환경변수)에 토큰 투입 후 첫 실행 검증 필요. |
| 노션 통합 공유 범위 | **대기 중 — 토큰 미전달** | 토큰 발급 및 연결 시 최소 권한 대상: 체험단 DB(`41a89944-f230-8091-9e54-000b1d8dea3c`), 경쟁 기록 DB(`41b89944-f230-814b-a322-000bfaf7e533`), 부모 페이지 `'천하제일 꿀통대회'`. 기존 DB·허브 운영 DB·게시판·규정은 절대 수정하지 않음. |
| 정산 기록 / 정산판 위치 | **미확인** | 정산판 위치 및 스키마 미확인. 추측하지 않고 P1 대시보드 섹션 G를 `'정산: 수동 확인'`으로 고정 표시함. |
| 워크스페이스 요금제 등급 | **미확인** | Business/Enterprise(분당 600회)인지 일반 요금제(분당 180회)인지 미확인. 보수적으로 일반 요금제 및 데이터 소스 쿼리 별도 한도(초당 약 3회)에 맞춰 요청 간 최소 `0.35초` 간격 적용. |
| 부모 페이지(`'천하제일 꿀통대회'`) ID | **미확인** | 실행 시 `NOTION_PARENT_PAGE_ID` 환경변수 우선, 미지정 시 `POST /v1/search`로 제목 정확 일치 탐색. |
| `collection://` UUID의 REST API 실응답 | **미확인** | `41a89944-f230-8091-9e54-000b1d8dea3c`, `41b89944-f230-814b-a322-000bfaf7e533`을 `/v1/data_sources/{data_source_id}/query` 기본값으로 설정하되, 실응답 여부는 토큰 전달 후 첫 실행에서 확인 필요(환경변수 오버라이드 지원). |

---

## 2. 공식 개발자 문서로 확인한 사실 (원문 근거 및 URL)

### 2.1 Notion API 요청 한도 (Rate Limits & Size Limits)
- **공식 문서 URL**: https://developers.notion.com/reference/request-limits
- **확인 원문**:
  - 일반 한도 구조:
    > "The Notion API enforces two rate limits: a per-connection limit and a per-workspace limit. Some endpoints also have their own limit."
  - 연결당 한도 (요금제별):
    > "Each connection has a fixed budget of requests per 60-second window, based on the workspace's plan:"
    > - Business and Enterprise: `"600 requests per minute (an average of 10 per second)"`
    > - All other plans: `"180 requests per minute (an average of 3 per second)"`
  - 데이터 소스 조회(Query) 별도 한도:
    > "Querying a data source has a separate limit of about 3 requests per second per connection. A request over this limit returns an HTTP 429 response with a Retry-After header."
  - 워크스페이스 공유 한도:
    > "A separate limit is shared across all of the workspace's connections and scaled to the workspace's plan. ... Retry-After for this limit can be longer than a minute."
  - 429 / 529 재시도 정책:
    > "Connections should handle HTTP 429 and 529 responses and respect the Retry-After response header. The header value is an integer number of seconds. Use the returned value rather than assuming a fixed wait."
    > "Do not retry every error. Retry 429 and 529 responses, except a 429 with `public_api_request_blocked`."
  - 쓰기 503 주의사항:
    > "A write can save its change and still return 503 when Notion runs out of time building the response. Repeating that write applies the change twice, so read `additional_data.retry_guidance` before you retry."
  - 요청 크기 한도:
    > Rich text `text.content`: `2000 characters` / Any array of all block types, including rich text objects: `100 elements` / Payloads overall: `1000 block elements and 500KB overall`.

### 2.2 데이터 소스 엔드포인트가 있는 현행 Notion API 버전
- **적용 버전 문자열**: **`2026-03-11`**
- **공식 문서 URL 및 원문**:
  1. Versioning — https://developers.notion.com/reference/versioning
     > "The Notion-Version header must be included in all REST API requests. ... The most recent Notion-Version is `2026-03-11`."
  2. Changes by version — https://developers.notion.com/reference/changes-by-version
     - `2025-09-03`:
       > "`/v1/databases` APIs are re-organized into `/v1/data_sources` (for managing individual data sources under a database container) and `/v1/databases` (for managing the database container.)"
     - `2026-03-11`:
       > "The `after` parameter on Append Block Children is replaced by a `position` object. The `archived` field is removed in favor of `in_trash` across all request parameters and response bodies. The `transcription` block type is renamed to `meeting_notes`."
  3. Query a data source — https://developers.notion.com/reference/query-a-data-source
     - 엔드포인트: `POST /v1/data_sources/{data_source_id}/query`
     - 페이지네이션 상한: 쿼리당 최대 10,000건 (`request_status.type === "incomplete"` 체크 필요)
  4. Filter data source entries — https://developers.notion.com/reference/filter-data-source-entries
     - `last_edited_time` 증분 필터 형식: `{"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": "..."}}` (`property` 키 없이 사용)
  5. Append block children — https://developers.notion.com/reference/patch-block-children
     - `2026-03-11` 위치 지정 객체: `{"type": "start"}`, `{"type": "end"}`, `{"type": "after_block", "after_block": {"id": "<block_id>"}}`

---

## 3. 공통 규칙 준수 설계 요약

1. **0원 원칙**: Python 3 표준 라이브러리 전용(외부 유료 도구·유료 API·외부 패키지 의존성 없음).
2. **기본 읽기 전용**: 기존 DB·부모 페이지 수정 API 미구현 + 보호 ID/제목 검증 가드(`ReadOnlyGuardError`).
3. **비밀값 분리**: `NOTION_TOKEN`은 환경변수 또는 `.env`(gitignore 적용)에서만 로드, 출력·오류 메시지 마스킹.
4. **추측 금지**: 미확인 항목은 코드 주석·README·본 보고서에 모두 `'미확인'`으로 명시.
5. **등급 상향 금지**: `normalize_verify_grade()`는 원본이 `'확인됨'`인 경우만 유지하고 빈 값·미분류 값은 `'미확인'`으로 내려 표시.
6. **실패 투명 표시**: API 오류·빈 응답·권한 없음 발생 시 섹션 A에 `부분 실패`/`실패` 및 원인 표기, 실패한 DB의 섹션에는 이전 캐시 숫자를 표시하지 않음.

---

## 4. 뮤즈 실행 환경 인수인계 체크리스트 (토큰 전달 후)

1. `.env.example`을 `.env`로 복사하고 `NOTION_TOKEN` 입력 (`chmod 600 .env`)
2. `./kkultong-dash check-config`로 환경변수 인식 확인
3. `./kkultong-dash run --dry-run`으로 두 DB 조회·집계·스키마 일치 여부 1차 검증
4. `./kkultong-dash run`으로 `'📊 꿀통 대시보드 (자동 갱신)'` 최초 1회 생성 및 노션 UI 렌더링 확인
5. 섹션 E 마커 사이에 임의 텍스트 입력 후 `./kkultong-dash run` 재실행하여 수동 입력 보존 및 증분 동기화 확인
6. `crontab -e`에 매일 11:20·17:50 KST 스케줄 등록
