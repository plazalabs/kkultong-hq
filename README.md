# kkultong-hq — 꿀통 대시보드 P1 (MVP)

노션 DB(체험단 신청·당첨 관리 DB, 경쟁 기록 DB)를 읽기 전용으로 조회하여, `'천하제일 꿀통대회'` 하위의 `'📊 꿀통 대시보드 (자동 갱신)'` 페이지 1장을 자동 갱신하는 Python 3 CLI 도구입니다.

> **범위 안내**: 이 저장소는 **P1 (MVP)** 범위만 구현합니다. P2(팩트 장부) 및 P3(에이전트 성적표)는 P1 합격 후 별도 지시로 진행합니다.

---

## 1. 핵심 원칙 및 안전장치

1. **0원 원칙**: 외부 유료 서비스·유료 API·유료 호스팅을 사용하지 않으며, **Python 3 표준 라이브러리만** 사용합니다(`pip install` 불필요).
2. **기본 읽기 전용**: 노션에 쓰기를 수행하는 유일한 대상은 이 앱이 `'천하제일 꿀통대회'` 아래에 생성하는 `'📊 꿀통 대시보드 (자동 갱신)'` 페이지 본문입니다.
   - 기존 DB(체험단 DB, 경쟁 기록 DB)와 부모 페이지(`'천하제일 꿀통대회'`)는 보호 목록에 등록되어 쓰기/삭제가 코드 레벨에서 차단됩니다(`ReadOnlyGuardError`).
   - 기존 DB 속성/행이나 `담당` 선택지를 수정하는 API 메서드는 아예 구현하지 않았습니다.
3. **비밀값 관리**: `NOTION_TOKEN`은 실행 환경의 환경변수 또는 `.env` 파일에서만 읽습니다. `.env`는 `.gitignore`에 포함되어 있으며, 로그·오류 메시지·CLI 출력에서 토큰 값을 마스킹/제거합니다.
4. **등급 상향 금지**: 경쟁 기록 DB의 `확인` 등급은 절대 `'확인됨'`으로 올리지 않습니다. 원본에 `'확인됨'`이 있는 경우만 그대로 표시하고, 빈 값·미분류 값은 `'미확인'`으로 내려 표시합니다.
5. **실패 투명 표시**: API 오류(401/403/404/429/5xx), 빈 응답, 권한 없음 발생 시 **대시보드 섹션 A에 실패 원인을 명시**하고, 실패한 DB의 섹션에는 이전 캐시 숫자를 정상인 것처럼 표시하지 않습니다.

---

## 2. 실행 환경 및 설치 (Ubuntu, Python 3)

- 요구 사항: Python 3.9+ (Ubuntu 22.04/24.04 기본 `python3` 포함, 추가 패키지 설치 불필요)

```bash
git clone https://github.com/plazalabs/kkultong-hq.git
cd kkultong-hq
cp .env.example .env
chmod 600 .env
```

`.env` 파일을 열어 `NOTION_TOKEN`을 입력합니다 (토큰은 절대 Git에 커밋하지 마세요).

```dotenv
NOTION_TOKEN=ntn_실제_토큰_값
# 필요 시 부모 페이지 ID를 직접 지정할 수 있습니다 (미지정 시 제목 '천하제일 꿀통대회'로 검색)
# NOTION_PARENT_PAGE_ID=
```

### 노션 통합(Integration) 권한 및 공유 범위 준비
1. Notion Developer Portal에서 Internal Integration 생성 후 권한 설정:
   - Read content (필수 — DB 조회 및 대시보드 기존 블록 읽기)
   - Insert content (필수 — 대시보드 페이지 최초 생성 및 블록 작성)
   - Update content (필수 — 대시보드 페이지 내 자동 갱신 블록 삭제/교체)
2. 노션 워크스페이스에서 아래 대상에 통합 연결(`•••` → `Add connections`):
   - **체험단 신청·당첨 관리 DB** (`41a89944-f230-8091-9e54-000b1d8dea3c`)
   - **경쟁 기록 DB** (`41b89944-f230-814b-a322-000bfaf7e533`)
   - **천하제일 꿀통대회** 페이지 (대시보드 페이지의 부모)
   - *(주의)* 허브 운영 DB, 게시판, 규정 등 불필요한 페이지는 공유하지 않아도 됩니다.

