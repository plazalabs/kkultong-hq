# 📒 팩트 장부 중복 주제키 정리안 (P2.5 — 충돌 후보 7개 주제키 23행)

- 작성일: 2026-11-22
- 상태: **제안 초안 (실행 안 함 — 회장님 승인 뒤 별도 작업)**
- 대상 범위: 충돌 후보 7개 주제키, 총 **23행**
  - `medium.membership` (5행)
  - `medium.payment_fee` (4행)
  - `naver_premium.fee` (5행)
  - `patreon.fee` (3행)
  - `patreon.payout` (2행)
  - `gumroad.fee` (2행)
  - `gumroad.payout` (2행)

> [!IMPORTANT]
> **미확인(유보) 전제**: 본 원격 세션에는 `NOTION_TOKEN`이 제공되지 않아 Notion `'📒 팩트 장부'` 39행의 실제 `주장` 원문(앞 30자), 개별 `값`, `올린 이`, `page_id`를 직접 조회하지 못했습니다. 추측·생성 금지 원칙에 따라 아래 행 단위 표의 `claim_prefix(주장 앞 30자)` 칸은 **`미확인 (뮤즈 환경에서 실장부 조회 후 매핑)`**으로 유보했습니다. 반면 각 주제키의 **현행 공식 문서 기준 정본 값·근거 URL·원문 발췌**는 2026-11-22 본 세션에서 직접 조회·검증한 사실을 기반으로 작성했습니다.

---

## 1. 정리 원칙 및 성적표 연동 영향

1. **정본 선정 우선순위**
   1. 2026-11-22 기준 현행 공식 문서 원문이 값을 직접 뒷받침하는 행
   2. 근거 URL과 원문 발췌가 온전하게 채워질 수 있는 행 (동일 조건이면 가장 구체적이고 최신인 행 1건을 정본으로 유지)
2. **`'대체됨'` 연결 대상**
   - 현행 공식 문서와 모순되는 구 정책/오류 값 행 (예: Medium `'멤버십 불필요'`, 네이버 플랫폼 수수료 `8%` 외 수치, Gumroad 구 최소 지급 `$10` 등)
   - 정본 행과 사실상 동일한 내용을 중복·축약 기재한 행
   - 정본이 아닌 행의 `'대체됨'` relation을 정본 행으로 연결한다(행 삭제 금지 — 감사 이력 보존).
3. **하위 주제키 분리 검토 (유보 조건)**
   - `fact-seed` 자동 분류 특성상 같은 주제키에 **서로 다른 차원의 사실**(예: `medium.membership` 안의 "가입 시 멤버십 필수 여부" vs "멤버십 월 구독료 \$5", `gumroad.fee` 안의 "직접 판매 10%+\$0.50" vs "Discover 30%")이 함께 묶였을 가능성이 있다.
   - 뮤즈가 실장부 23행의 `주장` 원문을 대조할 때 **실제 모순/중복**이면 `'대체됨'`으로 연결하고, **서로 다른 지표가 한 주제키에 혼재**된 경우에는 `'대체됨'` 대신 하위 주제키 분리(예: `medium.membership.required` / `medium.membership.price`)로 처리할지 회장님 승인을 받아 확정한다.
4. **대시보드·성적표 반영 효과 (참고)**
   - `'대체됨'` relation이 연결된 행은 `detect_fact_conflict_candidates()`에서 자동으로 충돌 후보 경보 대상에서 제외된다.
   - P2.5 작업 3의 성적표 정의 변경에 따라 `'대체됨'`이 연결된 행은 유지율 분모의 `'내려간 행'`에 포함된다(`자동확인불가` 제외).

---

## 2. 충돌 후보 7개 주제키 요약 (23행)

