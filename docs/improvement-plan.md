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
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 4-A | Calendar KST 타임존 수정 | `graph.py:1764–1766` | UTC 자정 기준 → KST(UTC+9) 기준으로 변환, 오전 일정 누락 방지 |
| 4-B | OAuth token expiry 저장 | `main.py:596–600` | `token_expiry` 필드 추가 저장 → `creds.expired` 정상 동작 |

---

### P5. BQ 성능 개선
**파일**: `graph.py`  
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 5-A | `check_bq_access()` ACL 캐싱 | `graph.py:46–63` | 사용자별 5분 TTL 캐시 → 반복 네트워크 조회 제거 |
| 5-B | BQ SQL 생성 모델 격상 | `graph.py` bq_node | 복잡 분석 SQL: `LITE_MODEL` → `MODEL_NAME`으로 격상 |

---

### P6. Dead Code 정리 및 RAG Refiner 재활성화 준비
**파일**: `graph.py`  
**상태**: `[x]`

| # | 항목 | 위치 | 설명 |
|---|------|------|------|
| 6-A | `rag_retriever_node` 삭제 | `graph.py:647–685` | 구형 Vertex AI Discovery Engine API 사용 레거시 코드 제거 |
| 6-B | `rag_refiner_node` 준비 | `graph.py:610–645` | RAG 파이프라인 재연결 전 로직 검토 및 정제 |

---

## Phase 3 — AI 에이전트 고도화 (Enhancement)

### P7. RAG 파이프라인 고도화
**파일**: `graph.py`, `rag_node.py`, `main.py`  
**상태**: `[x]`

| # | 항목 | 설명 |
|---|------|------|
| 7-A | `rag_refiner_node` 워크플로우 연결 | 대화 맥락 기반 검색어 정제 노드 RAG 파이프라인에 재연결. 정제 실패 시 원문 폴백 |
| 7-B | `_expand_query()` LLM 기반 동적 확장 | 하드코딩 5개 키워드 → `LITE_MODEL`로 유사어/확장어 동적 생성 |

---

### P8. 에이전트 품질 고도화
**파일**: `graph.py`  
**상태**: `[x]`

| # | 항목 | 설명 |
|---|------|------|
| 8-A | Supervisor 멀티턴 컨텍스트 강화 | Supervisor에서 최근 2턴(4개 메시지) 이력 참고 → "복직 후에는?" 같은 후속 질문 RAG 라우팅 정확도 향상 |
| 8-B | Supervisor Context Caching | `_SUPERVISOR_SYSTEM_INSTRUCTION` 고정 규칙 모듈 레벨 상수화 + `_get_supervisor_cache()` 인프라 구축. Cache 활성 시 cached_content 사용, 미달(4,096 토큰 기준) 시 `system_instruction` 파라미터 폴백 |

---

### P9. SSE 스트리밍 도입
**상태**: `[제외]` — 도표 렌더링(ApexCharts), 소스카드 등 구조화 응답과 충돌. 일반 텍스트 출력 형태가 사용자 UX에 맞지 않아 롤백 경험이 있어 구축 범위에서 제외.

---

## Phase 4 — 기능 확장 (Feature)

### P10. Google Calendar 회의실 예약 연동
**파일**: `graph.py` (`calendar_free_node`, `calendar_write_node`), `main.py`  
**상태**: `[ ]` — IT팀 회의실 리소스 이메일 목록(CSV) 수령 대기 중

**배경**: 사용자가 챗봇에서 "다음주 화요일 오후 2시 6인 회의실 잡아줘" 요청 시 가용 회의실 조회 + 캘린더 예약까지 원스톱 처리

| # | 항목 | 설명 |
|---|------|------|
| 10-A | 회의실 목록 등록 | IT팀 CSV 수령 후 Firestore `meeting_rooms` 컬렉션 또는 `graph.py` 상수 `MEETING_ROOMS`로 등록 (이름·이메일·층·수용인원) |
| 10-B | `calendar_free_node` 회의실 가용성 조회 | 사용자 지정 시간대에 `freebusy.query`로 회의실 목록 동시 조회 → 비어있는 회의실 필터링 후 안내 |
| 10-C | `calendar_write_node` 회의실 예약 | 사용자 선택 회의실 이메일을 `attendees`에 추가하여 이벤트 생성 → 회의실 자동 예약 |
| 10-D | Supervisor 라우팅 보강 | "회의실 잡아줘" 등 키워드에서 `CALENDAR_FREE` 정확 라우팅 확인 및 프롬프트 보강 |

**선행 조건**: IT팀으로부터 `c_xxx@resource.calendar.google.com` 형태의 회의실 리소스 이메일 목록 수령

---

### P11. 이미지 표시 기능 (RAG 응답 내 인라인 이미지)
**파일**: `embed_and_load.py`, `rag_node.py`, `graph.py` (`reasoner_node`), `main.py`, `templates/index.html`  
**상태**: `[→]` — 11-A/B/D/E/F 완료 (시트 수동 연동 경로), 11-C(이미지 파일 자동 처리)는 보류

