# kkultong-hq — 꿀통 대시보드 (P1 + P2 팩트 장부 + P3 에이전트 성적표)

노션 DB(체험단 신청·당첨 관리 DB, 경쟁 기록 DB)와 정산판, 신규 DB `'📒 팩트 장부'`를 읽기 전용으로 조회하여 `'천하제일 꿀통대회'` 하위의 `'📊 꿀통 대시보드 (자동 갱신)'` 페이지 1장을 자동 갱신(섹션 A~H)하고, `'📒 팩트 장부'`의 멱등 생성·시드 입력·자동 재검증 및 충돌 판정을 수행하는 Python 3 CLI 도구입니다.

> **범위 안내**: 이 저장소는 **P1 (대시보드 MVP)**, **P2 (팩트 장부 + 자동 재검증)**, **P3 (대시보드 섹션 H — 팩트 장부 기준 에이전트 성적표)** 범위를 구현합니다.

---

## 1. 핵심 원칙 및 안전장치

1. **0원 원칙**: 외부 유료 서비스·유료 API·유료 호스팅을 사용하지 않으며, **Python 3 표준 라이브러리만** 사용합니다(`pip install` 불필요).
2. **쓰기 대상 엄격 제한**: 노션에 쓰기를 수행하는 대상은 이 앱이 `'천하제일 꿀통대회'` 아래에 생성하는 **대시보드 페이지(`'📊 꿀통 대시보드 (자동 갱신)'`)** 와 **팩트 장부 DB(`'📒 팩트 장부'`)** 두 곳뿐입니다.
   - 기존 DB/페이지(체험단 DB, 경쟁 기록 DB, 부모 페이지 `'천하제일 꿀통대회'`, 정산판, 게시판, 허브)는 보호 목록에 등록되어 쓰기/삭제가 코드 레벨에서 차단됩니다(`ReadOnlyGuardError`).
   - 기존 DB 속성/행이나 `담당` 선택지를 수정하는 API 메서드는 아예 구현하지 않았습니다.
3. **비밀값 관리**: `NOTION_TOKEN`은 실행 환경의 환경변수 또는 `.env` 파일에서만 읽습니다. `.env`는 `.gitignore`에 포함되어 있으며, 로그·오류 메시지·CLI 출력에서 토큰 값을 마스킹/제거합니다.
4. **등급 상향 절대 금지**: 앱은 어떤 경로에서도 등급을 `'확인됨'`으로 올리지 않습니다(내리기와 표시만 수행).
5. **실패 투명 표시**: API 오류(401/403/404/429/5xx), 빈 응답, 권한 없음 발생 시 **대시보드 섹션 A에 실패 원인을 명시**하고, 실패한 DB의 섹션에는 이전 캐시 숫자를 정상인 것처럼 표시하지 않습니다.

---

## 2. 실행 환경, 저장소 경로 및 설치 (Ubuntu, Python 3)

