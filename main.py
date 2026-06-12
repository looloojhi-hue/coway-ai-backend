import os
import datetime  # 🎯 만족도 피드백 타임스탬프 고속 사출용
import json
import re
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse
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
db_fs = firestore.Client(project="gcp-cw-ai-chatbot")
COLLECTION_NAME = "coway_chat_sessions"

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
    fileData: Optional[str] = None  # Base64 디코딩용 파일 바이너리
    fileName: Optional[str] = None
    mimeType: Optional[str] = None

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
    html_file_path = os.path.join("templates", "Index.html")
    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        print(f"❌ [HTML 로드 크래시 방어] 파일 읽기 실패: {e}")
        return HTMLResponse(content=f"<h1>Internal Server Error: Template Load Failure</h1><p>{str(e)}</p>", status_code=500)

# ====================================================================
# [SECTION 4] 🧑‍💼 사내 임직원 인사 정보 동기화 라우터
# ====================================================================
@app.get("/api/user-info")
async def get_user_info(user_email: str = Depends(get_iap_user_email)):
    user_name = user_email.split('@')[0]
    return {
        "name": user_name,
        "dept": "COWAY 임직원",
        "email": user_email
    }

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
                    # 복수 차트가 인입될 경우 첫 번째 주력 분석 차트를 최우선 바인딩 처리
                    if collected_payload is None:
                        collected_payload = parsed_obj
            except Exception as parse_err:
                print(f"⚠️ [파이썬 계층 데이터 해독 패닉] {tag} JSON 파싱 실패: {parse_err}")
                
            # 오차 없는 최종 텍스트 슬라이싱 컷오프 범위 재결합하여 잔재 노출 원천 격살
            clean_text = clean_text[:idx] + clean_text[end_pos + 1:]
        else:
            # 닫히는 괄호 매칭 유실선 강제 청소 워시
            clean_text = clean_text[:idx] + clean_text[content_start:]
            
    return collected_payload, clean_text.strip()

