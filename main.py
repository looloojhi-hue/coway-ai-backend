import os
import base64
import datetime  # 🎯 만족도 피드백 타임스탬프 고속 사출용
import json
import re
import urllib.parse
import requests as http_requests
from fastapi import FastAPI, Request, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from google.cloud import bigquery  # 🎯 피드백 데이터 실시간 빅쿼리 스트리밍용
from langchain_core.messages import HumanMessage, AIMessage
from google.cloud import firestore  # 🎯 파이어스토어 실시간 제어 드라이버

# 💡 graph.py 하단에 정의한 명품 실시간 로깅 함수와 랭그래프 앱 객체 로드
from graph import coway_agent_app, log_to_analytics_v2

app = FastAPI(title="Coway AI Smart Search Portal", version="2.5")

# 🗄️ [인프라 기술 주입] 구글 공식 파이어스토어 클라이언트 바인딩
PROJECT_ID = "gcp-cw-ai-chatbot"
db_fs = firestore.Client(project=PROJECT_ID)
COLLECTION_NAME = "coway_chat_sessions"

# ====================================================================
# 🛡️ [워크스페이스 OAuth 2.0 스코프 선언]
# gmail.modify: 읽음 처리(UNREAD 라벨 제거) 포함 — gmail.readonly를 대체
# ====================================================================
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",        # 메일 읽기, 검색, 읽음 처리(readonly 포함)
    "https://www.googleapis.com/auth/gmail.compose",       # 초안 작성
    "https://www.googleapis.com/auth/gmail.send",          # 메일 즉시 발송 및 회신
    "https://www.googleapis.com/auth/calendar",             # 캘린더 조회/등록/수정/삭제/RSVP
    "https://www.googleapis.com/auth/tasks",                # Tasks 조회/등록/완료/삭제
    "https://www.googleapis.com/auth/drive.readonly",      # 드라이브 파일 검색 및 목록 조회
    "https://www.googleapis.com/auth/directory.readonly",  # 임직원 디렉토리 검색 (People API)
    "https://www.googleapis.com/auth/spreadsheets",        # 스프레드시트 읽기/쓰기 (Sheets API)
    "https://www.googleapis.com/auth/documents",           # Google Docs 문서 생성/편집 (Docs API)
]

# OAuth 2.0 클라이언트 자격증명 — GCP Console에서 발급된 값을 Cloud Run 환경변수로 주입
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8080/api/oauth2callback"
)
# ====================================================================


def _build_google_auth_url(state: str) -> str:
    """PKCE 없이 OAuth 2.0 인증 URL을 직접 생성 — flow.authorization_url()의 자동 PKCE 주입 우회"""
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)

# 📂 static 창고 및 templates 가방 인프라 포지셔닝
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ====================================================================
# [SECTION 1] 📦 REST API 통신용 Pydantic 데이터 무결성 스키마 장부
# ====================================================================
class ChatRequest(BaseModel):
    current: str
    lastQ: Optional[str] = ""
    lastA: Optional[str] = ""
    sessionId: Optional[str] = "default_session"

class FeedbackLogRequest(BaseModel):
    query: str
    type: str
    reasons: List[str]
    comment: Optional[str] = ""

class GlobalFeedbackSaveRequest(BaseModel):
    type: str
    priority: str
    title: str
    content: str
    lastQuery: Optional[str] = ""

# ====================================================================
# [SECTION 2] 🔑 사내 보안 가이드라인 준수 IAP 사원증 추출 엔진
# ====================================================================
def get_iap_user_email(request: Request) -> str:
    """인프라기술팀 윤지호님이 개통해줄 IAP 인증 정문의 보증 헤더를 강제 파싱합니다."""
    iap_header = request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_header:
        return iap_header.split(":")[-1].strip().lower()
    # 로컬 개발/디버깅 단계 및 파일럿용 미인증 세션 하이패스 대안 설정
    return "looloojhi@coway.com" 

# ====================================================================
# [SECTION 3] 📄 최전방 웹포털 진입점 라우터 (HtmlService 완벽 대체)
# ====================================================================
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    print("🖥️ [포털 접속] 최전방 HTML 고속 통로 연동 성공")
    html_file_path = os.path.join("templates", "index.html")
    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    except Exception as e:
        print(f"❌ [HTML 로드 크래시 방어] 파일 읽기 실패: {e}")
        return HTMLResponse(content=f"<h1>Internal Server Error: Template Load Failure</h1><p>{str(e)}</p>", status_code=500)