| # | 주제키 | 대상 행 수 | 공식 문서 기준 정본 값 (2026-11-22 직접 확인) | 정본 근거 공식 URL |
|---|---|---:|---|---|
| 1 | `medium.membership` | 5 | **MPP 가입 시 활성 Medium 멤버십 필요** (요금 별도 행 존재 시 `$5/month or $50/year`) | `https://help.medium.com/hc/en-us/articles/39121627791639-Medium-Partner-Program-eligibility` (및 `115011694187`, 요금은 `115004545567`) |
| 2 | `medium.payment_fee` | 4 | **Stripe 무료 계정 연동(제3자 지급 처리사), Medium 자체 결제 수수료율(%) 공식 표기 없음** (미국 원천징수는 W-8/조세조약 기준) | `https://help.medium.com/hc/en-us/articles/360003928833-Set-up-payouts-with-Stripe` (지급은 `25267297935895`, 세금은 `360022773274`) |
| 3 | `naver_premium.fee` | 5 | **플랫폼 수수료 8%** (결제 수수료 2.4%, 부가세 포함 2.64%는 `naver_premium.payment_fee`에 별도 존재) | `https://naver-premiumcontents.gitbook.io/guide/chapter2/page13` |
| 4 | `patreon.fee` | 3 | **신규 창작자 표준 플랫폼 수수료 10%** (2025-08-04 이후 개설 기준; 결제 처리 수수료 별도 `2.9%+$0.30` / 비미국 PayPal `3.9%+$0.30`) | `https://support.patreon.com/hc/en-us/articles/11111747095181-Creator-fees-overview` (상세 `36426991446797`) |
| 5 | `patreon.payout` | 2 | **미국 외 창작자 Payoneer 은행송금 최소 \$10** (현지통화 약 \$0.50 정액 / USD 교차통화 `1.55%+$0.25`, 수단 변경 시 5일 보류) | `https://support.patreon.com/hc/en-us/articles/39694936541965-Payouts-guide-for-creators-outside-of-the-US` (및 `208656246`) |
| 6 | `gumroad.fee` | 2 | **직접 판매 `10% + $0.50`** (+ sales tax, 카드 처리 `2.9%+$0.30` 별도) / **Discover `30%`** (처리 수수료 포함) | `https://gumroad.com/pricing` 및 `https://gumroad.com/help/article/66-gumroads-fees` |
| 7 | `gumroad.payout` | 2 | **표준 최소 잔액 \$100 USD** (한국 KRW 계좌 `min 40,000` KRW, 수요일 지급, 주간/월간/분기 정산 7일 보류) | `https://gumroad.com/help/article/13-getting-paid` |
| **합계** | **7개 주제키** | **23** | | |

---

## 3. 주제키별 상세 정리안 (행 단위 제안)

### 3.1 `medium.membership` (5행)

- **공식 문서 실측 근거 (2026-11-22)**
  1. `Medium Partner Program eligibility` (`39121627791639`, `updated_at: 2026-11-11T16:29:41Z`):
     > *"Before enrolling in the Medium Partner Program, make sure you meet all of the following requirements: You are a Medium member with an active membership subscription."*
  2. `Enroll in the Medium Partner Program` (`115011694187`, `updated_at: 2026-11-11T19:31:54Z`):
     > *"Before you enroll, make sure you meet all the requirements in Medium Partner Program eligibility, including an active Medium membership."*
  3. `Become a Medium Member` (`115004545567`, `updated_at: 2026-11-11T19:46:21Z`):
     > *"Medium Member \$5/month or \$50/year"* / *"Friend of Medium \$15/month or \$150/year"*