---

## 3. 실행 방법

```bash
# 1) 설정 점검 (토큰 값은 마스킹되어 출력되지 않음)
./kkultong-dash check-config

# 2) 즉시 갱신 (기본: 증분 동기화, 하루 1회 KST 첫 실행 시 자동으로 전체 재동기화)
./kkultong-dash run

# 3) 강제 전체 재동기화
./kkultong-dash run --full

# 4) 드라이런 (노션 DB 읽기·집계만 수행하고 노션 페이지는 수정하지 않음)
./kkultong-dash run --dry-run
```

### 종료 코드(Exit Code)
- `0`: 정상 (`실행 결과: 정상`)
- `2`: 부분 실패 (2개 DB 중 1개 실패 — 대시보드 섹션 A에 실패 원인을 기록하고 실패 DB 섹션은 이전 숫자 미표시)
- `1`: 전체 실패 (설정 오류, 두 DB 모두 실패, 또는 대시보드 페이지 접근/쓰기 실패)

---

## 4. 정기 실행 (cron 등록 예시 — 매일 11:20 · 17:50 KST)

Ubuntu 서버에서 `crontab -e`로 등록합니다. 로그 디렉터리는 미리 생성하세요(`mkdir -p /path/to/kkultong-hq/logs`).

```cron
# 시스템 타임존과 무관하게 한국 표준시(KST) 기준으로 11:20, 17:50 실행
CRON_TZ=Asia/Seoul
20 11 * * * cd /path/to/kkultong-hq && ./kkultong-dash run >> /path/to/kkultong-hq/logs/cron.log 2>&1
50 17 * * * cd /path/to/kkultong-hq && ./kkultong-dash run >> /path/to/kkultong-hq/logs/cron.log 2>&1
```

> **참고**: 사용하는 cron 데몬이 `CRON_TZ`를 지원하지 않고 서버 타임존이 UTC라면 `20 2 * * *`(11:20 KST) 및 `50 8 * * *`(17:50 KST)로 등록하세요. (`timedatectl`로 서버 타임존 확인 가능)

---

## 5. 공식 문서로 확인한 Notion API 버전 및 요청 한도

### 5.1 사용 API 버전
- **적용 `Notion-Version` 헤더**: **`2026-03-11`**
- 공식 문서 근거:
  - Versioning: https://developers.notion.com/reference/versioning
    - 원문: *"The Notion-Version header must be included in all REST API requests. ... The most recent Notion-Version is `2026-03-11`."*
  - Changes by version: https://developers.notion.com/reference/changes-by-version
    - `2025-09-03` 원문: *"`/v1/databases` APIs are re-organized into `/v1/data_sources` (for managing individual data sources under a database container) and `/v1/databases` (for managing the database container.)"*
    - `2026-03-11` 원문: *"The `after` parameter on Append Block Children is replaced by a `position` object. The `archived` field is removed in favor of `in_trash` across all request parameters and response bodies."*
  - Query a data source: https://developers.notion.com/reference/query-a-data-source (`POST /v1/data_sources/{data_source_id}/query`)
  - Append block children: https://developers.notion.com/reference/patch-block-children (`position: {"type": "start" | "end" | "after_block"}`)

### 5.2 요청 한도 및 재시도 정책
- 공식 문서 URL: https://developers.notion.com/reference/request-limits
- 원문 요약:
  - **연결당 한도(Per-connection limit)**:
    - Business and Enterprise: *"600 requests per minute (an average of 10 per second)"*
    - All other plans: *"180 requests per minute (an average of 3 per second)"*
  - **데이터 소스 쿼리 별도 한도**:
    - *"Querying a data source has a separate limit of about 3 requests per second per connection. A request over this limit returns an HTTP 429 response with a Retry-After header."*
  - **워크스페이스 공유 한도(Per-workspace limit)**:
    - *"A separate limit is shared across all of the workspace's connections and scaled to the workspace's plan. ... Retry-After for this limit can be longer than a minute."*
  - **429 / 529 재시도 규칙**:
    - *"Connections should handle HTTP 429 and 529 responses and respect the Retry-After response header. The header value is an integer number of seconds."*
    - *"Do not retry every error. Retry 429 and 529 responses, except a 429 with `public_api_request_blocked`."*
  - **요청 크기 한도**:
    - `rich_text.text.content` 최대 2,000자, 블록/rich_text 배열 최대 100개, 단일 페이로드 최대 1,000 블록 요소 및 500KB.