# ====================================================================
# [SECTION 4] 🧑‍💼 사내 임직원 인사 정보 동기화 라우터
# ====================================================================
@app.get("/api/user-info")
async def get_user_info(user_email: str = Depends(get_iap_user_email)):
    import time
    display_name = user_email.split('@')[0]

    # Firestore 캐시 조회 (People API 호출 생략, ~50ms)
    cache_key = user_email.replace('@', '__at__').replace('.', '__')
    cache_ref = db_fs.collection("user_info_cache").document(cache_key)
    try:
        cached_doc = cache_ref.get()
        if cached_doc.exists:
            data = cached_doc.to_dict()
            if time.time() - data.get('cached_at', 0) < 86400:  # 24h TTL
                return {"name": data['name'], "email": user_email}
    except Exception:
        pass

    # 캐시 미스 시 People API 조회 (첫 접속 또는 TTL 만료)
    try:
        from graph import get_workspace_service
        people_svc = get_workspace_service('people', 'v1', user_email)
        result = people_svc.people().searchDirectoryPeople(
            query=user_email,
            readMask='names',
            sources=['DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE'],
            pageSize=1
        ).execute()
        people = result.get('people', [])
        if people and people[0].get('names'):
            raw = people[0]['names'][0].get('displayName', '')
            korean_name = raw.split('[')[0].strip()
            if korean_name:
                display_name = korean_name
    except Exception:
        pass

    # Firestore에 결과 캐시 저장
    try:
        cache_ref.set({'name': display_name, 'cached_at': time.time()})
    except Exception:
        pass

    return {"name": display_name, "email": user_email}

# ====================================================================
# [SECTION 5] 🤖 [WBS 3.0] Cloud Run 중앙 오케스트레이션 대화 엔진 (정공법 완공본)
# ====================================================================
def extract_structured_payload(text: str, tag: str):
    """
    🎯 [정해인 프로 사규 대괄호 특수문자 및 다중 차트 누출 방어 가드 완공]
    태그 마커의 고유 길이를 연산하여 스캔 포인터를 진짜 데이터 구역 뒤로 강제 격리 전진시킵니다.
    본문 내에 복수 개의 차트/출처 토큰이 멀티 사출되더라도 찌꺼기 없이 와일 루프로 완벽히 전수 제어 청소합니다.
    """
    tag_marker = f"[{tag}]"
    clean_text = text
    collected_payload = None
    
    opener = '{' if tag == 'CHART_DATA' else '['
    closer = '}' if tag == 'CHART_DATA' else ']'
    
    while True:
        idx = clean_text.find(tag_marker)
        if idx == -1:
            break
            
        # 🎯 [핵심 패치] 태그명 자체의 대괄호 글자에 낚이지 않도록 탐색 오프셋을 태그 명칭 뒤로 격리 전진!
        content_start = idx + len(tag_marker)
        start_pos = clean_text.find(opener, content_start)
        
        if start_pos == -1:
            # 매칭 구조가 망가진 유령 태그선 문자열 청소 세정
            clean_text = clean_text[:idx] + clean_text[content_start:]
            continue
            
        count = 0
        end_pos = -1
        for i in range(start_pos, len(clean_text)):
            if clean_text[i] == opener:
                count += 1
            elif clean_text[i] == closer:
                count -= 1
                if count == 0:
                    end_pos = i
                    break
                    
        if end_pos != -1:
            json_str = clean_text[start_pos:end_pos + 1]
            try:
                parsed_obj = json.loads(json_str.strip())
                if tag == 'SOURCE_REPORTS':
                    if collected_payload is None:
                        collected_payload = []
                    if isinstance(parsed_obj, list):
                        collected_payload.extend(parsed_obj)
                    else:
                        collected_payload.append(parsed_obj)
                else:
                    # CHART_DATA 다중 사출 전부 수집 (첫 번째만 저장 시 1개 차트 버그 발생)
                    if collected_payload is None:
                        collected_payload = []
                    collected_payload.append(parsed_obj)
            except Exception as parse_err:
                print(f"⚠️ [파이썬 계층 데이터 해독 패닉] {tag} JSON 파싱 실패: {parse_err}")
                
            # 오차 없는 최종 텍스트 슬라이싱 컷오프 범위 재결합하여 잔재 노출 원천 격살
            clean_text = clean_text[:idx] + clean_text[end_pos + 1:]
        else:
            # 닫히는 괄호 매칭 유실선 강제 청소 워시
            clean_text = clean_text[:idx] + clean_text[content_start:]
            
    return collected_payload, clean_text.strip()

