# 코봇 아키텍처 문서

> 코웨이 전사 AI 챗봇 "코봇" 백엔드의 모듈 구조, 데이터 흐름, 의존관계를 정리합니다.

---

## 1. 전체 구조 (High-Level)

```
사용자 브라우저
    │  POST /api/chat  (+ IAP 헤더 X-Goog-Authenticated-User-Email)
    ▼
┌──────────────────────────────────────────────────────────┐
│  main.py  (FastAPI on Cloud Run)                         │
│  · IAP 이메일 추출 → user_info 구성                        │
│  · coway_agent_app.invoke() 호출                          │
│  · 응답 후처리: [CHART_DATA] / [SOURCE_REPORTS] /         │
│    |||SUGGESTIONS||| 파싱 후 구조화된 JSON 반환             │
└──────────────┬───────────────────────────────────────────┘
               │  LangGraph invoke
               ▼
┌──────────────────────────────────────────────────────────┐
│  graph.py  (LangGraph StateGraph)                        │
│  Supervisor → 의도 분류 → 적합 노드 실행                    │
└──────────────┬───────────────────────────────────────────┘
               │
      ┌────────┴─────────┬──────────────────────┐
      ▼                  ▼                      ▼
 RAG 파이프라인      BQ 파이프라인          Workspace 노드들
 (rag_node.py)   (BQ SQL 생성/실행)    (Gmail/Calendar/Tasks/
                                       Drive/Sheets/Docs/
                                       People)
```

---

## 2. 모듈별 역할

### `main.py` — FastAPI 진입점
- **역할**: HTTP 레이어. 모든 API 엔드포인트 정의 및 LangGraph 결과 후처리.
- **주요 엔드포인트**:
  | 경로 | 메서드 | 설명 |
  |------|--------|------|
  | `/api/chat` | POST | 채팅 요청 처리 메인 엔드포인트 |
  | `/api/sync-knowledge` | POST | 지식베이스 동기화 파이프라인 트리거 |
  | `/api/oauth2callback` | GET | Google OAuth 2.0 인증 콜백 |
  | `/api/history` | GET | 사용자 히스토리 조회 |
  | `/api/feedback-log` | POST | 답변별 피드백 저장 |
  | `/api/global-feedback` | POST | 글로벌 의견 저장 |
  | `/` | GET | index.html 서빙 |
- **후처리 흐름**:
  1. `final_state["sources"]` → `source_results` 구성
  2. LLM 응답에서 `[SOURCE_REPORTS][...]` 파싱하여 소스 보완
  3. `[CHART_DATA]{...}` → `parsed_chart_data`
  4. `|||SUGGESTIONS|||` → `suggestions_payload`
  5. 정제된 `clean_answer_body` + 나머지 필드를 JSON으로 반환
- **의존**: `graph.py` (`coway_agent_app`, `log_to_analytics_v2`)

---

### `graph.py` — LangGraph 에이전트 오케스트레이터
- **역할**: 의도 분류 및 멀티 에이전트 실행 그래프.
- **상태 스키마** (`AgentState`): `messages`, `user_info`, `retrieved_docs`, `sources`, `top_dept_code`, `bq_error_count`, `workspace_action`, 기타
- **Supervisor 라우팅 의도 → 노드 매핑**:
  | 의도 | 노드(들) | 설명 |
  |------|---------|------|
  | `RAG` | `rag_search_node` → `reasoner_node` | 하이브리드 BQ 벡터 검색 + Gemini 추론 |
  | `BQ` | `bq_node` (→ `bq_corrector_node` 최대 2회 재시도) | Gemini SQL 생성 → BigQuery 실행 |
  | `GENERAL` | `general_node` | 일상 대화 응답 |
  | `EMAIL_WRITE` | `email_write_node` | Gmail 초안/발송 |
  | `EMAIL_READ` | `email_read_node` | Gmail 수신함 요약 |
  | `CALENDAR_WRITE` | `calendar_write_node` | Google Calendar CRUD |
  | `CALENDAR_READ` | `calendar_read_node` | Google Calendar 조회 |
  | `TASK_WRITE` | `task_write_node` | Google Tasks CRUD |
  | `TASK_READ` | `task_read_node` | Google Tasks 조회 |
  | `PEOPLE_SEARCH` | `people_search_node` | 임직원 디렉토리 검색 |
  | `SHEET_READ` | `sheet_read_node` | 스프레드시트 읽기 |
  | `SHEET_WRITE` | `sheet_write_node` | 스프레드시트 쓰기 |
  | `DOCS_CREATE` | `docs_create_node` | Google Docs 문서 생성 |
- **Firestore Checkpointing**: `langgraph_checkpoint_firestore.FirestoreSaver`로 대화 상태 지속
- **OAuth 패턴**: `get_workspace_service(email, service)` → `user_tokens/{email}` Firestore 조회 → 토큰 없으면 `ValueError("AUTH_REQUIRED_FOR:{email}")` raise → main.py에서 OAuth 플로우 안내
- **의존**: `rag_node.py` (`hybrid_search_bq`, `is_broad_query`), GCP AI/BQ/Firestore 클라이언트