### 저장소 및 폴더 경로 (섹션 8-3)
- **원본 저장소**: `github.com/plazalabs/kkultong-hq` (https://github.com/plazalabs/kkultong-hq)
- **뮤즈 로컬 클론 경로**: `~/workspace/work/kkultong-hq-local`

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

# 2) 대시보드 즉시 갱신 (기본: 증분 동기화, 하루 1회 KST 첫 실행 시 자동으로 전체 재동기화)
./kkultong-dash run

# 3) 강제 전체 재동기화
./kkultong-dash run --full

# 4) 드라이런 (노션 DB 읽기·집계만 수행하고 노션 페이지는 수정하지 않음)
./kkultong-dash run --dry-run

# 5) [P2] '📒 팩트 장부' DB 멱등 생성 (이미 존재하면 재생성하지 않고 재사용)
./kkultong-dash fact-init

# 6) [P2] '📒 팩트 장부' 자동 재검증 + 충돌 판정 (스케줄 등록은 뮤즈 담당)
./kkultong-dash fact-revalidate
./kkultong-dash fact-revalidate --dry-run
./kkultong-dash fact-revalidate --update-dashboard

# 7) [P2] v1·v2 노션 페이지에서 플랫폼 수치 시드 입력 (페이지 ID 또는 제목을 인자로 전달)
./kkultong-dash fact-seed \
  --v1-page "C 갈래 영어 시범 — 작업 분할 v1" \
  --v2-page "원소스 멀티유즈 워크플로우 v2" \
  --dry-run
```

### 종료 코드(Exit Code)
- `0`: 정상 (`실행 결과: 정상`)
- `2`: 부분 실패 (2개 DB 중 1개 실패 — 대시보드 섹션 A에 실패 원인을 기록하고 실패 DB 섹션은 이전 숫자 미표시)
- `1`: 전체 실패 (설정 오류, 두 DB 모두 실패, 대시보드 페이지 접근/쓰기 실패, 또는 시드 원본 페이지 접근 실패)

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
  2. 마커 바깥쪽의 자동 갱신 블록(상단 A~D + E 제목, 하단 F~H)만 삭제합니다.
  3. Notion API `2026-03-11`의 `position` 객체로 상단 블록은 `{"type": "start"}`(+ 후속 배치 `after_block` 체이닝)으로 마커 앞에 삽입하고, 하단 블록은 `{"type": "end"}`로 마커 뒤에 추가합니다.
  4. **안전 백업 및 마커 복구**: 매 갱신 전 보존 구간의 블록 JSON을 로컬 SQLite(`manual_section_snapshots`)에 백업합니다. 사용자가 실수로 끝 마커나 양쪽 마커를 지운 경우에도 `F.` 제목 직전까지의 수동 내용을 보존한 채 마커를 자동 복구하고 섹션 A에 경고를 남깁니다.

### 6.3 SQLite 로컬 캐시 및 일일 전체 재동기화
- 저장 위치: `./data/kkultong_dash.db` (`KKULTONG_DB_PATH`로 변경 가능, `.gitignore` 대상)
- **증분 동기화**: 같은 KST 날짜 내 재실행 시 `last_edited_time >= (이전 최대 시각 - 1분 겹침)` 필터로 변경된 행만 조회해 SQLite를 갱신합니다.
- **일일 1회 전체 재동기화**: KST 날짜가 바뀌어 그날 처음 실행될 때(또는 `--full` 지정 시) 필터 없이 전체 행을 다시 조회합니다. 노션에서 행이 삭제/보관되어 기존 캐시 행 수와 다르면 **섹션 A에 경고**를 표시하고 캐시를 전체 조회 기준으로 재정렬합니다.

### 6.4 P2.5 팩트 장부 재검증 확장 (Zendesk JSON API 경로·문서 갱신 감시·등급 이력 스냅샷)
- **Zendesk 공개 JSON API fetch 경로**: `help.medium.com` 및 `support.patreon.com`의 글 URL(`/hc/<locale>/articles/<글번호>[-<슬러그>]`)은 실행 환경에서 HTML 403을 반환하므로 `https://<host>/api/v2/help_center/<locale>/articles/<글번호>.json`으로 변환해 조회하고, 응답의 `article.body`(HTML)를 가시 텍스트로 변환해 발췌 대조 대상으로 사용합니다. 그 외 도메인은 기존 HTML fetch 경로를 그대로 유지합니다.
- **문서 갱신 감시 (`fact_source_doc_state`)**: Zendesk JSON 응답의 `article.updated_at`을 행별로 로컬 SQLite에 저장해 두고, 재검증 시 직전 실행 값과 달라졌으면 자동 재검증 결과(`발췌 그대로` 등)와 별개로 `'원문 갱신됨'` 표시를 남깁니다.
- **등급 이력 스냅샷 (`fact_grade_history`)**: 재검증 실행마다 전체 행의 `(page_id, grade, auto_revalidation_result, run_at_kst)`를 로컬 SQLite의 이력 테이블에 누적 기록합니다(대시보드 미표시, `'자동 상향 0건'` 등 사후 독립 검증 목적).

---

## 7. 대시보드 출력 구성 (섹션 A ~ H)

| 섹션 | 내용 | 비고 |
|---|---|---|
| **A. 실행 상태 (머리줄)** | 갱신 시각(KST 초 단위), **다음 예정 갱신 시각**(11:20/17:50 중 가까운 시각), 실행 결과(`정상`/`부분 실패`/`실패`), 실패 원인, 경고, **팩트 경보** | API 오류·빈 응답·권한 없음·일일 행 수 불일치·팩트 장부 경보 표시 |
| **B. 마감 임박** | 상태=`당첨`·`리뷰대기`(본인 구매 리뷰 전체 제외), `리뷰마감일` 있는 행 D-day 오름차순(D-3 이하 빨간색) + 별도 `마감일 미입력` 표 | KST 날짜 기준 D-day 계산, 본인 구매 리뷰는 `쿠팡 리뷰 점수 작업`에만 표시 |
| **C. 체험단 현황** | 전체 행 수, 상태별 행 수, 사이트별 신청·당첨 수 및 당첨률(`(당첨+리뷰대기+리뷰완료) ÷ 발표 완료 행`) | 모든 당첨률 옆에 `n=○` 필수 표기, 합계 행에 `(본인 구매 리뷰 N건 제외)` 실측 건수 병기 |
| **D. 현물 실적** | 상태=`당첨`·`리뷰대기`·`리뷰완료` 행의 캠페인명·제공내역 + 별도 `쿠팡 리뷰 점수 작업` 표 | **제공내역 원문 그대로**, 금액 추정·합산 금지 |
| **E. 수동 입력 칸** | 티스토리·애드센스·쿠팡 파트너스 상태 | 마커 구간 인플레이스 무손실 보존 |
| **F. 경쟁 기록 요약** | 최근 24시간 새 행 수(`created_time` 기준, 담당별), 담당 × 구분 교차표, 담당별 확인 등급 분포 | 경고 콜아웃 + `빈 값` 별도 집계, 등급 상향 금지, 분모=`해당없음` 제외 `n` 표기 |
| **G. 정산** | `'📊 일별·월별 수익 통계 & 실행 체크리스트'` 링크 + 구조화(DB/표) 시 원문 핵심 요약, 비구조화/조회 불가 시 `정산: 수동 확인` 유지 | 숫자 추측 금지, `NOTION_SETTLEMENT_PAGE_ID`로 페이지 ID 설정 가능 |
| **H. 에이전트 성적표 (팩트 장부 기준)** | 대상 에이전트 5명(`뮤즈`·`피클`·`큐`·`인스팅트`·`클실장`)의 올린 팩트 수·유지율·정정당한 수·잡아낸 수·자동 경보 수 | **오직 📒 팩트 장부만 사용**(경쟁 기록 DB 미사용), 숫자만 표기(순위·서열·우열·판정 문구 금지), 유지율 분모 5 미만 시 `'표본 부족'` 병기 |

### 7.1 섹션 H (`H. 에이전트 성적표 (팩트 장부 기준)`) 집계 정의 및 표기 규칙

- **데이터 출처**: 오직 **`📒 팩트 장부`** DB만 읽기 전용으로 조회합니다. 경쟁 기록 DB의 자기 신고 등급이나 이전 캐시로 대체하지 않습니다.
- **대상 에이전트 (고정 순서)**: `뮤즈`, `피클`, `큐`, `인스팅트`, `클실장` (수치 기준 정렬·순위화 금지)
- **표기 규칙**:
  - 표에는 숫자(및 조건부 `'표본 부족'`, `'집계 불가'`, `'회장님 행 N'` 괄호 분리 표기)만 표시하며, 순위·서열·우열·판정 문구 및 성과 기반 색상 강조를 넣지 않습니다.
  - **표본 부족 (P2.5 변경)**: `올린 팩트 수`가 아니라 **유지율의 분모(`유지 중 + 내려간 행`) `< 5`**인 에이전트는 유지율 칸에 `'표본 부족'`을 병기합니다(예: `75.0% (3/4, 표본 부족)`, `집계 불가 (표본 부족)`).
    - *변경 근거*: 올린 팩트 수가 5 이상이더라도 대부분이 `'미확인'` 등 미평가 행이면 실제 유지율 계산 표본(분모)이 극소수일 수 있으므로, 유지율 분모를 기준으로 표본 부족을 표시합니다.
- **지표 5개 집계 정의 (장부 현재 필드 범위 내, P2.5 확정 판정 3건 반영)**:
  1. **올린 팩트 수**: `'올린 이'`가 해당 에이전트인 행 수
  2. **유지율**: 해당 에이전트가 올린 행 중 `유지 중인 행 ÷ (유지 중인 행 + 내려간 행)`
     - **유지 중인 행**: 현재 `등급 == '확인됨'`이고 `'대체됨'` relation이 비어 있는 행
     - **내려간 행 (장부 현재 필드 기준, P2.5 변경)**: `등급 == '자동확인불가'`를 제외하고 아래 세 조건 중 하나에 해당하는 행
       1. `등급 == '불일치'` (P2 충돌 탐지가 `'확인됨'` 행만 `'불일치'`로 하향하며, 수동 `'불일치'` 지정도 확인 유지 탈락 행으로 포함)
       2. `등급 == '원문대조필요'` 이고 `자동 재검증 결과 == '발췌 사라짐'` (P2 자동 재검증이 원문 발췌 소실로 하향한 행)
       3. `'대체됨'` relation이 1건 이상 연결된 행 (*P2.5 변경 근거*: 정본 정리·정정 과정에서 다른 행으로 대체된 행은 더 이상 유지 중인 정본이 아니므로 `'대체됨'` 관계만 있는 행도 내려간 행에 포함)
     - **제외 항목**: `등급 == '자동확인불가'`(`로그인 필요`, P2 팩트 경보 비대상 — 기존대로 제외), `'대체됨'`이 비어 있는 `등급 == '미확인'`, `'대체됨'`이 비어 있고 `발췌 사라짐` 외 결과(`확인 불가`·`열림 실패`·`발췌 그대로`·미검증)인 `'원문대조필요'` 행은 제외합니다.
     - **집계 불가 조건**: 분모(`유지 중 + 내려간 행`)가 `0`이거나, 스키마에서 필수 속성(`올린 이`·`등급`·`자동 재검증 결과`·`대체됨`)이 누락된 경우 `'집계 불가'`로 표시합니다(분모가 `0`으로 계산된 경우 `0 < 5`이므로 `'집계 불가 (표본 부족)'`, 스키마 누락으로 분모 자체를 계산할 수 없는 경우 `'집계 불가'`).
  3. **정정당한 수**: 해당 에이전트가 올린 행 중 `'정정한 이'`가 비어 있지 않고 `'올린 이'`와 다른 행 수 (본인 정정 제외, `'회장님'` 등 타인이 정정한 경우 포함)
  4. **잡아낸 수 (P2.5 변경)**: `'정정한 이'`가 해당 에이전트이고 `'올린 이'`가 다른 대상 에이전트(`뮤즈`·`피클`·`큐`·`인스팅트`·`클실장` 중 본인 제외) 또는 **`'회장님'`**인 행 수 (`'올린 이'` 미입력, 본인 정정, 대상 외 타인은 제외). `'회장님'`이 올린 행을 정정한 건이 1건 이상이면 대시보드 표기에서 괄호로 분리 표시합니다(예: `3 (회장님 행 1)`).
     - *변경 근거*: `'회장님'`이 올린 행의 오류를 에이전트가 바로잡은 기여도 집계에 포함하되, 에이전트 간 상호 정정과 구분해 읽을 수 있도록 괄호로 분리합니다.
  5. **자동 경보 수**: 해당 에이전트가 올린 행 중 자동 재검증으로 등급이 내려간 행 수
     - **집계 기준 (장부 현재 필드 기준)**:
       1. `등급 == '원문대조필요'` 이고 `자동 재검증 결과 == '발췌 사라짐'`
       2. `등급 == '불일치'` 이고 `'충돌'` relation이 1건 이상 연결됨
     - **제외 및 집계 불가 조건**: `등급 == '자동확인불가'`(`로그인 필요`, P2 경보 비대상), 현재 `등급 == '확인됨'` 행, `'충돌'` relation이 비어 있는 `'불일치'` 행, 수동 `'대체됨'` relation만 연결된 행은 제외합니다. 스키마에서 필수 속성(`올린 이`·`등급`·`자동 재검증 결과`·`충돌`)이 누락된 경우 `'집계 불가'`로 표시합니다.
- **현재 필드 범위의 알려진 한계**: `'📒 팩트 장부'` 자체에는 `'이전 등급'` 이력 필드가 없으므로, 처음부터 `'원문대조필요'`에 원문 발췌가 채워져 있던 행이 `'발췌 사라짐'`을 받은 경우와 `'확인됨' → '원문대조필요'` 하향을 노션 필드만으로 완전히 구분할 수 없습니다. (P2.5부터 재검증 실행 시마다 로컬 SQLite `fact_grade_history`에 전체 행 스냅샷을 누적 기록하여 사후 독립 검증이 가능합니다.)

---

## 8. 테스트 실행

```bash
python3 -m unittest discover -s tests -v
```

> **주의**: 저장소의 테스트는 노션 API 및 외부 웹 페이지를 모의(Mock) 픽스처 HTTP 트랜스포트로 대체한 정적·단위·통합 테스트입니다. 실제 노션 API 호출 검증(`fact-init`, `fact-seed`, `fact-revalidate`, `run`)은 뮤즈의 Ubuntu 환경(`~/workspace/work/kkultong-hq-local`)에서 런처로 수행합니다.

---

## 9. 런타임 확인 사항 (뮤즈 실행 시 확인)

- **정산판 페이지 구조 (`41a89944-f230-817a-9849-d34d51978411`)**: 실행 시점에 하위 DB/표 존재 여부를 자동 판별하여 구조화 요약 또는 링크+`수동 확인`으로 분기 렌더링함.
- **P2 팩트 장부 실실행**: `fact-init`(DB 최초 1회 생성), `fact-seed`(v1/v2 페이지 ID·제목 인자 전달), `fact-revalidate`(새벽 1회 스케줄 등록은 뮤즈 담당)는 뮤즈 환경에서 실토큰으로 실행·교차 확인함.
- **워크스페이스 요금제 등급**: `미확인` (일반 요금제 기준 초당 3회 미만으로 보수적 페이싱 적용).