def _save_history_background(session_id: str, user_email: str, intent: str,
                              current_msg: str, answer: str,
                              source_results: list, extracted_links: str,
                              parsed_chart_data):
    """Firestore 히스토리 저장 — BackgroundTasks에서 실행되므로 응답 반환에 영향 없음."""
    try:
        doc_ref = db_fs.collection(COLLECTION_NAME).document(session_id)
        doc = doc_ref.get()
        short_title = current_msg[:25] + "..." if len(current_msg) > 25 else current_msg
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_history_payload = [
            {"role": "user",      "content": current_msg, "timestamp": now},
            {"role": "assistant", "content": answer, "timestamp": now,
             "results": source_results, "links": extracted_links, "chartData": parsed_chart_data},
        ]
        if doc.exists:
            doc_ref.update({
                "title": short_title,
                "badge_type": intent,
                "messages": firestore.ArrayUnion(new_history_payload),
                "updated_at": firestore.SERVER_TIMESTAMP,
            })
        else:
            user_sessions = (
                db_fs.collection(COLLECTION_NAME)
                .where("user_email", "==", user_email)
                .order_by("updated_at", direction=firestore.Query.DESCENDING)
                .get()
            )
            if len(user_sessions) >= 6:
                for old_doc in reversed(user_sessions):
                    if not old_doc.to_dict().get("is_pinned", False):
                        db_fs.collection(COLLECTION_NAME).document(old_doc.id).delete()
                        print(f"🗑️ [선입선출 청소] 6개 초과 삭제: {old_doc.id}")
                        break
            doc_ref.set({
                "user_email": user_email,
                "title": short_title,
                "badge_type": intent,
                "is_pinned": False,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "messages": new_history_payload,
            })
        print(f"📊 [Firestore] 히스토리 적재 완료 (세션: {session_id})")
    except Exception as fs_err:
        print(f"⚠️ [Firestore] 히스토리 적재 실패: {fs_err}")