- **정본 판정**: 공식 문서 2건 모두 활성 멤버십을 필수로 요구하므로 **'멤버십 필요'가 정본**이다. `'멤버십 불필요'`를 주장하는 행은 정본이 될 수 없다.
- **행 단위 정리 제안 (5행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 1 | `medium.membership` | `미확인 (뮤즈 실장부 대조 후 기입)` | MPP 가입 시 **'멤버십 필요'** (활성 멤버십 필수) 대표 1행 | **정본 유지** | 근거 URL `39121627791639`, 발췌 채움 |
| 2 | `medium.membership` | `미확인 (뮤즈 실장부 대조 후 기입)` | MPP 가입 시 **'멤버십 불필요'** 값 행 (1) | **`'대체됨'` 연결 + 하향** | #1 정본으로 `'대체됨'` 연결 (공식 출처 없음, 발췌 미생성) |
| 3 | `medium.membership` | `미확인 (뮤즈 실장부 대조 후 기입)` | MPP 가입 시 **'멤버십 불필요'** 값 행 (2) 또는 구 정책 행 | **`'대체됨'` 연결 + 하향** | #1 정본으로 `'대체됨'` 연결 (공식 출처 없음, 발췌 미생성) |
| 4 | `medium.membership` | `미확인 (뮤즈 실장부 대조 후 기입)` | 멤버십 필요/구독 조건 중복 표현 행 | **`'대체됨'` 연결** | #1 정본으로 `'대체됨'` 연결 |
| 5 | `medium.membership` | `미확인 (뮤즈 실장부 대조 후 기입)` | 멤버십 **구독료(\$5/월 또는 \$50/년)** 행 (존재 시) 또는 중복 행 | **조건부 분리 검토(유보) / 중복 시 `'대체됨'`** | 요금 행이면 `medium.membership_price`로 분리 + `115004545567` 연결; 단순 중복이면 #1로 `'대체됨'` 연결 |

---

### 3.2 `medium.payment_fee` (4행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `Set up payouts with Stripe` (`360003928833`, `updated_at: 2026-11-11T16:29:45Z`):
    > *"To receive payouts from the Medium Partner Program, you need to create or connect a free Stripe account. Stripe is Medium's third-party payout processor."*
  - `Medium Partner Program payouts` (`25267297935895`, `updated_at: 2026-11-13T20:19:37Z`): 최소 지급액 `\$10 USD`, 매월 `10th business day` 지급. (참고: 구 문서 `360037691372`는 Zendesk API에서 HTTP 404 `RecordNotFound` 반환.)
  - `Earnings withholding for US taxes` (`360022773274`, `updated_at: 2026-11-11T16:29:47Z`): 비미국 거주자(W-8)는 로열티 원천징수 적용(조세조약 혜택 신청 가능).
- **정본 판정**: Medium 공식 도움말에는 Medium이 부과하는 별도 결제 수수료율(%)이 명시되어 있지 않다. `fact-seed`가 Stripe 키워드(`"stripe"`)가 포함된 문장을 모두 `medium.payment_fee`로 묶었을 가능성이 높다.
- **행 단위 정리 제안 (4행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 6 | `medium.payment_fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | Stripe 무료 연동/제3자 지급 처리사(또는 공식 확인된 Stripe 정산 조건) 대표 1행 | **정본 유지** (URL `360003928833` 백필) | 구 `360037691372`(404) 참조 시 현행 URL로 교체 |
| 7 | `medium.payment_fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | Stripe 정산·수수료 중복 기술 행 | **`'대체됨'` 연결** | #6 정본으로 `'대체됨'` 연결 |
| 8 | `medium.payment_fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 지급 최소액(\$10)·지급일(10영업일) 또는 세금 원천징수(W-8) 혼재 행 | **주제키 분리 검토(유보) 또는 `'대체됨'`** | 지급 조건이면 `medium.payout`(`25267297935895`), 세금이면 `medium.tax`(`360022773274`)로 분리; 중복이면 #6으로 `'대체됨'` |
| 9 | `medium.payment_fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 공식 출처 없는 추정 수수료율(%) 또는 중복 행 | **`'대체됨'` 연결 (또는 하향)** | 원문 미뒷받침 시 발췌 미생성, #6으로 `'대체됨'` 연결 |

---

### 3.3 `naver_premium.fee` (5행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `https://naver-premiumcontents.gitbook.io/guide/chapter2/page13`:
    > *"정산 기준에 따라 산정된 '매출액 – 결제 수수료 – 플랫폼 수수료'를 정산합니다."*
    > *"플랫폼 수수료 8% 프리미엄콘텐츠 스튜디오 이용 수수료"*
    > *"결제 수수료 2.4%(부가세 포함 2.64%) 네이버페이 결제수단 이용 수수료"*
- **정본 판정**: 플랫폼 수수료는 **`8%`**가 정본이다. 결제 수수료 `2.4%(부가세 포함 2.64%)`는 이미 별도 주제키 `naver_premium.payment_fee`(1행)로 분리되어 있다.
- **행 단위 정리 제안 (5행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 10 | `naver_premium.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | **플랫폼 수수료 8%** 정확 기술 대표 1행 | **정본 유지** | 근거 URL `page13` + 원문 발췌 채움 |
| 11 | `naver_premium.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 플랫폼 수수료 8% 중복/표현 변형 행 (1) | **`'대체됨'` 연결** | #10 정본으로 `'대체됨'` 연결 |
| 12 | `naver_premium.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 플랫폼 수수료 8% 중복/표현 변형 행 (2) | **`'대체됨'` 연결** | #10 정본으로 `'대체됨'` 연결 |
| 13 | `naver_premium.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 합산/추정 수수료(예: 10% 또는 10.4%/10.64% 합산 표기) 또는 구 수치 행 (1) | **`'대체됨'` 연결 + 하향** | `page13`에 없는 수치는 발췌 미생성, #10으로 `'대체됨'` 연결 |
| 14 | `naver_premium.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 정산일(익월 21일)/부가세/쿠폰 등 다른 항목 혼재 또는 구 수치 행 (2) | **조건부 분리 검토(유보) 또는 `'대체됨'`** | 정산 일정이면 `naver_premium.payout` 분리 검토; 수수료 중복·오류이면 #10으로 `'대체됨'` 연결 |

---

### 3.4 `patreon.fee` (3행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `Creator fees overview` (`11111747095181`, `updated_at: 2026-11-22T02:23:18Z`):
    > *"If you published your creator page after August 4, 2025, please note that you are on our standard 10% pricing plan."*
  - `A standard platform fee for new creators — effective after August 4, 2025` (`36426991446797`, `updated_at: 2026-11-09T19:52:16Z`):
    - 플랫폼 수수료: **`10%`** (신규 창작자 표준 요금제)
    - 결제 처리 수수료 (> \$3): Credit card / Apple Pay `2.9% + $0.30`, PayPal / Venmo (US) `2.9% + $0.30`, PayPal / Venmo (non-US) `3.9% + $0.30`
- **정본 판정**: 신규 창작자 기준 **`표준 플랫폼 수수료 10%`**가 정본이다.
- **행 단위 정리 제안 (3행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 15 | `patreon.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 신규 창작자 **표준 플랫폼 수수료 10%** 행 | **정본 유지** | 근거 URL `11111747095181` 백필 + 원문 발췌 |
| 16 | `patreon.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 구 요금제(예: Pro 8% / Premium 12%) 또는 중복 행 | **`'대체됨'` 연결** | #15 정본으로 `'대체됨'` 연결 (단, 2025-08-04 이전 레거시 조건 명시 행은 예외 검토) |
| 17 | `patreon.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 결제 처리 수수료(`2.9%+$0.30` / 비미국 PayPal `3.9%+$0.30`) 또는 중복 행 | **조건부 분리 검토(유보) / 중복 시 `'대체됨'`** | 결제 처리 수수료 행이면 `patreon.payment_fee`로 분리(`36426991446797`); 플랫폼 수수료 중복·오류이면 #15로 `'대체됨'` 연결 |

---

### 3.5 `patreon.payout` (2행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `Payouts guide for creators outside of the US` (`39694936541965`, `updated_at: 2026-11-20T09:40:37Z`):
    > *"Fees: 1.55% of payout amount + \$0.25 per payout. No additional currency conversion fee is applied. Minimum payout of \$10."* (USD 교차통화 은행송금 기준; 현지통화 은행송금은 약 `\$0.50` 정액, 최소 출금액 `\$10`)
  - `How payouts work` (`208656246`, `updated_at: 2026-11-21T06:50:16Z`): 출금 수단 등록/변경 시 5일 보류, iOS 인앱결제 75일 보류.
- **정본 판정**: 한국(미국 외) 창작자 기준 출금 수단·최소 출금액(`$10`)·수수료를 공식 문서(`39694936541965`)에 맞춰 기술한 행 1건을 정본으로 유지한다.
- **행 단위 정리 제안 (2행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 18 | `patreon.payout` | `미확인 (뮤즈 실장부 대조 후 기입)` | 미국 외 창작자 출금 수단·최소 출금액(\$10)·수수료 대표 1행 | **정본 유지** | 근거 URL `39694936541965` (또는 보류 기간 주장이면 `208656246`) 백필 |
| 19 | `patreon.payout` | `미확인 (뮤즈 실장부 대조 후 기입)` | 중복/축약 또는 구 출금 조건 행 | **`'대체됨'` 연결** (서로 다른 지표면 하위키 분리 검토) | #18 정본으로 `'대체됨'` 연결 |

---

### 3.6 `gumroad.fee` (2행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `https://gumroad.com/pricing` 및 `https://gumroad.com/help/article/66-gumroads-fees`:
    > *"For sales made on Gumroad's website, we charge a 10% + \$0.50 fee + sales tax per transaction. This does not include: Credit card processing (2.9% + \$0.30) PayPal fees"*
    > *"Sales made through Gumroad's marketplace (discovery sales via Gumroad.com) are subject to a flat 30% fee, which includes all processing fees."*
- **정본 판정**: 현행 직접 판매 수수료 **`10% + $0.50`**(결제 처리 수수료 별도) 기준 행을 정본으로 유지한다.
- **행 단위 정리 제안 (2행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 20 | `gumroad.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 직접 판매 **`10% + $0.50`** (+ 카드 처리 별도) 현행 기준 행 | **정본 유지** | 근거 URL `https://gumroad.com/pricing` 또는 `https://gumroad.com/help/article/66-gumroads-fees` |
| 21 | `gumroad.fee` | `미확인 (뮤즈 실장부 대조 후 기입)` | 구 요율(`$0.50` 누락된 단순 `10%` 등) 또는 Discover `30%` 혼재 행 | **`'대체됨'` 연결** (Discover 30% 전용 행이면 `gumroad.fee.discover` 분리 검토) | 단순 구 요율·중복이면 #20으로 `'대체됨'` 연결 |

---

### 3.7 `gumroad.payout` (2행)

- **공식 문서 실측 근거 (2026-11-22)**
  - `https://gumroad.com/help/article/13-getting-paid`:
    > *"You need a minimum balance of \$100 USD to receive a payout. Some countries have higher local currency minimums—see the "(min X)" values in the table above."*
    > *"Korea KRW (min 40,000)"* (지급 요일: Wednesday)
    > *"Payouts include sales made through the previous Friday (UTC). On the weekly, monthly, and quarterly schedules, a sale has a minimum 7-day holding period in your Gumroad balance before it becomes eligible for payout."*
- **정본 판정**: 현행 공식 문서 기준 **최소 잔액 `\$100 USD` (한국 KRW 계좌 `min 40,000` KRW, 7일 보류)**와 일치하는 행을 정본으로 유지한다. (구 최소 지급액 `\$10` 행은 현행 문서와 불일치하므로 정본 불가.)
- **행 단위 정리 제안 (2행)**

| 순번 | 주제키 | 주장 앞부분 (30자) | 행 유형(실장부 대조 기준) | 제안 조치 | 연결 대상 / 비고 |
|---:|---|---|---|---|---|
| 22 | `gumroad.payout` | `미확인 (뮤즈 실장부 대조 후 기입)` | 현행 정산 기준(`\$100 USD` / 한국 `KRW 40,000` / 7일 보류 / 수요일 지급) 일치 행 | **정본 유지** | 근거 URL `https://gumroad.com/help/article/13-getting-paid` |
| 23 | `gumroad.payout` | `미확인 (뮤즈 실장부 대조 후 기입)` | 구 기준(예: 최소 `\$10`) 또는 중복·축약 행 | **`'대체됨'` 연결** (둘 다 구 기준이면 둘 다 하향 후 신규 정본 행 1건 작성 검토) | #22 정본으로 `'대체됨'` 연결 |

---

## 4. 회장님 승인 및 뮤즈 실행 전 확인 사항 (유보 목록)

1. **실장부 23행 `claim_prefix`·`page_id` 바인딩 (유보)**: 본 세션에 `NOTION_TOKEN`이 없어 개별 행 원문을 조회하지 못했습니다. 뮤즈가 실장부에서 위 7개 주제키 23행의 `(page_id, 주장 앞 30자, 값, 등급, 근거 URL)`을 추출해 본 표의 1~23번과 1:1 매핑한 뒤 회장님 승인을 받아야 합니다.
2. **하위 주제키 분리 여부 결정 (유보)**: `medium.membership`(자격 vs 가격), `medium.payment_fee`(Stripe 연동 vs 원천징수/지급일), `patreon.fee`(플랫폼 수수료 vs 결제 처리 수수료), `gumroad.fee`(직접 판매 vs Discover)에서 서로 다른 차원의 사실이 동일 주제키에 혼재된 경우 `'대체됨'` 연결 대신 하위 주제키 분리로 정리할지 회장님 결정이 필요합니다.
3. **실행 금지 준수**: 본 문서는 정리안 제안만 담고 있으며, Notion `'📒 팩트 장부'`의 `'대체됨'` 연결·등급 조정·URL/발췌 백필은 회장님 승인 후 별도 작업으로 실행합니다.