**배경**: 현재 텍스트+링크만 반환되는 답변에 이미지를 인라인으로 표시하여 직관적인 정보 전달. 부서 담당자가 각 부서 폴더의 "공식이미지" 서브폴더에 이미지를 올리고 URL을 FAQ 시트에 붙여넣으면 관련 질문 답변 시 이미지가 함께 표시됨

| # | 항목 | 파일 | 설명 | 상태 |
|---|------|------|------|------|
| 11-A | BigQuery 스키마 변경 | BigQuery / `embed_and_load.py` | `knowledge_master`에 `images STRING`(JSON 배열) 컬럼 추가. 단일 `image_url` 대신 `links` 컬럼과 동일한 다중 이미지 구조로 설계 | `[x]` |
| 11-B | Sheets `이미지N_이름`/`이미지N_URL` 동적 파싱 | `embed_and_load.py` | 기존 `링크1~20` 동적 감지 엔진과 동일한 패턴으로 `이미지1~20` 세트 자동 감지 → `images` 컬럼에 JSON 직렬화. "공식이미지" 폴더는 문서 스캔에서 제외(11-C와 경로 분리, 폴더 내 안내 텍스트 파일 오염 방지) | `[x]` |
| 11-C | 이미지 파일 직접 처리 | `embed_and_load.py` | `.jpg/.png/.gif/.webp` 파일 감지 → Gemini Vision으로 설명 생성 → 벡터화, Drive 공유 URL 저장 | `[ ]` 보류 — 11-B(시트 수동 연동)와 동일 폴더 사용 시 중복 노출 위험, 폴더 분리 규칙 확정 후 재착수 |
| 11-D | RAG 검색 결과에 `images` 포함 | `rag_node.py` | `hybrid_search_bq` SELECT에 `images` 컬럼 추가, 문서 블록에 `[이미지목록]: {JSON}` 필드로 반환 | `[x]` |
| 11-E | `main.py` 응답 JSON에 `images` 포함 | `graph.py`(`reasoner_node`), `main.py` | 블록에서 `[이미지목록]` 정규식 추출·JSON 파싱 후 소스카드(`extracted_sources`)에 `images` 배열로 병합, `main.py`는 그대로 pass-through | `[x]` |
| 11-F | 프론트엔드 소스카드 이미지 렌더링 | `index.html` | `images` 배열 있을 때 소스카드 하단에 썸네일 `<img>` 다건 인라인 렌더링 (클릭 시 원본 열기) | `[x]` |

**구현 순서(실제)**: 11-A → 11-B → 11-D → 11-E → 11-F (11-C는 폴더 분리 정책 미확정으로 보류)

---

## 변경 이력

| 날짜 | Phase | 내용 |
|------|-------|------|
| 2026-06-30 | — | 계획서 최초 작성 |
| 2026-06-30 | Phase 1 | P1·P2·P3 완료: Firestore/BQ 싱글턴, Model Armor 토큰 캐싱, Gmail 병렬 조회, 히스토리 비동기 처리 |
| 2026-06-30 | Phase 2 | P4·P5 완료: Calendar KST 타임존 수정, OAuth expiry 저장, BQ ACL 5분 TTL 캐시, BQ SQL 생성 MODEL_NAME 격상 |
| 2026-06-30 | Phase 2 | P6 완료: rag_retriever_node(구형 Discovery Engine) 삭제, rag_refiner_node 보존 (P7에서 연결 예정) |
| 2026-06-30 | Phase 3 | P7 완료: rag_refiner_node 그래프 연결(Refiner→Search→Reasoner), main.py 중복 메시지 제거(현재 질문만 전달), _expand_query LLM 동적 확장으로 교체 |
| 2026-07-01 | Phase 3 | P8 완료: Supervisor 최근 2턴 히스토리 라우팅 컨텍스트(P8-A), Context Cache 인프라 + system_instruction 분리(P8-B). P9 SSE 스트리밍 구축 범위 제외 |
| 2026-07-01 | Phase 4 | P10 등록: Google Calendar 회의실 예약 연동 — IT팀 CSV 수령 대기 중 |
| 2026-07-01 | Phase 4 | P11 등록: 이미지 표시 기능 — 사용자·부서 요구사항 기반, 설계 완료 |
| 2026-07-01 | Phase 4 | P11-A/B/D/E/F 완료: BQ `images` 컬럼(JSON 배열), 시트 `이미지N` 동적 파싱, RAG→Reasoner→소스카드 이미지 파이프라인 연결, 소스카드 썸네일 렌더링. "공식이미지" 폴더 문서 스캔 제외 처리로 안내 텍스트 오염 방지. 11-C(자동 이미지 처리)는 보류 |