@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, request: Request,
                        background_tasks: BackgroundTasks,
                        user_email: str = Depends(get_iap_user_email)):
    print(f"\n📥 [API 수신] 사용자 질문: {payload.current} (세션 계정: {user_email})")
    
    session_id = payload.sessionId if payload.sessionId and payload.sessionId != "default_session" else f"session_{user_email.split('@')[0]}_default"
    
    # LangGraph Firestore 체크포인터가 이전 대화 히스토리를 자동으로 복원하므로
    # 현재 질문만 전달 — 중복 메시지 적재 방지
    messages_list = [HumanMessage(content=payload.current)]

    initial_state = {
        "messages": messages_list,
        "current_intent": "",
        "top_intent": "",
        "pending_intents": [],
        "refined_query": "",
        "retrieved_docs": "",
        "sources": [],
        "user_info": {"email": user_email},
        "top_dept_code": "분류 불가",
        "bq_error_log": "",
        "bq_retry_count": 0
    }
    
    print("================ LangGraph 시작 ================")
    config = {"configurable": {"thread_id": session_id}}
    try:
        final_state = coway_agent_app.invoke(initial_state, config=config)
    except Exception as invoke_err:
        err_str = str(invoke_err)
        if "AUTH_REQUIRED_FOR:" in err_str:
            auth_email = err_str.split("AUTH_REQUIRED_FOR:")[-1].strip()
            print(f"🔐 [OAuth 필요] 워크스페이스 연동 인증 요청 → {auth_email}")
            return {
                "summary": {"summaryText": "구글 워크스페이스 연동을 위해 인증이 필요합니다. 잠시 후 인증 창이 열립니다."},
                "auth_required": True,
                "auth_email": auth_email,
                "chartData": None,
                "results": [],
                "links": "[]",
                "suggestions": [],
                "sessionId": session_id,
            }
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
            print(f"⚠️ [API 쿼터 초과] Gemini 429 — 사용자에게 안내 메시지 반환")
            return {
                "summary": {"summaryText": "현재 AI 서버 요청이 집중되어 일시적으로 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요."},
                "auth_required": False,
                "chartData": None,
                "results": [],
                "links": "[]",
                "suggestions": ["잠시 후 다시 질문해 주세요", "다른 질문을 먼저 시도해 보세요"],
                "sessionId": session_id,
            }
        raise
    print("================ LangGraph 종료 ================\n")
    
    raw_content = final_state["messages"][-1].content
    if isinstance(raw_content, list):
        final_answer_text = "".join([item["text"] for item in raw_content if isinstance(item, dict) and "text" in item])
    else:
        final_answer_text = str(raw_content)

    # 🎯 파이썬 백엔드 레이어에서 물리 구조체 전수 추출 및 본문 완전 세정 마감
    parsed_chart_data, final_answer_text = extract_structured_payload(final_answer_text, "CHART_DATA")
    parsed_source_data, final_answer_text = extract_structured_payload(final_answer_text, "SOURCE_REPORTS")

    # 🎯 추천 질문 파트 분리 프로세스 정비 (안전한 파이썬 표준 문법으로 수선 완료)
    clean_answer_body = final_answer_text
    suggestions_payload = []
    if "|||SUGGESTIONS|||" in clean_answer_body:
        parts = clean_answer_body.split("|||SUGGESTIONS|||")
        clean_answer_body = parts[0].strip()
        if len(parts) > 1:
            suggestions_payload = [re.sub(r'^[-\*\d\.\s]+', '', s).strip() for s in parts[1].strip().split("\n") if s.strip()]

    # 🔐 LangGraph 노드가 AUTH_REQUIRED 예외를 텍스트로 삼킨 케이스 감지 (노드 내부 except가 먼저 잡는 경우)
    if "AUTH_REQUIRED_FOR:" in clean_answer_body:
        auth_email = clean_answer_body.split("AUTH_REQUIRED_FOR:")[-1].split()[0].strip()
        print(f"🔐 [OAuth 필요 - 텍스트 감지] 워크스페이스 연동 인증 요청 → {auth_email}")
        return {
            "summary": {"summaryText": "구글 워크스페이스 연동을 위해 인증이 필요합니다. 잠시 후 인증 창이 열립니다."},
            "auth_required": True,
            "auth_email": auth_email,
            "chartData": None,
            "results": [],
            "links": "[]",
            "suggestions": [],
            "sessionId": session_id,
        }

    # Python이 추출한 소스를 기준으로 (인용 번호 [1][2][3] 순서와 일치)
    # LLM이 생성한 SOURCE_REPORTS는 doc_name 보완 전용으로만 사용
    source_results = list(final_state.get("sources", []))

    # LLM SOURCE_REPORTS로 doc_name 보완 (generic 이름인 경우 LLM의 정확한 이름으로 교체)
    if parsed_source_data:
        llm_url_map = {}
        for ps in parsed_source_data:
            d_url = ps.get("doc_url") or ps.get("url") or ""
            d_name = ps.get("doc_name") or ps.get("name") or ""
            if d_url and d_name:
                llm_url_map[d_url.strip()] = d_name
        for sr in source_results:
            sr_url = sr.get("doc_url", "")
            if sr_url in llm_url_map and sr.get("doc_name") in ("사내 규정 지식 파일", "", None):
                sr["doc_name"] = llm_url_map[sr_url]
        # LLM이 Python이 못 잡은 URL을 추가로 제시한 경우에만 append
        for ps in parsed_source_data:
            d_url = (ps.get("doc_url") or ps.get("url") or "").strip()
            if d_url and not any(s.get("doc_url") == d_url for s in source_results):
                source_results.append({
                    "doc_name": ps.get("doc_name") or ps.get("name") or "참조 문서",
                    "doc_url": d_url,
                    "links": ps.get("links") or "",
                    "images": ps.get("images") or []
                })

    # top_intent: Supervisor 최초 분류값 (Dispatcher가 "__DONE__"으로 덮어쓴 current_intent 대신 사용)
    intent = final_state.get("top_intent") or final_state.get("current_intent", "GENERAL")
    response_status = "SUCCESS"

    if intent == "RAG" and "찾을 수 없습니다" in clean_answer_body:
        response_status = "FAIL"
        clean_answer_body = "죄송합니다. 관련 규정을 찾지 못해 관련 부서로 내용 보강 요청을 드리겠습니다. 관련 질문은 담당부서로 직접 문의바랍니다."
        source_results = []  # 답변 없으면 출처도 표시 안함

    user_agent_str = request.headers.get("user-agent", "Unknown-Agent")
    log_payload = {
        "lastQ": payload.current,
        "user_email": user_email,
        "user_info": {"name": user_email.split('@')[0]}
    }

    extracted_links = final_state.get("links", "[]")
    if not extracted_links or extracted_links == "[]":
        if source_results:
            synchronized_link_list = [{"title": s.get("doc_name", "바로가기"), "url": s.get("links", "#")} for s in source_results if s.get("links")]
            extracted_links = json.dumps(synchronized_link_list, ensure_ascii=False)

    # BQ 로깅 + Firestore 히스토리 저장 — 응답 반환 후 백그라운드에서 처리
    background_tasks.add_task(
        log_to_analytics_v2,
        payload=log_payload,
        ai_response=clean_answer_body,
        response_status=response_status,
        intent=intent,
        top_dept_code=final_state.get("top_dept_code", "분류 불가"),
        user_agent=user_agent_str,
    )
    background_tasks.add_task(
        _save_history_background,
        session_id=session_id,
        user_email=user_email,
        intent=intent,
        current_msg=payload.current,
        answer=clean_answer_body,
        source_results=source_results,
        extracted_links=extracted_links,
        parsed_chart_data=parsed_chart_data,
    )

    return {
        "summary": { "summaryText": clean_answer_body },
        "chartData": parsed_chart_data,
        "results": source_results,
        "links": extracted_links,
        "suggestions": suggestions_payload,
        "sessionId": session_id
    }