- **코드 반영 방식**:
  - 워크스페이스 요금제가 미확인이므로 보수적으로 요청 간 최소 `0.35초` 간격(초당 약 2.85회)을 유지합니다.
  - HTTP `429`/`529` 수신 시 `Retry-After` 헤더(없으면 `additional_data.retry_after`, 없으면 지수 백오프) + 지터(0~0.25초) 대기 후 최대 6회 시도합니다.
  - `public_api_request_blocked` 429와 쓰기 요청의 503은 재시도하지 않고 즉시 오류로 표면화합니다.

---

## 6. 주요 설계 결정

### 6.1 대시보드 페이지 식별 방법 (중복 생성 방지 + 기존 페이지 보호)
실행할 때마다 새 페이지를 만들지 않고 아래 **4단계 우선순위**로 기존 `'📊 꿀통 대시보드 (자동 갱신)'` 페이지를 찾습니다.

1. **환경변수 `NOTION_DASHBOARD_PAGE_ID`** 지정 시: `GET /v1/pages/{id}`로 존재·미삭제·제목 정확 일치를 검증 후 사용.
2. **로컬 SQLite `app_state.dashboard_page_id`**: 이전 실행에서 저장한 ID를 `GET /v1/pages/{id}`로 존재·미삭제·부모 일치·제목 일치 검증 후 사용(검색 API 색인 지연 없음).
3. **부모 페이지(`'천하제일 꿀통대회'`) 1단계 하위 `child_page` 직접 스캔** (`GET /v1/blocks/{parent_id}/children`): 검색 API보다 강한 일관성을 보장하며, 부모 페이지 범위를 벗어난 페이지를 건드리지 않습니다.
4. **최초 1회 생성**: 위 1~3단계에서 찾지 못한 경우에만 `POST /v1/pages`로 생성하고 ID를 SQLite에 저장합니다.

> **안전 가드**: 대상 페이지의 제목이 `'📊 꿀통 대시보드 (자동 갱신)'`과 다르거나 부모 페이지 ID와 같으면 `ReadOnlyGuardError`를 발생시켜 기존 페이지 덮어쓰기를 원천 차단합니다.

### 6.2 섹션 E(수동 입력 칸) 보존 방법 — 마커 기반 인플레이스(In-Place) 보존
- 최초 생성 시 섹션 E 본문을 아래 두 마커 블록으로 감쌉니다.
  - 시작 마커: `[KKULTONG-MANUAL-E:START]`
  - 끝 마커: `[KKULTONG-MANUAL-E:END]`
- **갱신 시 동작**:
  1. 시작 마커~끝 마커 사이의 블록(사용자가 노션 UI에서 직접 입력한 티스토리·애드센스·쿠팡 파트너스 상태 및 중첩 블록·서식·표·링크 등)은 **삭제하지도, 다시 쓰지도 않고 페이지 안에 그대로 유지**합니다.
  2. 마커 바깥쪽의 자동 갱신 블록(상단 A~D + E 제목, 하단 F~G)만 삭제합니다.
  3. Notion API `2026-03-11`의 `position` 객체로 상단 블록은 `{"type": "start"}`(+ 후속 배치 `after_block` 체이닝)으로 마커 앞에 삽입하고, 하단 블록은 `{"type": "end"}`로 마커 뒤에 추가합니다.
  4. **안전 백업 및 마커 복구**: 매 갱신 전 보존 구간의 블록 JSON을 로컬 SQLite(`manual_section_snapshots`)에 백업합니다. 사용자가 실수로 끝 마커나 양쪽 마커를 지운 경우에도 `F.` 제목 직전까지의 수동 내용을 보존한 채 마커를 자동 복구하고 섹션 A에 경고를 남깁니다.