---

### `rag_node.py` — RAG 검색 엔진
- **역할**: 사용자 질문을 벡터화하여 BigQuery에서 하이브리드 검색 실행.
- **핵심 함수**:
  | 함수 | 역할 |
  |------|------|
  | `get_query_embedding(text)` | 질문 → 3072차원 벡터 (`gemini-embedding-2`) |
  | `is_broad_query(query)` | 포괄적 질문 여부 판별 (`알려줘`, `전체`, `목록` 등) |
  | `_expand_query(user_query)` | 광범위 질문에 대해 보조 검색 쿼리 반환 |
  | `hybrid_search_bq(user_query, user_email, top_k)` | 메인 검색 함수 |
- **하이브리드 검색 알고리즘**:
  1. `VECTOR_SEARCH()` (코사인 거리, BQ 내장) — top_k×2(20개) 프리페치 후 필터
  2. ACL 필터: `allowed_groups = 'employee_all@coway.com' OR LIKE '%user_email%'`
  3. 키워드 부스트: 핵심 키워드(불용어 제거 후 최대 4개 OR 패턴)가 `content`에 있으면 +0.15
  4. `hybrid_score = (1 - distance) + keyword_boost` 내림차순 정렬
  5. 광범위 질문 감지 시: `_expand_query`로 2차 BQ 검색 → 중복 URL 제외 후 병합
- **반환값**: `(context_text: str, top_dept_code: str)` — `---` 구분자로 연결된 문서 블록들

---

### `embed_and_load.py` — 지식베이스 동기화 파이프라인
- **역할**: Google Drive 공유폴더를 재귀 순회하여 문서를 파싱·임베딩·BigQuery 적재.
- **트리거**: POST `/api/sync-knowledge` (매일 새벽 2시 Cloud Scheduler)
- **처리 흐름**:
  ```
  Drive 공유폴더(TARGET_SHARED_FOLDER_ID)
      → 재귀 탐색 (폴더명으로 dept_code 추론: '팀'/'TF' 접미사)
      → 변경된 파일만 처리 (modifiedTime vs BQ last_modified 비교)
      → 파일 유형별 파싱:
          · Spreadsheet (Google Sheets) → FAQ 행 단위 분할
          · PDF / 이미지 → Gemini AI 파싱 (document_parser.py)
          · Docs / PPT → 텍스트 내보내기
      → MarkdownTextSplitter로 청킹
      → gemini-embedding-2로 3072차원 벡터 생성
      → BigQuery knowledge_master 테이블에 upsert
      → Drive에서 삭제된 파일은 BQ에서도 purge
  ```
- **의존**: `document_parser.py` (`parse_document_to_markdown`), Drive API, BigQuery, `google-genai`

---

### `document_parser.py` — 문서 파싱 유틸리티
- **역할**: 다양한 파일 형식을 Markdown 텍스트로 변환.
- **처리 유형**:
  | 파일 형식 | 처리 방식 |
  |-----------|---------|
  | PDF / 이미지 | 바이트를 Gemini에 직접 전달하여 Markdown 변환 |
  | Excel / XLSX | `openpyxl` 파싱, 하이퍼링크 → Markdown 링크 보존 |
- **팀명 추출**: `extract_team_name_from_path(path)` — 경로 오른쪽부터 '팀' 접미사 세그먼트 탐색
- **의존**: `google-genai` (enterprise client, `global` location)

---

### `templates/index.html` — 프론트엔드 SPA
- **역할**: 단일 HTML 파일 내 모든 UI 로직 (CSS + Vanilla JS).
- **주요 동작**:
  - `fetch('/api/chat')` → 응답의 `results`, `summary`, `chartData`, `suggestions` 렌더링
  - 인용번호 렌더링: `[N]` → `<span class="citation-chip">` (정규식 치환, line ~911)
  - `scrollToDoc(msgId, num)` → `#doc-card-{msgId}-{num}` 스크롤
  - ApexCharts로 `chartData` 시각화
  - 히스토리 사이드바: `/api/history` 폴링

---

## 3. 데이터 흐름 상세

### 3-1. 채팅 요청 (RAG 경로)

```
사용자 입력
    ↓
main.py: IAP 헤더 → user_email 추출
    ↓
graph.py: Supervisor (Gemini 3.5 Flash)
    · 의도 → RAG
    ↓
graph.py: rag_search_node
    · is_broad_query() 판별 → top_k=8(광범위) or top_k=5(핀포인트)
    · hybrid_search_bq() 호출
    ↓
rag_node.py: hybrid_search_bq
    · get_query_embedding() → 3072d 벡터
    · BQ VECTOR_SEARCH (코사인, top_k×2 프리페치)
    · ACL 필터 + 키워드 부스트 → top_k 결과 선별
    · 광범위 쿼리: _expand_query → 2차 BQ 검색 후 병합
    · 반환: "---" 구분 문서 블록 문자열 + top_dept_code
    ↓
graph.py: reasoner_node
    · 중복 URL 청크 병합
    · valid_blocks 구성 (URL 있는 블록만) → num_docs = 소스카드 수와 1:1
    · [문서 N] 번호 부여 → LLM 프롬프트 구성
    · Model Armor 프롬프트 검사
    · Gemini 3.5 Flash 호출 → 응답 생성
    · 범위 초과 인용번호 제거 (num_docs 초과 시)
    · Model Armor 응답 검사
    · 반환: {messages, sources}
    ↓
main.py: extract_structured_payload
    · [SOURCE_REPORTS] JSON 파싱
    · [CHART_DATA] JSON 파싱
    · |||SUGGESTIONS||| 분리
    · BQ analytics 로그 (chatbot_analytics.query_analytics_v2)
    · Firestore 히스토리 저장 (user_history/{email}/sessions, 6슬롯 롤링)
    ↓
JSON 응답 반환 → 프론트엔드
```