# ====================================================================
# [SECTION 6] 👍 [WBS 4.4] 만족도조사 피드백 실시간 빅쿼리 자율 적재 엔진
# ====================================================================
@app.post("/api/feedback/log")
async def log_feedback_endpoint(payload: FeedbackLogRequest, user_email: str = Depends(get_iap_user_email)):
    print(f"📊 [피드백 엔드포인트 격발] 유형: {payload.type} / 계정: {user_email}")
    
    PROJECT_ID = "gcp-cw-ai-chatbot"
    DATASET_ID = "chatbot_analytics"
    TABLE_ID = "feedback_logs" 
    
    bq_client = bigquery.Client(project=PROJECT_ID)
    table_ref = bq_client.dataset(DATASET_ID).table(TABLE_ID)
    
    try:
        bq_client.get_table(table_ref)
    except Exception:
        print(f"🧱 [인프라 생성] '{TABLE_ID}' 장부가 존재하지 않아 신규 테이블 빌드를 개시합니다...")
        schema = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("user_email", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("user_query", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("feedback_type", "STRING", mode="REQUIRED"), 
            bigquery.SchemaField("reasons", "STRING", mode="REPEATED"),      
            bigquery.SchemaField("comment", "STRING", mode="NULLABLE"),      
        ]
        table = bigquery.Table(table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field="timestamp")
        bq_client.create_table(table)
        print(f"✅ [인프라 완료] 피드백 저장소 '{TABLE_ID}' 파티션 테이블 개설 대성공!")

    row_to_insert = [{
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "user_email": user_email,
        "user_query": payload.query,
        "feedback_type": payload.type,
        "reasons": payload.reasons, 
        "comment": payload.comment if payload.comment else ""
    }]
    
    try:
        errors = bq_client.insert_rows_json(table_ref, row_to_insert)
        if errors:
            print(f"❌ [BQ 피드백 에러] 적재 유실 발생: {errors}")
            return {"success": False, "error": str(errors)}
        print(f"📊 [BQ 피드백 적재 완료] 유형: {payload.type} / 계정: {user_email} ➔ 완공!")
        return {"success": True}
    except Exception as e:
        print(f"⚠️ [BQ 피드백 패닉] 런타임 크래시 예외 제어: {e}")
        return {"success": False, "error": str(e)}
        
# ====================================================================
# [SECTION 7] 💡 [WBS 4.2] 개선 제안 및 GCS 첨부파일 버킷 이관 엔진
# ====================================================================
@app.post("/api/feedback/save")
async def save_global_feedback_endpoint(payload: GlobalFeedbackSaveRequest, user_email: str = Depends(get_iap_user_email)):
    print(f"🎉 [개선 제안 접수] 제목: {payload.title} / 계정: {user_email}")
    now = datetime.datetime.now(datetime.timezone.utc)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    doc_id = f"FB-{now.strftime('%Y%m%d%H%M%S')}-{user_email.split('@')[0]}"

    # 1. Firestore 저장 (항상 동작 — 기본 백본)
    try:
        db_fs.collection("improvement_suggestions").document(doc_id).set({
            "user_email": user_email,
            "type": payload.type,
            "priority": payload.priority,
            "title": payload.title,
            "content": payload.content,
            "last_query": payload.lastQuery or "",
            "created_at": firestore.SERVER_TIMESTAMP
        })
        print(f"✅ [개선제안] Firestore 저장 완료: {doc_id}")
    except Exception as fs_err:
        print(f"⚠️ [개선제안] Firestore 저장 실패: {fs_err}")

    # 2. Google Sheets 저장 (FEEDBACK_SHEET_ID 환경변수 설정 시 활성화)
    sheet_id = os.environ.get("FEEDBACK_SHEET_ID", "")
    if sheet_id:
        try:
            import google.auth
            from googleapiclient.discovery import build as gapi_build
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
            sheets = gapi_build("sheets", "v4", credentials=creds)
            sheets.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Sheet1!A:G",
                valueInputOption="USER_ENTERED",
                body={"values": [[ts_str, user_email, payload.type, payload.priority, payload.title, payload.content, payload.lastQuery or ""]]}
            ).execute()
            print(f"✅ [개선제안] Google Sheets 저장 완료")
        except Exception as sheets_err:
            print(f"⚠️ [개선제안] Google Sheets 저장 실패: {sheets_err}")

    return {"success": True, "id": doc_id}

