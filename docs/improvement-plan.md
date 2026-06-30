# 코봇 고도화 계획서

> 작성일: 2026-06-30  
> 대상 브랜치: `master`  
> 진행 상태 범례: `[ ]` 대기 · `[→]` 진행 중 · `[x]` 완료

---

## Phase 1 — 병목현상 해결 (Performance)

### P1. 클라이언트 싱글턴화 + 히스토리 비동기 처리
**파일**: `graph.py`, `main.py`  
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 1-A | Firestore 클라이언트 싱글턴 | `graph.py:259, 281` | `get_workspace_service()` 내부에서 매 호출마다 `firestore.Client()` 재생성 → 모듈 레벨 1회 생성으로 교체 |
| 1-B | BQ 클라이언트 재사용 | `graph.py:3644` | `log_to_analytics_v2()` 내 `bq_client_logger = bigquery.Client(...)` → 기존 모듈 레벨 `bq_client` 재사용 |
| 1-C | 히스토리 저장 비동기화 | `main.py:388–437` | Firestore 히스토리 쓰기 + BQ 로그 → `BackgroundTasks`로 응답 반환 후 처리 |

---

### P2. Model Armor 인증 토큰 캐싱
**파일**: `graph.py`  
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 2-A | 토큰 캐시 딕셔너리 추가 | `graph.py:71–78` | `_armor_token_cache` 모듈 레벨 딕셔너리. 만료 60초 전 갱신, 그 외엔 캐시 반환 |

---

### P3. Gmail 메시지 병렬 조회
**파일**: `graph.py`  
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 3-A | ThreadPoolExecutor 도입 | `graph.py:1313–1331` | for-loop 순차 조회 → `ThreadPoolExecutor(max_workers=10)` 병렬 조회 |

---

## Phase 2 — 코드 품질 개선 (Quality)

### P4. 잠재적 버그 수정
**파일**: `graph.py`, `main.py`  
**상태**: `[ ]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 4-A | Calendar KST 타임존 수정 | `graph.py:1764–1766` | UTC 자정 기준 → KST(UTC+9) 기준으로 변환, 오전 일정 누락 방지 |
| 4-B | OAuth token expiry 저장 | `main.py:596–600` | `token_expiry` 필드 추가 저장 → `creds.expired` 정상 동작 |

---

### P5. BQ 성능 개선
**파일**: `graph.py`  
**상태**: `[ ]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 5-A | `check_bq_access()` ACL 캐싱 | `graph.py:46–63` | 사용자별 5분 TTL 캐시 → 반복 네트워크 조회 제거 |
| 5-B | BQ SQL 생성 모델 격상 | `graph.py` bq_node | 복잡 분석 SQL: `LITE_MODEL` → `MODEL_NAME`으로 격상 |

---

### P6. Dead Code 정리 및 RAG Refiner 재활성화 준비
**파일**: `graph.py`  
**상태**: `[ ]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 6-A | `rag_retriever_node` 삭제 | `graph.py:647–685` | 구형 Vertex AI Discovery Engine API 사용 레거시 코드 제거 |
| 6-B | `rag_refiner_node` 준비 | `graph.py:610–645` | RAG 파이프라인 재연결 전 로직 검토 및 정제 |

---

## Phase 3 — AI 에이전트 고도화 (Enhancement)

### P7. RAG 파이프라인 고도화
**파일**: `graph.py`, `rag_node.py`  
**상태**: `[ ]`

| # | 항목 | 설명 |
|---|------|------|
| 7-A | `rag_refiner_node` 워크플로우 연결 | 대화 맥락 기반 검색어 정제 노드 RAG 파이프라인에 재연결. 정제 실패 시 원문 폴백 |
| 7-B | `_expand_query()` LLM 기반 동적 확장 | 하드코딩 5개 키워드 → `LITE_MODEL`로 유사어/확장어 동적 생성 |

---

### P8. 에이전트 품질 고도화
**파일**: `graph.py`, `main.py`  
**상태**: `[ ]`

| # | 항목 | 설명 |
|---|------|------|
| 8-A | 멀티턴 컨텍스트 강화 | `main.py` ChatRequest에 최근 3–5턴 전달, Supervisor 라우팅 정확도 향상 |
| 8-B | Supervisor 프롬프트 Context Caching | 고정 분류 규칙 부분 Gemini Context Caching API 적용, 토큰 비용 절감 |

---

### P9. SSE 스트리밍 도입
**파일**: `main.py`, `templates/index.html`  
**상태**: `[ ]`

| # | 항목 | 설명 |
|---|------|------|
| 9-A | FastAPI StreamingResponse 적용 | `GENERAL` / `RAG` 노드부터 단계적 적용 |
| 9-B | 프론트엔드 SSE 수신 처리 | `index.html` fetch → EventSource 전환 |

---

## 변경 이력

| 날짜 | Phase | 내용 |
|------|-------|------|
| 2026-06-30 | — | 계획서 최초 작성 |
| 2026-06-30 | Phase 1 | P1·P2·P3 완료: Firestore/BQ 싱글턴, Model Armor 토큰 캐싱, Gmail 병렬 조회, 히스토리 비동기 처리 |