### 3-2. 지식베이스 동기화

```
Cloud Scheduler (매일 02:00 KST)
    ↓
POST /api/sync-knowledge
    ↓
embed_and_load.py
    · Drive API로 TARGET_SHARED_FOLDER_ID 재귀 탐색
    · BQ existing_meta(file_id, last_modified) 조회
    · 변경 파일만 처리:
        · Sheets → FAQ 행 분할 → chunk
        · PDF/이미지 → document_parser.parse_document_to_markdown
        · Docs/PPT → text export
    · MarkdownTextSplitter로 청킹
    · gemini-embedding-2 임베딩 (3072d)
    · BQ INSERT (knowledge_master)
    · Drive 삭제 파일 → BQ DELETE (purge)
```

---

## 4. 의존관계 그래프

```
main.py
  └─ graph.py
       ├─ rag_node.py
       │    └─ [google-genai, google-cloud-bigquery]
       └─ [google-genai, google-cloud-bigquery, google-cloud-firestore,
           google-api-python-client (Gmail/Calendar/Tasks/Drive/Sheets/Docs/People)]

embed_and_load.py
  └─ document_parser.py
       └─ [google-genai (enterprise), openpyxl]

templates/index.html (프론트엔드, 서버 의존 없음)
```

---

## 5. GCP 인프라 및 외부 서비스

| 서비스 | 용도 | 상세 |
|--------|------|------|
| **Cloud Run** | 애플리케이션 호스팅 | `coway-cobot-fullstack`, `asia-northeast3` |
| **Gemini 3.5 Flash** | LLM 추론 | `gemini-3.5-flash`, enterprise client (`global`) |
| **gemini-embedding-2** | 벡터 임베딩 | 3072차원, `us` 리전 |
| **BigQuery** | 벡터 스토어 + 정형 데이터 + 분석 로그 | 아래 표 참조 |
| **Firestore** | 대화 히스토리 + OAuth 토큰 + LangGraph 체크포인트 | 아래 표 참조 |
| **Model Armor** | 프롬프트/응답 안전성 검사 | `asia-northeast3` |
| **Google Drive API** | 지식베이스 원천 문서 수집 | `embed_and_load.py` |
| **Google Workspace APIs** | Gmail, Calendar, Tasks, Sheets, Docs, People | 노드별 per-user OAuth |
| **Cloud IAP** | 사용자 인증 (헤더 `X-Goog-Authenticated-User-Email`) | |
| **Cloud Scheduler** | 새벽 2시 sync-knowledge 트리거 | |

### BigQuery 데이터셋

| 데이터셋.테이블 | 역할 |
|-----------------|------|
| `hrga_rag_data.knowledge_master` | 벡터 지식베이스 (임베딩 + 문서 메타) |
| `hrga_travel_data.travel_master_db` | 임직원 출장 기록 |
| `hrga_cost_data.budget_master` | 예산 마스터 |
| `hrga_cost_data.budget_raw` | 원시 예산 데이터 |
| `hrga_cost_data.execution_detail` | 집행 상세 |
| `chatbot_analytics.query_analytics_v2` | 쿼리 로그 (자동 생성) |
| `chatbot_analytics.feedback_logs` | 만족도 피드백 (자동 생성) |

### Firestore 컬렉션

| 컬렉션 | 역할 |
|--------|------|
| `coway_chat_sessions` | 전체 대화 히스토리 (레거시) |
| `user_history/{email}/sessions` | 사이드바용 6슬롯 롤링 히스토리 (활성) |
| `user_tokens/{email}` | Workspace API per-user OAuth refresh 토큰 |
| `langgraph_checkpoints` (자동) | LangGraph 상태 체크포인트 |

---

## 6. 주요 진입점 요약

| 진입점 | 파일 | 실행 방법 |
|--------|------|-----------|
| 웹 서버 (운영) | `main.py` | `uvicorn main:app --host 0.0.0.0 --port 8080` |
| RAG 단독 테스트 | `rag_node.py` | `python rag_node.py` |
| 지식베이스 동기화 | `embed_and_load.py` | `python embed_and_load.py` 또는 POST `/api/sync-knowledge` |
| 문서 파서 테스트 | `document_parser.py` | `python document_parser.py` |
| Cloud Run 배포 | — | `gcloud run deploy coway-cobot-fullstack --source . --region asia-northeast3` |