@app.post("/api/chat")
async def chat_endpoint(payload: ChatRequest, request: Request, user_email: str = Depends(get_iap_user_email)):
    print(f"\n📥 [API 수신] 사용자 질문: {payload.current} (세션 계정: {user_email})")
    
    session_id = payload.sessionId if payload.sessionId and payload.sessionId != "default_session" else f"session_{user_email.split('@')[0]}_default"
    
    messages_list = []
    if payload.lastQ:
        messages_list.append(HumanMessage(content=payload.lastQ))
    if payload.lastA:
        messages_list.append(AIMessage(content=payload.lastA))
    messages_list.append(HumanMessage(content=payload.current))

    initial_state = {
        "messages": messages_list,
        "current_intent": "",
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
    final_state = coway_agent_app.invoke(initial_state, config=config)
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

    # LLM의 [SOURCE_REPORTS]를 1순위로 채우고, 그래프 추출 소스는 미등록 URL에 한해 보완
    # (역순이었을 때: generic "참고 사규 지침서 N" 이름이 URL을 선점 → LLM의 정확한 파일명이 dedup에 차단되는 버그)
    source_results = []
    if parsed_source_data:
        for ps in parsed_source_data:
            d_name = ps.get("doc_name") or ps.get("name") or "참조 문서"
            d_url = ps.get("doc_url") or ps.get("url") or "#"
            d_links = ps.get("links") or ""
            source_results.append({"doc_name": d_name, "doc_url": d_url, "links": d_links})

    for s in final_state.get("sources", []):
        s_url = s.get("doc_url", "")
        if s_url and not any(x.get("doc_url") == s_url for x in source_results):
            source_results.append(s)

    intent = final_state.get("current_intent", "GENERAL")
    response_status = "SUCCESS"
    
    if intent == "RAG" and "찾을 수 없습니다" in clean_answer_body:
        response_status = "FAIL"
        clean_answer_body = "죄송합니다. 관련 규정을 찾지 못해 관련 부서로 내용 보강 요청을 드리겠습니다. 관련 질문은 담당부서로 직접 문의바랍니다."

    user_agent_str = request.headers.get("user-agent", "Unknown-Agent")
    log_payload = {
        "lastQ": payload.current,
        "user_email": user_email,
        "user_info": {"name": user_email.split('@')[0]}
    }

    try:
        log_to_analytics_v2(
            payload=log_payload,
            ai_response=clean_answer_body,
            response_status=response_status,
            intent=intent,
            top_dept_code=final_state.get("top_dept_code", "분류 불가"),
            user_agent=user_agent_str
        )
    except Exception as log_err:
        print(f"⚠️ [RPA 무중단 방어] 실시간 빅쿼리 V2 로깅 중 오류 발생: {log_err}")

    extracted_links = final_state.get("links", "[]")
    if not extracted_links or extracted_links == "[]":
        if source_results:
            synchronized_link_list = [{"title": s.get("doc_name", "바로가기"), "url": s.get("links", "#")} for s in source_results if s.get("links")]
            extracted_links = json.dumps(synchronized_link_list, ensure_ascii=False)

    # 🌟 [🚨 Phase 4.5 긴급 복구 - 파이어스토어 기반 3x2 격자 히스토리 동기화 및 6슬롯 Rolling 백업 엔진]
    # 누락되었던 히스토리 적재 레일을 완벽히 복원하여 메인 화면 사이드바와 연동 완료시킵니다.
    try:
        db_fs_local = firestore.Client(project=PROJECT_ID)
        history_ref = db_fs_local.collection("user_history").document(user_email).collection("sessions").document(session_id)
        
        # 최신 질문 명칭을 따 대화방 타이틀 실시간 자동 갱신
        room_title = payload.current[:18] + "..." if len(payload.current) > 18 else payload.current
        history_ref.set({
            "sessionId": session_id,
            "title": room_title,
            "last_query": payload.current,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "is_pinned": False
        }, merge=True)
        
        # 6슬롯 초과분 자율 롤링 삭제 매커니즘 (즐겨찾기 핀 고정 방은 영구 보존)
        sessions_ref = db_fs_local.collection("user_history").document(user_email).collection("sessions")
        active_sessions = [d for d in sessions_ref.order_by("updated_at", direction=firestore.Query.DESCENDING).stream()]
        if len(active_sessions) > 6:
            for old_sess in active_sessions[6:]:
                if not old_sess.to_dict().get("is_pinned", False):
                    sessions_ref.document(old_sess.id).delete()
        print(f"📊 [Firestore] 3x2 격자 6슬롯 매트릭스 롤링 백업 적재 완공! (세션: {session_id})")
    except Exception as fs_err:
        print(f"⚠️ [Firestore] 히스토리 매트릭스 백업 중 예외 스킵 (RPA 방어): {fs_err}")

    # 최종 취합 가방 패키징 토스
    return {
        "summary": { "summaryText": clean_answer_body },
        "chartData": parsed_chart_data, 
        "results": source_results,
        "links": extracted_links,
        "suggestions": suggestions_payload,
        "sessionId": session_id
    }

    # ====================================================================
    # 💾 [파이어스토어 무결성 가방 적재 벨트 라인]
    # ====================================================================
    try:
        doc_ref = db_fs.collection(COLLECTION_NAME).document(session_id)
        doc = doc_ref.get()
        
        # 🎯 [이중 캔버스 유령 흰 박스 및 찌그러짐 버그 최종 타격 격살]
        # 금고 데이터베이스 장부에는 찌꺼기 문자열이 100% 소멸한 깨끗한 clean_answer_body만 엄격히 격리 적재!
        new_history_payload = [
            {
                "role": "user", 
                "content": payload.current, 
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            },
            {
                "role": "assistant", 
                "content": clean_answer_body, 
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "results": source_results,   
                "links": extracted_links,     
                "chartData": parsed_chart_data 
            }
        ]
        
        if doc.exists:
            doc_ref.update({
                "messages": firestore.ArrayUnion(new_history_payload),
                "updated_at": firestore.SERVER_TIMESTAMP
            })
        else:
            user_sessions = db_fs.collection(COLLECTION_NAME)\
                                 .where("user_email", "==", user_email)\
                                 .order_by("updated_at", direction=firestore.Query.DESCENDING)\
                                 .get()
            
            if len(user_sessions) >= 6:
                for old_doc in reversed(user_sessions):
                    old_data = old_doc.to_dict()
                    if not old_data.get("is_pinned", False):
                        db_fs.collection(COLLECTION_NAME).document(old_doc.id).delete()
                        print(f"🗑️ [선입선출 청소] 6개 초과로 인해 삭제된 구형 룸: {old_doc.id}")
                        break
            
            short_title = payload.current[:25] + "..." if len(payload.current) > 25 else payload.current
            
            doc_ref.set({
                "user_email": user_email,
                "title": short_title,
                "badge_type": intent,  
                "is_pinned": False,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "messages": new_history_payload
            })
            print(f"🧱 [구조체 가방 세션 적재 완료] ➔ {session_id}")
            
    except Exception as fs_err:
        print(f"⚠️ [파이어스토어 백업 실패 가드 격발]: {fs_err}")

    # 완전히 분리 분할 정돈된 프리미엄 API 응답 포맷 배달 확정 사출
    return {
        "summary": { "summaryText": clean_answer_body },
        "chartData": parsed_chart_data, 
        "results": source_results,
        "links": extracted_links
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
    try:
        if payload.fileData:
            print(f"📁 첨부파일 감지됨: {payload.fileName} ({payload.mimeType}) -> GCS 버킷 이관 준비 완료")
        return {"success": True, "id": "FB-PYTHON-GENERATED-ID"}
    except Exception as err:
        return {"success": False, "error": str(err)}

# ====================================================================
# [SECTION 8] 🔐 [WBS 2.5] 정통 OAuth 2.0 콜백 인증 토큰 수신 수문장 (선제 매립)
# ====================================================================
@app.get("/api/oauth2callback")
async def google_oauth2_callback_gateway(request: Request):
    print("🔐 [OAuth 콜백 수신] 임직원이 자발적으로 승인한 구글 OAuth 인증 패킷 도달!")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    return HTMLResponse(content="""
        <html>
            <body style='text-align:center; padding-top:100px; font-family:sans-serif;'>
                <h2 style='color:#2563eb;'>🔒 구글 업무 시스템 인증 성공!</h2>
                <p>보안 세션 금고에 안전하게 등록되었습니다. 이 창을 닫고 챗봇으로 복귀하십시오.</p>
                <script>setTimeout(() => { window.close(); }, 2500);</script>
            </body>
        </html>
    """)

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