# ====================================================================
# [SECTION 8] 🔐 [WBS 2.5] 구글 워크스페이스 OAuth 2.0 인증 게이트웨이
# ====================================================================

@app.get("/api/auth/google")
async def initiate_google_oauth(email: str, request: Request):
    """OAuth 인증 URL 생성 — 프론트엔드가 팝업으로 오픈"""
    print(f"🔐 [OAuth 시작] 인증 URL 요청 → {email}")
    state = base64.urlsafe_b64encode(email.encode()).decode()
    auth_url = _build_google_auth_url(state)
    return {"auth_url": auth_url}


@app.get("/api/oauth2callback")
async def google_oauth2_callback_gateway(request: Request):
    """OAuth 인증 코드 수신 → 토큰 교환 → Firestore 저장"""
    print("🔐 [OAuth 콜백] 구글 인증 패킷 수신")
    error = request.query_params.get("error")
    if error:
        print(f"❌ [OAuth 거부] 사용자가 권한 승인을 거부했습니다: {error}")
        return HTMLResponse(content="""
            <html><body style='text-align:center;padding-top:100px;font-family:sans-serif;'>
                <h2 style='color:#dc2626;'>❌ 인증이 취소되었습니다</h2>
                <p>권한 승인이 거부되었습니다. 창을 닫고 다시 시도해주세요.</p>
                <script>setTimeout(() => { window.close(); }, 3000);</script>
            </body></html>
        """)

    code = request.query_params.get("code")
    state = request.query_params.get("state")

    try:
        user_email = base64.urlsafe_b64decode(state.encode()).decode()

        # flow.fetch_token() 대신 직접 POST — PKCE code_verifier 이슈 우회
        token_resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            raise Exception(f"토큰 교환 실패: {token_data.get('error')} — {token_data.get('error_description', '')}")

        _expires_in = token_data.get("expires_in", 3600)
        _expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=_expires_in)).isoformat()
        db_fs.collection("user_tokens").document(user_email).set({
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_expiry": _expiry,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, merge=True)

        print(f"✅ [OAuth 완료] 토큰 저장 성공 → {user_email}")
        return HTMLResponse(content="""
            <html><body style='text-align:center;padding-top:100px;font-family:sans-serif;'>
                <h2 style='color:#2563eb;'>🔒 구글 업무 시스템 인증 성공!</h2>
                <p>보안 세션 금고에 안전하게 등록되었습니다. 이 창을 닫으면 챗봇이 자동으로 재시도합니다.</p>
                <script>setTimeout(() => { window.close(); }, 2000);</script>
            </body></html>
        """)

    except Exception as e:
        print(f"❌ [OAuth 콜백 실패] {e}")
        return HTMLResponse(content=f"""
            <html><body style='text-align:center;padding-top:100px;font-family:sans-serif;'>
                <h2 style='color:#dc2626;'>❌ 인증 처리 중 오류가 발생했습니다</h2>
                <p>{str(e)}</p>
                <script>setTimeout(() => {{ window.close(); }}, 4000);</script>
            </body></html>
        """, status_code=500)

# ====================================================================
# [SECTION 9] ⏰ [WBS 2.0] 구글 드라이브 지식 베이스 자율 CRUD 스케줄러
# ====================================================================
from embed_and_load import main_sync_pipeline

@app.post("/api/sync-knowledge")
async def trigger_drive_sync_endpoint():
    print("⏰ [스케줄러 트리거] 새벽 2시 구글 공유드라이브 자율 CRUD 싱크 가동!")
    try:
        main_sync_pipeline() 
        return {"success": True, "message": "공유 드라이브 자율 동기화 완공"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ====================================================================
# [SECTION 10] 📜 [WBS 4.3] 파이어베이스 최근 6개 Rolling + 즐겨찾기 통제 엔진
# ====================================================================
@app.get("/api/chat/history/list")
async def get_chat_history_list(user_email: str = Depends(get_iap_user_email)):
    print(f"📜 [대시보드 로드] 계정: {user_email} ➔ 메타데이터 고속 스캔 가동")
    try:
        docs = db_fs.collection(COLLECTION_NAME)\
                    .where("user_email", "==", user_email)\
                    .order_by("updated_at", direction=firestore.Query.DESCENDING)\
                    .stream()
        
        history_list = []
        for doc in docs:
            data = doc.to_dict()
            history_list.append({
                "session_id": doc.id,
                "title": data.get("title", "새로운 대화"),
                "badge_type": data.get("badge_type", "GENERAL"),
                "is_pinned": data.get("is_pinned", False),
                "updated_at": data.get("updated_at").isoformat() if data.get("updated_at") else ""
            })
        # 즐겨찾기(pinned) 우선 정렬, 같은 그룹 내에서는 updated_at DESC 유지
        history_list.sort(key=lambda x: 0 if x['is_pinned'] else 1)
        return {"success": True, "sessions": history_list}
    except Exception as e:
        print(f"⚠️ [목록 조회 패닉] {e}")
        return {"success": False, "sessions": [], "error": str(e)}

@app.get("/api/chat/history/detail/{session_id}")
async def get_chat_history_detail(session_id: str, user_email: str = Depends(get_iap_user_email)):
    print(f"📂 [대화방 진입] 방 ID: {session_id} ➔ 파이어베이스 금고에서 대화 내역 및 구조체 일괄 매핑 인출")
    try:
        doc_ref = db_fs.collection(COLLECTION_NAME).document(session_id).get()
        if not doc_ref.exists:
            return {"success": False, "error": "존재하지 않거나 삭제된 대화방입니다."}
            
        data = doc_ref.to_dict()
        if data.get("user_email") != user_email:
            return {"success": False, "error": "본인 계정의 대화 기록만 열람 가능합니다."}
            
        return {"success": True, "history": data.get("messages", [])}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/chat/history/toggle-pin/{session_id}")
async def toggle_chat_session_pin(session_id: str, user_email: str = Depends(get_iap_user_email)):
    print(f"⭐ [즐겨찾기 토글] 방 ID: {session_id} ➔ 상태 전사 동기화 격발")
    try:
        doc_ref = db_fs.collection(COLLECTION_NAME).document(session_id)
        doc = doc_ref.get()
        if not doc.exists:
            return {"success": False, "error": "해당 대화방을 찾을 수 없습니다."}
            
        data = doc.to_dict()
        if data.get("user_email") != user_email:
            return {"success": False, "error": "권한이 없습니다."}
            
        current_pin_status = data.get("is_pinned", False)
        new_pin_status = not current_pin_status
        
        doc_ref.update({"is_pinned": new_pin_status})
        print(f"⭐ [토글 완료] 방 ID: {session_id} ➔ 신규 상태: {new_pin_status}")
        return {"success": True, "is_pinned": new_pin_status}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/api/chat/reset")
async def start_new_chat_session(user_email: str = Depends(get_iap_user_email)):
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    new_session_id = f"session_{user_email.split('@')[0]}_{now_str}"
    print(f"🔄 [새 세션 개설] 계정: {user_email} ➔ 신규 세션 ID 발급: {new_session_id}")
    return {"success": True, "session_id": new_session_id}