### 6.3 SQLite 로컬 캐시 및 일일 전체 재동기화
- 저장 위치: `./data/kkultong_dash.db` (`KKULTONG_DB_PATH`로 변경 가능, `.gitignore` 대상)
- **증분 동기화**: 같은 KST 날짜 내 재실행 시 `last_edited_time >= (이전 최대 시각 - 1분 겹침)` 필터로 변경된 행만 조회해 SQLite를 갱신합니다.
- **일일 1회 전체 재동기화**: KST 날짜가 바뀌어 그날 처음 실행될 때(또는 `--full` 지정 시) 필터 없이 전체 행을 다시 조회합니다. 노션에서 행이 삭제/보관되어 기존 캐시 행 수와 다르면 **섹션 A에 경고**를 표시하고 캐시를 전체 조회 기준으로 재정렬합니다.

---

## 7. 대시보드 출력 구성 (섹션 A ~ G)

| 섹션 | 내용 | 비고 |
|---|---|---|
| **A. 실행 상태 (머리줄)** | 갱신 시각(KST 초 단위), 실행 결과(`정상`/`부분 실패`/`실패`), 실패 원인, 경고 | API 오류·빈 응답·권한 없음·일일 행 수 불일치 표시 |
| **B. 마감 임박** | 상태=`당첨`·`리뷰대기`, `리뷰마감일` 있는 행 D-day 오름차순(D-3 이하 빨간색) + 별도 `마감일 미입력` 표 | KST 날짜 기준 D-day 계산 |
| **C. 체험단 현황** | 전체 행 수, 상태별 행 수, 사이트별 신청·당첨 수 및 당첨률(`(당첨+리뷰대기+리뷰완료) ÷ 발표 완료 행`) | 모든 당첨률 옆에 `n=○` 필수 표기 |
| **D. 현물 실적** | 상태=`당첨`·`리뷰대기`·`리뷰완료` 행의 캠페인명·제공내역 | **제공내역 원문 그대로**, 금액 추정·합산 금지 |
| **E. 수동 입력 칸** | 티스토리·애드센스·쿠팡 파트너스 상태 | 마커 구간 인플레이스 무손실 보존 |
| **F. 경쟁 기록 요약** | 최근 24시간 새 행 수(`created_time` 기준, 담당별), 담당 × 구분 교차표, 담당별 확인 등급 분포 | 등급 상향 금지(빈 값→`미확인`), 분모=`해당없음` 제외 `n` 표기 |
| **G. 정산** | `정산: 수동 확인` 고정 표시 | 정산판 위치 미확인 (P1 수동 확인) |

---

## 8. 테스트 실행

```bash
python3 -m unittest discover -s tests -v
```

> **주의**: 저장소의 테스트는 노션 API를 모의(Mock) HTTP 트랜스포트로 대체한 정적·단위·통합 테스트입니다. 실제 노션 API 호출 검증은 `NOTION_TOKEN` 전달 후 뮤즈의 Ubuntu 환경에서 첫 실행 시 수행해야 합니다.

---

## 9. 미확인 사항 (실호출 전 확인 필요)

- **토큰 및 통합 공유 범위**: `대기 중 — 토큰 미전달`
- **정산판 위치·스키마**: `미확인` (섹션 G에 `정산: 수동 확인` 표시)
- **워크스페이스 요금제 등급**: `미확인` (일반 요금제 기준 초당 3회 미만으로 보수적 페이싱 적용)
- **`'천하제일 꿀통대회'` 부모 페이지 ID**: `미확인` (기본 제목 검색 + `.env` `NOTION_PARENT_PAGE_ID` 지정 지원)
- **`collection://` UUID의 REST `/v1/data_sources/{id}/query` 실응답**: `미확인` (첫 실호출 시 검증 필요, 환경변수로 ID 교체 가능)
- **실제 노션 UI 렌더링 및 `position: {"type": "start"}` 실동작**: 공식 문서(`2026-03-11`) 기준 구현 완료, 실호출 동작은 토큰 전달 후 첫 실행 시 검증 필요
