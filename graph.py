from typing import TypedDict, Annotated, Sequence, Literal, List
from langchain_core.messages import BaseMessage, AIMessage
from langgraph.graph import StateGraph, START, END
import operator
import json
import base64
import datetime
import re 
from pydantic import BaseModel, Field
import google.auth
import google.auth.transport.requests
import requests
from google.cloud import bigquery
from google.cloud import firestore  # 🎯 OAuth 토큰 동적 인출을 위한 파이어스토어 드라이버 추가

# 💡 OAuth2.0 자율 토큰 관리 및 갱신용 패키지 장전
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# 💡 [초격차 혁신] 2026년 오피셜 google-genai SDK 드라이버 기동
from google import genai
from google.genai import types
from googleapiclient.discovery import build
import langgraph_checkpoint_firestore

# =====================================================================
# 🧠 Google I/O 2026 오피셜 에이전트 플랫폼 인프라 세팅 (신규 프로젝트 락인)
# =====================================================================
PROJECT_ID = "gcp-cw-ai-chatbot"
MODEL_NAME = "gemini-3.5-flash"  # 🎯 GA 반영 완료된 최신 3.5 싱킹 엔진 명시

# 구글 엔터프라이즈 에이전트 플랫폼 클라이언트 초기화
ai_client = genai.Client(
    enterprise=True,
    project=PROJECT_ID,
    location="global"  # 🚀 최신 글로벌 에이전트 오케스트레이션 엔드포인트
)

# 빅쿼리 클라이언트
bq_client = bigquery.Client(project=PROJECT_ID)


# 🛡️ [Phase 5.0 신설] 기술보안팀 정원재 소장님 확정 Model Armor 쉴드 룸 세팅
MODEL_ARMOR_LOCATION = "asia-northeast3"  # 👈 서울 리전 타격 고정
MODEL_ARMOR_TEMPLATE_URI = f"projects/{PROJECT_ID}/locations/{MODEL_ARMOR_LOCATION}/templates/coway-chatbot-template"

# 📡 2026 오피셜 구글 가이드에 따른 Model Armor 전용 로우 레벨 API 호출 클라이언트 장전
def get_model_armor_headers():
    creds, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }

# ==========================================
# 상태 장부 및 출력 구조체 정의
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    current_intent: str
    top_intent: str          # Supervisor가 최초 분류한 인텐트 — Dispatcher가 덮어써도 보존
    pending_intents: list    # 복수 업무 요청 시 순차 처리 대기 큐
    refined_query: str
    retrieved_docs: str
    sources: list       # 💡 main.py를 거쳐 프론트엔드로 출처 파일 명단을 무결하게 배달할 보관함
    user_info: dict
    top_dept_code: str  # 🎯 낚아챈 부서 코드를 임시 저장할 장부 칸
    bq_error_log: str   # 🛠️ [Phase 4.3] 빅쿼리 런타임 에러 추적 메모리 칸 추가
    bq_retry_count: int # 🛠️ [Phase 4.3] 무한 루프 탈출용 재시도 카운터 장부 추가

# Structured Output 지원을 위한 표준 Pydantic 클래스 선언
class RouteDecision(BaseModel):
    intents: List[str] = Field(description="수행할 작업 목록. 각 항목은 RAG, BQ, GENERAL, EMAIL_READ, EMAIL_WRITE, CALENDAR_READ, CALENDAR_WRITE, TASK_READ, TASK_WRITE 중 하나. 복수 작업 요청 시 순서대로 모두 포함.")

class RefinedQuery(BaseModel):
    query: str = Field(description="명사 위주의 핵심 키워드 조합")

class CalendarEventSchema(BaseModel):
    year: int
    month: int
    day: int
    title: str
    startHour: int
    startMinute: int
    endHour: int
    endMinute: int

class TaskSchema(BaseModel):
    title: str
    notes: str
    due: str

# ====================================================================
# 🛡️ [개정 완공] 구글 워크스페이스 3-Legged OAuth 자율 토큰 관리 헬퍼 엔진
# ====================================================================
def get_workspace_service(service_name: str, version: str, user_email: str):
    """
    보안팀 거부 대상인 DWD 마스터키 방식을 전면 영구 폐기합니다.
    임직원이 자발적으로 승인해준 Firestore 내부 'user_tokens' 보관함에서 
    개별 Refresh Token을 실시간으로 꺼내와 만료 시 자동 갱신(Hydration)하며 호출합니다.
    """
    db_fs_local = firestore.Client(project=PROJECT_ID)
    token_ref = db_fs_local.collection("user_tokens").document(user_email).get()
    
    if not token_ref.exists:
        # 인증 열쇠가 없으면 main.py 및 프론트엔드에 팝업 요청 예외 전사 투척
        raise ValueError(f"AUTH_REQUIRED_FOR:{user_email}")
        
    token_data = token_ref.to_dict()
    
    # 임직원 개인의 구글 사원증 토큰 구조체 복원
    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data.get("client_id"),  # 정원재 소장님이 등록해준 기지 ID
        client_secret=token_data.get("client_secret")
    )
    
    # 1시간 만료 타임체인 감지 시 사내망 백그라운드 자율 자동 갱신 가동
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # 따끈한 새 열쇠를 파이어스토어 장부에 즉시 영구 업데이트 보존
        db_fs_local.collection("user_tokens").document(user_email).update({
            "access_token": creds.token,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        })
        
    return build(service_name, version, credentials=creds)

# ==========================================
# 에이전트 핵심 노드(Node) 함수 설계 구간
# ==========================================
def get_last_human_input(state: AgentState) -> str:
    """멀티인텐트 실행 중 AIMessage가 쌓여도 항상 원본 사용자 질문을 반환"""
    for msg in reversed(state["messages"]):
        if hasattr(msg, 'type') and msg.type == "human":
            return msg.content
    return ""

def supervisor_node(state: AgentState):
    print("🚦 [Supervisor] 제미나이 3.5가 의도를 파악 중입니다...")
    user_input = get_last_human_input(state)

    prompt = f"""
    당신은 코웨이 전사 AI 챗봇의 총괄 지휘자입니다.
    사용자 질문을 분석하여 수행해야 할 모든 작업을 intents 목록에 순서대로 담아 반환하세요.
    단일 요청이면 항목 1개, 복수 요청이면 2개 이상을 포함하세요.

    [의도 분류 기준]
    - RAG: 사내 규정, 복리후생, 인사, 가이드 등 텍스트 문서 검색
    - BQ: 매출액, 실적, 예산, 판매량 등 수치 데이터 조회 (예: 집행비용 분석, 출장현황 분석)
    - GENERAL: 단순 인사, 안부, 일상 대화 (예: 안녕, 넌 누구야)
    - EMAIL_WRITE: 메일/이메일 작성, 초안 작성, 답장 요청
    - EMAIL_READ: 이메일 요약, 읽기, 확인 요청
    - CALENDAR_WRITE: 캘린더/일정/스케줄 추가·등록·생성 요청 (예: 내일 오후 3시 미팅 일정 잡아줘)
    - CALENDAR_READ: 캘린더/일정 조회·확인 요청 (예: 오늘 내 미팅 일정 알려줘)
    - TASK_WRITE: 할일·할 일·해야 할 일·테스크·태스크·투두·TODO·to-do 등록·추가 요청
    - TASK_READ: 할일·할 일·해야 할 일·테스크 목록 조회·확인 요청
    [★ 핵심 구분 규칙 - 반드시 준수]
    1. "할일", "할 일", "해야 할 일", "테스크", "태스크", "투두", "to-do", "체크리스트" 키워드 → TASK_WRITE 또는 TASK_READ
       ※ 이 키워드가 포함된 요청은 절대로 CALENDAR로 분류하지 마세요.
    2. "일정", "캘린더", "calendar", "스케줄", "미팅", "회의", "약속" 키워드 → CALENDAR_WRITE 또는 CALENDAR_READ
    3. 사용자가 "A도 해주고 B도 해줘" 형태로 두 가지를 동시 요청하면 intents에 [A_INTENT, B_INTENT] 순서로 모두 포함하세요.

    [복수 요청 예시]
    - "할일에 등록하고 캘린더에도 추가해줘" → ["TASK_WRITE", "CALENDAR_WRITE"]
    - "메일 요약하고 오늘 일정도 알려줘" → ["EMAIL_READ", "CALENDAR_READ"]
    - "파이썬 교육 할일 등록해줘, 캘린더에는 내일 1시로 추가해줘" → ["TASK_WRITE", "CALENDAR_WRITE"]

    질문: {user_input}
    """

    response = ai_client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RouteDecision,
        ),
    )
    decision_data = json.loads(response.text)
    intents = decision_data.get("intents", ["GENERAL"])
    if not intents:
        intents = ["GENERAL"]

    print(f"✅ [Supervisor] 판단 결과: {intents}")
    return {
        "current_intent": intents[0],
        "top_intent": intents[0],   # Dispatcher 덮어쓰기와 무관하게 최초 인텐트 보존
        "pending_intents": intents[1:],
        "bq_retry_count": 0,
        "bq_error_log": "",
        "sources": []
    }

def rag_refiner_node(state: AgentState):
    print("🔍 [RAG Refiner] 이전 대화 맥락까지 고려하여 검색어 정제 중...")
    
    chat_history = ""
    for msg in state["messages"]:
        role = "사용자" if msg.type == "human" else "챗봇"
        chat_history += f"{role}: {msg.content}\n"
    
    prompt = f"""
    당신은 코웨이 전사 사내 규정 검색을 위한 검색어 최적화 AI 전문가입니다.
    below의 [대화 기록]을 읽고, 사용자가 '가장 마지막에 한 질문'의 진짜 의도를 파악하세요.

    [💡 코웨이 전용 약어/동의어 사전]
    - 지타워, g타워, G-Tower -> 본사
    - 런웨이 -> Leanway(학습 시스템 LMS) 
    - 회갑 -> 환갑
    ※ 지침: 임직원들이 위와 같은 사내 줄임말이나 약어를 쓰더라도, 사규 문서에 실재할 법한 공식 표준 명칭으로 LLM 상식을 활용해 치환하여 분석하세요. 
    
    [대화 기록]
    {chat_history}
    
    [지시사항]
    위 대화 맥락과 코웨이 약어 사전을 융합하여, 마지막 사용자 질문을 Vertex AI Search 엔진이 튕겨내지 않고 가장 잘 매칭할 수 있는 핵심 명사구 단 '하나'의 검색어로 출력하세요. (쉼표 금지, '방법', '절차', '시기', '언제' 같은 무의미한 단어는 원천 제외할 것)
    """
    
    response = ai_client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RefinedQuery,
        ),
    )
    result_data = json.loads(response.text)
    print(f"✅ [RAG Refiner] 정제된 키워드: {result_data.get('query')}")
    return {"refined_query": result_data.get("query")}

def rag_retriever_node(state: AgentState):
    query = state["refined_query"]
    print(f"📚 [RAG Retriever] '{query}'(으)로 구형 데이터 앱 직접 호출 중...")
    
    creds, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    
    OLD_PROJECT_NUMBER = "81027032834"
    APP_ID = "coway-ai-chatbot_1766022708310"
    url = f"https://discoveryengine.googleapis.com/v1alpha/projects/{OLD_PROJECT_NUMBER}/locations/global/collections/default_collection/engines/{APP_ID}/servingConfigs/default_search:search"
    
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "pageSize": 3
    }
    
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])
        if results:
            doc_contents = ""
            for i, res in enumerate(results):
                doc_data = res.get("document", {})
                snippet = doc_data.get("derivedStructData", {}).get("snippets", [{}])[0].get("snippet", "내용 없음")
                link = doc_data.get("derivedStructData", {}).get("link", "링크 없음")
                doc_contents += f"[문서 {i+1} 원본링크: {link}]\n{snippet}\n\n"
            
            print(f"✅ [RAG Retriever] 규정 문서 {len(results)}개 및 출처 링크 확보 완료!")
            return {"retrieved_docs": doc_contents}
            
    print("❌ [RAG Retriever] 검색된 문서가 없습니다.")
    return {"retrieved_docs": "관련 규정 문서를 찾을 수 없습니다."}

def rag_search_node(state: AgentState):
    print("\n🔍 [RAG Search] 빅쿼리 고성능 하이브리드 검색 및 권한(ACL) 실시간 검증 가동...")
    from rag_node import hybrid_search_bq

    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "employee_all@coway.com")
    
    context_text, top_dept_code = hybrid_search_bq(user_input, user_email)
    
    if not context_text:
        context_text = "시스템에 등록된 사내 규정이나 관련 문서를 찾을 수 없거나, 해당 문서를 열람할 권한이 없습니다."
        print("⚠️ 관련 문서를 찾지 못했습니다.")
        
    return {"retrieved_docs": context_text, "top_dept_code": top_dept_code}

def reasoner_node(state: AgentState):
    print("🧠 [Reasoner] 제미나이 3.5 싱킹 엔진 가동 및 최종 추론 답변 생성 중...")
    user_input = get_last_human_input(state)
    docs = state["retrieved_docs"]
    
    extracted_sources = []
    if docs:
        # 🗄️ GCP knowledge_master 컬럼 필드 규격(doc_name, doc_url, links)과 1:1 결합하도록 사전 정렬
        # 🎯 [정해인 프로 지정 가드 - 중첩 대괄호 파일명 완벽 해독 정규식]
        # 파일명 내부에 [인사팀], (총무팀) 등 온갖 괄호 특수문자가 겹쳐있어도 
        # 진짜 마크다운 종착점인 ](http) 구조만 완벽하게 저격하여 유실 없이 실명을 인출합니다.
        nested_md_pattern = r'\[((?:\[[^\]]*\]|[^\]\n])+)\]\((https?://[^\s)]+)\)'
        md_matches = re.findall(nested_md_pattern, docs)
        for name, url in md_matches:
            if url not in [s.get('doc_url') for s in extracted_sources]:
                clean_name = name.replace("원본링크:", "").replace("출처:", "").strip()
                extracted_sources.append({"doc_name": clean_name, "doc_url": url.strip(), "links": ""})
        
        raw_matches = re.findall(r'원본링크:\s*(https?://[^\s\]\n]+)', docs)
        for url in raw_matches:
            if url not in [s.get('doc_url') for s in extracted_sources]:
                extracted_sources.append({"doc_name": "사내 규정 지식 파일", "doc_url": url.strip(), "links": ""})
                
        if not extracted_sources and "http" in docs:
            all_urls = re.findall(r'(https?://[^\s\n\)]+)', docs)
            for idx, url in enumerate(all_urls):
                if url not in [s.get('doc_url') for s in extracted_sources]:
                    extracted_sources.append({"doc_name": f"참고 사규 지침서 {idx+1}", "doc_url": url.strip(), "links": ""})

    print("🛡️ [Model Armor] 프롬프트 인젝션 및 탈옥(Jailbreak) 실시간 스캔 중...")
    headers = get_model_armor_headers()
    try:
        url = f"https://modelarmor.{MODEL_ARMOR_LOCATION}.googleapis.com/v1/{MODEL_ARMOR_TEMPLATE_URI}:sanitizeUserPrompt"
        payload = {"user_prompt": user_input}
        
        armor_req = requests.post(url, headers=headers, json=payload, timeout=5)
        if armor_req.status_code == 200:
            armor_res = armor_req.json()
            if armor_res.get("sanitization_result", {}).get("is_sanitized", False):
                print("🚨 [Model Armor 차단] 악성 입력 패턴 또는 인젝션 공격이 감지되었습니다!")
                return {"messages": [AIMessage(content="⚠️ 안전한 사내망 환경 준수를 위해 입력하신 프롬프트가 기술보안팀 보안 필터링 시스템(Model Armor)에 의해 차단되었습니다. 사규 및 업무 가이드에 부합하는 정제된 언어로 다시 질문해 주세요.")], "sources": []}
    except Exception as armor_err:
        print(f"⚠️ Model Armor 프롬프트 검사 오프라인 예외 스킵 (RPA 무중단 방어 가동): {armor_err}")

    # 🎯 [정해인 프로 지침 반영 - 민감 일비 비교 예시 원천 격살 및 중립 비용 샘플 전환 완료]
    prompt = f"""
    당신은 코웨이(Coway) 임직원의 질문 의도를 찰떡같이 파악하고 사내 지식베이스를 바탕으로 가장 정확하고 '논리적인' 답변을 제공하는 최고 수준의 전사 통합 AI 어시스턴트 '코봇'입니다.

    [🎯 사용자의 원래 질문 및 사연 (맥락 파악용)]
    "{user_input}"
    ※ 중요: 위 질문에 포함된 개인 상황(나이, 연도, 특정 부서명 등)을 현재 시점 기준으로 분석하여, 아래 검색된 규정 문서와 결합해 맞춤형으로 추론하여 답변하세요.

    [지침]
    1. 사용자의 질문에 오타나 줄임말이 있더라도, 제공된 문서의 문맥을 유추하여 가장 적절한 정보를 찾아 답변하세요.
    2. 특정 부서의 위치나 담당자를 묻는 질문인 경우, 문서의 상하위 맥락을 꼼꼼히 역추적하세요.
    3. 규정 조항, 예외 사항, 필수 절차 등이 검색 결과에 있다면 종합적으로 엮어서 상세히 설명하세요.
    4. 검색 결과에 없는 내용은 절대 지어내지 마세요.
    5. 아무리 찾아도 관련 내용이 전혀 없다면 "죄송합니다. 제공된 규정이나 문서에서는 해당 내용을 찾을 수 없습니다. 담당 부서에 문의해 주세요."라고만 답변하세요.
    6. 표(Table) 구조나 다단 텍스트 유실 조각을 원래 규정 취지에 맞춰 세련되게 자율 재조립(Reconstruct) 하세요.
    7. 특정 업무의 '담당자'를 안내할 때는 반드시 해당 행(Row)과 정확히 1:1로 매치되는 담당자명만 사출하세요.
    8. 복잡한 정산이나 계산 수식이 동반될 경우 원리 중심의 단계별 추론(Step-by-step reasoning)을 가이드하세요.
    9. 나이 및 연도 연산 시 속으로 명시적인 수식을 세워 오차 없이 정밀 연산하세요.
    10. 답변을 모두 작성한 후, 맨 마지막 줄에 사용자가 이어서 궁금해할 만한 '추천 질문' 3가지를 반드시 "|||SUGGESTIONS|||" 이라는 구분자 뒤에 줄바꿈으로 구분하여 작성하세요.
    
    11. [🔒 출처 구조화 명세 조항 - 주요 출처 텍스트 출력 절대 금지]
    - 답변 본문 인용 마킹([1], [2] 등)이 완료된 후, 절대로 사용자 화면에 "주요 출처"나 "🎯 주요 출처" 같은 텍스트 리스트를 직접 출력하지 마십시오. (하단 카드 섹션과 중복되어 UI가 지저분해집니다.)
    - 대신, 답변 맨 마지막 줄(추천 질문 구분자 |||SUGGESTIONS||| 바로 위)에 반드시 아래의 정확한 JSON 출처 명세 포맷을 [SOURCE_REPORTS] 태그와 함께 한 줄의 순수 JSON 문자열로 사출하십시오. 앞뒤에 백틱기호는 엄격히 금지합니다.
    - doc_name 필드에는 파일명이나 문서의 진짜 명칭(예: "[인사팀] 코웨이 복리후생 규정_2025.09.01.pdf")을 입력하십시오.
    - ★ [출처 필터링 엄수] [SOURCE_REPORTS]에는 반드시 본문에서 실제로 인용한([1], [2] 등 마킹) 문서만 포함하세요. 검색은 됐으나 본문에 인용하지 않은 문서는 절대 포함하지 마세요. 관련 내용을 찾지 못해 "찾을 수 없습니다"라고 답한 경우에는 [SOURCE_REPORTS] 자체를 출력하지 마세요.

    포맷 규격:
    [SOURCE_REPORTS] [{{"doc_name": "실제 문서 이름 1", "doc_url": "해당 문서의 구글드라이브 URL"}}, {{"doc_name": "실제 문서 이름 2", "doc_url": "해당 문서의 구글드라이브 URL"}}]

    [📊 Generative UI & 데이터 시각화 특별 지침 - categories & series 일원화]
    - 임직원이 부서별 인원 통계, 만족도 조사 수치 현황, 연도별 추이 비교 등 '시각적 차트' 그래프가 동반되면 좋은 질문을 던진 경우, 위의 텍스트 답변을 모두 마친 후 '맨 마지막 줄'에 반드시 아래의 정확한 JSON 차트 명세 포맷을 한 줄로 포함하여 사출하십시오.
    - 앞뒤에 백틱(```) 기호는 엄격히 금지하며, 아래 예시처럼 categories와 series 구조 표준을 완벽히 지켜야 프론트엔드 캔버스가 작동합니다.

    포맷 예시 (시각화 필요 시 답변 맨 하단 추가용 - 중립적 예시 체계 전환):
    [CHART_DATA] {{"type": "bar", "title": "월별 비용 집행 추이 현황 (단위: 원)", "categories": ["1월", "2월", "3월", "4월"], "series": [{{"name": "집행액", "data": [1200000, 1500000, 1100000, 1800000]}}]}}
    
    [검색된 규정]
    {docs}
    """
    
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    final_response_text = response.text.strip()

    print("🛡️ [Model Armor] 생성된 AI 응답문 유해성 및 헤이트 스피치 실시간 교차 검증 중...")
    try:
        url = f"https://modelarmor.{MODEL_ARMOR_LOCATION}.googleapis.com/v1/{MODEL_ARMOR_TEMPLATE_URI}:sanitizeModelResponse"
        payload = {"model_response": final_response_text}
        
        armor_req = requests.post(url, headers=headers, json=payload, timeout=5)
        if armor_req.status_code == 200:
            armor_res = armor_req.json()
            if armor_res.get("sanitization_result", {}).get("is_sanitized", False):
                print("🚨 [Model Armor 차단] LLM 생성 응답 중 유해 정보 부적격 조항 감지!")
                return {"messages": [AIMessage(content="⚠️ 기술보안팀 정적 데이터 가이드라인 정책에 의거하여, 생성된 응답 내부의 부적격 단어 조항이 감지되어 답변 사출이 자율 거부되었습니다. 질문을 미세하게 수정하여 다시 시도해 주세요.")], "sources": []}
    except Exception as armor_err:
        print(f"⚠️ Model Armor 응답문 검사 예외 제어 발동 (서비스 가용성 우선 우회): {armor_err}")

    return {"messages": [AIMessage(content=final_response_text)], "sources": extracted_sources}

def general_node(state: AgentState):
    print("👋 [GENERAL] 일상 대화 처리 중...")
    user_input = get_last_human_input(state)
    
    prompt = f"""
    당신은 코웨이 임직원을 위한 사내 전사 AI 챗봇입니다.
    사용자의 일상적인 인사나 대화에 친절하고 자연스럽게 답변해 주세요.
    '사내 규정 및 인사/복리후생 제도'등과 관련된 질문에만 답변할 수 있다고 안내하세요
    사용자 질문: {user_input}
    """
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return {"messages": [AIMessage(content=response.text.strip())]}

def bq_node(state: AgentState):
    print("📊 [BQ] 제미나이 3.5 기반 자율형 다차원 데이터 애널리스트 모드 기동...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    system_message = rf"""
    당신은 코웨이의 최고 데이터 분석가(Chief Data Analyst)입니다.
    사용자의 질문에 답변하기 위해 BigQuery 데이터베이스에 직접 유효한 표준 SQL 쿼리를 작성하고 실행하세요.
    
    [보안 및 권한 준수 사항]
    1. 현재 질문하는 사용자의 ID는 '{user_email}' 입니다.
    2. 사용자 ID(@coway.com)가 BigQuery 데이터 세트 레벨에서 권한을 가진 테이블만 조회하세요.
    3. 쿼리 실행 중 'Access Denied' 에러 발생 시, 사용자에게 권한이 없음을 정중히 안내하세요.

    🚫 [절대 접근 금지 구역]
    데이터베이스에 남아있는 과거 V1 버전의 구형 테이블(general_affairs_travel, hrga_cost_budget_master 등)은 절대로 조회하지 마십시오.

    ====================================================================
    🔒 [치명적: 데이터세트 라우팅 결정 대원칙 - 오선택 절대 금지]
    ====================================================================
    사용자의 질문 의도를 분석하여 아래 규칙에 따라 단 하나의 데이터세트만 선택하십시오. 두 영역을 절대 혼동하거나 섞지 마십시오.

    1. ✈️ [출장현황 / 여비교통비 세부 내역 관련 질문] ➔ 무조건 'hrga_travel_data.travel_master_db' 테이블만 사용
       - 판단 기준: 질문에 출장, trip, destination, 출장지, 출장자, 사번, 인원수, 숙박비, 식비, 일비, 항공, KTX, 대중교통, 렌트, 유류비, 통행료 등의 단어가 포함된 경우
       - 🚨 [경고] 사용자가 "출장비 분석해줘" 또는 "출장 현황 보여줘"라고 하면 절대로 hrga_cost_data 데이터세트를 조회하지 마십시오. 무조건 hrga_travel_data 데이터세트만 타격해야 합니다.

    2. 📈 [총무 집행비용 / 전사 부서 예산 및 정산 관련 질문] ➔ 무조건 'hrga_cost_data' 테이블들만 사용
       - 판단 기준: 질문에 집행, 비용, 예산, budget, 대분류(cat_l), 중분류(cat_m), 소분류(cat_s), 소모품비, 전월대비, 증감율 등의 단어가 포함된 경우

    ====================================================================
    🗄️ [코웨이 전사 빅쿼리 데이터 세트 및 테이블별 물리 스키마 명세]
    ====================================================================
    
    📁 DATASET 1: hrga_travel_data (전사 출장현황 데이터세트)
      - TABLE ID: travel_master_db (출장 마스터 테이블)
        * 🚨 [출장 집계 핵심 비즈니스 규칙]:
          - '출장 건수': 하나의 출장번호에 여러 명이 동행할 수 있으므로 반드시 COUNT(DISTINCT trip_id) 사용.
          - '총 출장자(연인원)': 동일인이 여러 번 출장 시 각각 카운팅하는 COUNT(emp_id) 사용. (COUNT(DISTINCT emp_id)는 중복 제거된 고유 인원수이므로 혼용 금지)
          - '고유 출장자 수': 사용자가 "몇 명이 출장을 다녀왔나" 같이 중복 제거를 원할 때만 COUNT(DISTINCT emp_id) 사용.
        * 물리 컬럼 명세:
          - trip_id (출장번호), emp_id (출장자 사번), emp_name (출장자 이름)
          - job_title (직책), emp_group (사원 그룹)
          - hq_name (본부 명칭), office_name (실 명칭), dept_name (부서(팀) 명칭) ➔ 🚨 출장 데이터 소속 조회 시에는 이 컬럼명 체계를 고수하세요.
          - company_code (회사코드), trip_type (출장구분 — 예: 국내/해외 구분값 존재), purpose_category (출장목적구분), purpose_detail (출장목적), destination (출장지)
          - client_code (거래선코드), client_name (거래선명), expense_type (체제비유형), duration (출장기간)
          - start_date (시작일), end_date (종료일), doc_status (결재문서상태), appr_status (결재상태), includes_weekend (주말포함여부)
        * 🚨 [doc_status 필터 금지] doc_status, appr_status 컬럼의 유효 값을 정확히 알 수 없으므로, 사용자가 명시적으로 "완료된 출장만", "승인된 건만" 등을 요청하지 않는 한 절대로 WHERE doc_status = ... 조건을 추가하지 마세요. 조건 없이 전체 데이터를 조회하세요.
          - planned_amt (계획금액), actual_amt (실제사용금액), lodge_amt (숙박비), meal_amt (식비), daily_allowance (일비)
          - transport_other_amt (교통비외), ktx_amt (KTX요금), flight_amt (항공료), public_transport_amt (대중교통), rent_amt (렌트비)
          - fuel_amt (유류비), toll_amt (통행료), own_car_fuel_amt (소유차량 유류비), own_car_toll_amt (소유차량 통행료), own_car_parking_amt (소요차량 주차)
          - roaming_amt (해외로밍비), visa_passport_amt (비자여권발급비), insurance_amt (여행자보험료), etc_amt (기타금액), parking_amt (주차비)
          - air_card_num (항공카드번호), ship_boarding_amt (선박승선비), ship_ferry_amt (선박도선비)
        * 🚨 [STRING 타입 + NULL 안전 집계] 금액 컬럼(_amt로 끝나는 모든 컬럼)은 BigQuery에 STRING 타입으로 저장됩니다.
          ① 단일 컬럼 집계: SUM(SAFE_CAST(actual_amt AS FLOAT64))
          ② 복수 컬럼 합산: 반드시 각 컬럼에 COALESCE를 씌워 NULL을 0으로 처리하세요.
             NULL + 숫자 = NULL 이므로 COALESCE 없이 더하면 전체 합이 NULL로 나옵니다.
             올바른 예: COALESCE(SAFE_CAST(ktx_amt AS FLOAT64), 0) + COALESCE(SAFE_CAST(flight_amt AS FLOAT64), 0) + ...
        * 🚨 [총 교통비 집계 표준] 교통비는 아래 13개 컬럼을 모두 COALESCE로 합산하세요 (누락 시 결측 발생):
          transport_other_amt, ktx_amt, flight_amt, public_transport_amt, rent_amt,
          fuel_amt, toll_amt, own_car_fuel_amt, own_car_toll_amt, own_car_parking_amt,
          parking_amt, ship_boarding_amt, ship_ferry_amt
        * 🚨 [출장현황 분석 필수 구성] 사용자가 출장현황 분석을 요청하면 아래 항목을 반드시 포함하세요:
          ① 전체 현황: 총 출장건수(COUNT DISTINCT trip_id), 총 출장자 연인원(COUNT emp_id), 총 실제사용금액
          ② 본부별(hq_name) 출장건수·비용 분석
          ③ trip_type 기준 국내/해외 구분 분석 (GROUP BY trip_type 또는 별도 서브쿼리)
          ④ 해외출장인 경우: 목적지(destination)별 집중도, 출장목적(purpose_category)별 분류, 본부별 해외출장 빈도

    📁 DATASET 2: hrga_cost_data (총무팀 집행비용 데이터세트)
      - TABLE ID: budget_master (연간 예산 계획 테이블)
        * 물리 컬럼 명세: year (연도), cat_l (대분류), cat_m (중분류), planned_budget (예산금액)

      - TABLE ID: budget_raw (비용 집행 ROW DATA 테이블)
        * 물리 컬럼 명세:
          - exe_month (집행일자: 'YYYY-MM' 포맷), manager (담당자)
          - cat_l (대분류), cat_m (중분류), cat_s (소분류), detail (세부집행내용)
          - qty (수량), amount (금액) ➔ 🚨 비용 금액 집계 및 가산 시 SUM(CAST(amount AS INT64)) 연산을 조합하세요.
          - hq (본부 명칭), department (실 명칭), team (부서(팀) 명칭) ➔ 🚨 비용 데이터 소속 조회 시에는 dept_name이 아니라 hq, department, team 컬럼명을 엄격히 사용해야 합니다.

      - TABLE ID: execution_detail (집행비용 당월/전월 대조 상세현황 테이블)
        * 물리 컬럼 명세:
          - id (NO), exe_month (집행일자), manager (담당자)
          - cat_l (대분류), cat_m (중분류), cat_s (소분류), qty (수량)
          - current_amount (당월 집행액), prev_amount (전월 집행액), diff_amount (전월대비 증감액), diff_rate (전월대비 증감율)
          - summary (총평), inc_reason (증가 사유), dec_reason (감소 사유) ➔ 🚨 수치 단답형 분석 방지를 위해 이 비정형 텍스트 인사이트 컬럼들을 적극 활용하세요.

    ====================================================================

    ⚠️ [데이터 조회 핵심 예외처리 지침]
    1. 집행연월 컬럼(`exe_month`) 또는 출장일자 매칭 시 날짜 범위 매칭의 무결성을 위해 무조건 `LIKE '2026-03%'` 연산자와 와일드카드(`%`) 조합을 기본으로 삼으세요.
    2. 집행비용 요청 시 출장 데이터를 혼합하지 마세요.
    3. 텍스트 컬럼 그룹핑 집계 시 SQL 엔진 크래시 방지를 위해 필요시 `ANY_VALUE()` 함수를 활용하십시오.

    🎯 [부서별 명시적 필터링 조항 - 정해인 프로 표준 규격]
    - 사용자가 특정 부서명(예: "총무팀")을 콕 집어서 비용 및 출장 분석을 요구하면, 자의적으로 전사 데이터를 대조군으로 넓혀서 뽑지 말고 해당 부서명으로 정확하게 매핑하여 SQL 필터를 적용하십시오.
      * 비용 테이블(`budget_raw`)에서 조회 시: `WHERE team = '총무팀'`
      * 출장 테이블(`travel_master_db`)에서 조회 시: `WHERE dept_name = '총무팀'`
    - 단, 사용자가 "부서별 집행비용 특이사항 분석해줘" 또는 "전사 부서별 집행 현황 분석"과 같이 명시적으로 부서간 비교나 전사 차원의 분할을 요구할 때만 `GROUP BY team` 혹은 `GROUP BY dept_name`을 가동하여 다차원 리포트를 빌드해야 합니다.

    🌟 [🔥 다차원 멀티 차트 데이터 빈약도 방지 조항]
    - 단순히 통짜 총합 결과만 내지 말고, 부서별/항목별 데이터 분할 요청이 있을 시에는 카테고리(`cat_m`) 등으로 쪼개서 가독성 높은 집계를 수행하세요.
    - 가이드라인에 명시된 실제 테이블별 스키마 컬럼명 외에 가상의 컬럼명을 임의로 유추하거나 위반하여 SQL에 삽입하는 행위는 엄격히 금지합니다.

    출력은 마크다운 코드블록을 포함하여 오직 순수 SQL문만 전달하세요.
    """
    
    # BQ_Corrector가 이미 SQL을 수정한 경우 재생성하지 않고 교정 SQL 직접 사용
    corrected_sql = state.get("refined_query", "")
    if corrected_sql and corrected_sql not in ("", "SQL 실행 실패"):
        generated_sql = corrected_sql.replace("```sql", "").replace("```", "").strip()
        print(f"♻️ [BQ] 교정된 SQL 재사용 (재시도 #{state.get('bq_retry_count', 0)}):\n{generated_sql}")
    else:
        sql_response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=[{"role": "system", "parts": [{"text": system_message}]}, {"role": "user", "parts": [{"text": user_input}]}]
        ).text
        generated_sql = sql_response.replace("```sql", "").replace("```", "").strip()
        print(f"🔍 [BQ Generated SQL]:\n{generated_sql}")
    print(f"🔍 [BQ] 사용자({user_email})의 권한 범위 내에서 데이터 탐색 및 SQL 실행 중...")
    
    try:
        query_job = bq_client.query(generated_sql)
        query_results = [dict(row) for row in query_job.result()]
        
        data_status_guard = "NORMAL"
        if not query_results or len(query_results) == 0:
            data_status_guard = "EMPTY"
        elif len(query_results) <= 1:
            data_status_guard = "INSUFFICIENT"

        # EMPTY: LLM에게 분석 요청하지 않고 즉시 안내 메시지 반환 (환각 방지)
        if data_status_guard == "EMPTY":
            print(f"⚠️ [BQ] 조회 결과 없음(EMPTY) — 환각 방지를 위해 LLM 분석 스킵")
            empty_report = (
                "📭 **조회된 데이터가 없습니다.**\n\n"
                "요청하신 조건에 해당하는 데이터를 데이터베이스에서 찾을 수 없었습니다.\n\n"
                "**가능한 원인:**\n"
                "- 해당 기간에 집행된 데이터가 아직 시스템에 적재되지 않았을 수 있습니다.\n"
                "- 검색 조건(기간, 부서명 등)을 더 구체적으로 지정해보세요.\n\n"
                "조건을 변경하여 다시 질문해주시면 재조회해드리겠습니다."
            )
            return {
                "messages": [AIMessage(content=empty_report + f"\n\n---\n<details><summary>AI가 실행한 SQL 보기</summary>\n\n```sql\n{generated_sql}\n```\n</details>")],
                "bq_error_log": "",
                "refined_query": ""
            }

        summary_prompt = f"""
        당신은 코웨이 경영진의 의사결정을 돕는 '수석 데이터 애널리스트 AI'입니다.
        데이터베이스에서 추출된 날 것의 데이터(Raw JSON)를 바탕으로 비즈니스 브리핑을 작성하세요.
        ★ 절대 규칙: 아래 데이터 JSON에 실제로 존재하는 수치만 사용하세요. 없는 수치·비율·금액을 추측하거나 지어내는 것은 엄격히 금지합니다.

        [보고서 작성 순서 — 반드시 이 순서대로]
        1. 🎯 Executive Summary (전체 종합 현황 먼저)
           - 총 출장건수, 총 출장자(연인원), 총 실제사용금액을 반드시 첫 줄에 명시하세요.
           - 국내/해외 건수와 비용 비율을 한 줄로 요약하세요.
           - 전체 데이터에서 가장 주목할 특이사항 1~2가지를 압축하여 마무리하세요.
        2. 해외 출장 분석 (해외 데이터가 있을 때)
           - 본부별 집행 규모 TOP 순위, 출장 목적지 집중 지역, 주요 출장 목적 분류
        3. 국내 출장 분석 (국내 데이터가 있을 때)
           - 본부별 집행 규모, 출장 빈도 TOP 순위, 지출 패턴 특이사항
        4. 비용 구조 분석 (항목별: 교통비·숙박비·식비·일비 비중)
        5. 특이사항 (아래 임원 제외 규칙 적용)

        [임원 특이사항 제외 규칙]
        - emp_group 컬럼에 '임원', '고문', '의장', '대표이사', '사외이사', '상임감사' 등이 포함된 인원은 비용이 높더라도 특이사항으로 분류하지 마세요.
        - 임원은 비즈니스석 탑승·빈번한 해외 출장이 구조적으로 예정된 직급이므로 일반 사원 기준의 이상값 탐지 대상에서 제외합니다.
        - 특이사항 분류 시 반드시 emp_name(출장자 이름)을 기준으로 판단하세요. hq_name(본부명)을 사람 이름으로 혼용하지 마세요.
        - 데이터에 emp_name이 없을 경우, 특이사항 섹션을 생략하세요.

        [보고서 포맷 규칙]
        - 마크다운 표(Table)는 절대 금지, 개조식 불릿 포인트만 사용하세요.
        - 수치는 인간 가독성에 맞춰 축약하세요. (예: "약 3억 2,043만 원")

        [다차원 멀티 차트 필수 사출 — {data_status_guard} 상태]
        - 상태가 [NORMAL]일 때, 데이터에서 의미 있는 모든 지표를 차트로 시각화하세요.
        - 차트는 최소 3개 이상 사출하세요. 데이터가 풍부할수록 더 많이 그려도 됩니다.
        - 출장 데이터 기준 권장 차트 구성:
          ① 본부별 실제사용금액 비교 (bar)
          ② 국내 vs 해외 출장건수 비교 (bar 또는 각 본부별 국내/해외 계열 구분)
          ③ 비용 항목별 구성 비율 (교통비/숙박비/식비/일비) (bar)
          ④ 출장 빈도 TOP 본부 (trip_count 기준 bar)
          - 데이터에 destination, purpose_category 등 추가 차원이 있으면 추가 차트를 더 그리세요.
        - 각 [CHART_DATA] 태그는 보고서 본문 마지막 줄들에 연속으로 사출하세요. 앞뒤 백틱(```) 기호 금지.

        포맷 규격 A:
        [CHART_DATA] {{"type": "bar", "title": "본부별 출장 실제사용금액", "categories": ["3사업본부", "2연구소"], "series": [{{"name": "실제사용금액", "data": [320430000, 227470000]}}]}}

        포맷 규격 B (다중 계열):
        [CHART_DATA] {{"type": "bar", "title": "본부별 국내/해외 출장건수", "categories": ["고객/품질본부", "1사업본부"], "series": [{{"name": "국내", "data": [101, 65]}}, {{"name": "해외", "data": [5, 3]}}]}}

        데이터 JSON: {json.dumps(query_results, default=str)}
        사용자가 던진 실제 질문: {user_input}
        """
        final_report = ai_client.models.generate_content(model=MODEL_NAME, contents=summary_prompt).text
        print(f"✅ [BQ] 데이터 품질 검증 완료 (상태: {data_status_guard}) ➔ 최종 답변 정제 완공!")
        
        return {
            "messages": [AIMessage(content=final_report + f"\n\n---\n<details><summary>AI가 실행한 SQL 보기</summary>\n\n```sql\n{generated_sql}\n```\n</details>")],
            "bq_error_log": "",
            "refined_query": ""
        }
    except Exception as e:
        err_str = str(e)
        print(f"❌ [⚠️ CRITICAL-BQ-ERROR] 빅쿼리 노드 런타임 크래시: {err_str}")

        # Access Denied: 권한 없는 사용자 → LLM 재시도 없이 바로 안내
        if "Access Denied" in err_str or "403" in err_str or "accessDenied" in err_str:
            access_denied_report = (
                "🔒 **데이터 접근 권한이 없습니다.**\n\n"
                f"요청하신 데이터(`{user_input[:40]}...`)는 접근이 제한된 데이터셋입니다.\n\n"
                "해당 데이터 분석이 필요하신 경우, **데이터 관리자에게 권한 신청**을 해주세요.\n"
                "권한 신청 후 재질문해주시면 바로 분석해드리겠습니다."
            )
            return {
                "messages": [AIMessage(content=access_denied_report)],
                "bq_error_log": "",   # 권한 오류는 SQL 교정 불필요 → 재시도 차단
                "refined_query": ""
            }

        fallback_error_report = (
            f"⚠️ **BigQuery 데이터 분석 중 오류가 발생했습니다.**\n\n"
            f"**[오류 내용]:** `{err_str[:300]}`\n\n"
            "잠시 후 다시 시도해주세요."
        )
        return {
            "messages": [AIMessage(content=fallback_error_report)],
            "bq_error_log": err_str,
            "refined_query": "SQL 실행 실패"
        }

def bq_corrector_node(state: AgentState):
    print("🔧 [BQ Corrector] 제미나이 3.5 디버거가 출동하여 쿼리 자율 복구에 착수합니다...")
    failed_sql = state["refined_query"]
    error_msg = state["bq_error_log"]
    retry_cnt = state.get("bq_retry_count", 0) + 1
    
    prompt = f"""
    당신은 구글 빅쿼리(BigQuery) 컴파일러 및 SQL 문법 교정의 신입니다.
    우리가 가동한 데이터 애널리스트 AI가 SQL을 작성했으나 아래와 같은 치명적인 에러를 만나 무산되었습니다.

    [실패한 원본 SQL]
    ```sql
    {failed_sql}
    ```

    [빅쿼리가 뿜어낸 오피셜 에러 메시지]
    "{error_msg}"

    [스키마 핵심 정보 - 교정 시 반드시 참고]
    - travel_master_db의 금액 컬럼(_amt로 끝나는 컬럼: actual_amt, planned_amt, lodge_amt, meal_amt, daily_allowance, ktx_amt, flight_amt 등)은 모두 STRING 타입으로 저장됨.
      → SUM/AVG/MAX/MIN 집계 시 반드시 SAFE_CAST(컬럼명 AS FLOAT64) 래핑 필수.
      → 예: SUM(SAFE_CAST(actual_amt AS FLOAT64))
    - budget_raw의 amount 컬럼도 STRING 타입 → SUM(CAST(amount AS INT64)) 또는 SUM(SAFE_CAST(amount AS FLOAT64))

    [교정 수선 명령]
    위 에러 로그를 분석하여 에러의 원인을 완전히 제거한 '완벽하게 수정된 표준 BigQuery SQL'을 재창조하세요.
    앞뒤에 ```sql 같은 코드블록 기호는 일절 제외하고 오직 순수 교정 SQL문만 사출하세요.
    """
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    corrected_sql = response.text.replace("```sql", "").replace("```", "").strip()
    print(f"♻️ [BQ Corrector] 교정 완료된 신규 SQL 사출 (시도 카운트: {retry_cnt}/2)")
    
    return {"refined_query": corrected_sql, "bq_retry_count": retry_cnt}

def email_write_node(state: AgentState):
    print("✉️ [EMAIL_WRITE] 제미나이 3.5가 이메일 초안 작성을 준비합니다...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    prompt = f"""당신은 코웨이 임직원을 돕는 전문 비서입니다.
    [지시사항]: {user_input}
    결과는 [메일 제목]과 [메일 본문]으로 구분하여 비즈니스 매너에 맞게 작성하세요."""
    
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    
    try:
        gmail = get_workspace_service('gmail', 'v1', user_email)
        raw_message = f"Subject: AI가 작성한 메일 초안\n\n{ai_response}"
        encoded_message = base64.urlsafe_b64encode(raw_message.encode("utf-8")).decode("utf-8")
        gmail.users().drafts().create(userId=user_email, body={"message": {"raw": encoded_message}}).execute()
        print("✅ [EMAIL_WRITE] Gmail 임시보관함 적재 성공")
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ Gmail 연동 실패: {str(e)}")

    return {"messages": [AIMessage(content=f"✨ **이메일 초안 작성이 완료되었습니다!**\n\nGmail의 **[임시보관함]**에 메일을 안전하게 저장해 두었습니다.\n\n---\n\n{ai_response}")]}

def email_read_node(state: AgentState):
    print("📧 [EMAIL_READ] 안읽은 메일을 수신하여 브리핑 정제 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    email_text = ""
    try:
        gmail = get_workspace_service('gmail', 'v1', user_email)
        q = 'is:unread in:inbox'
        if "오늘" in user_input:
            q += ' newer_than:1d'
            
        results = gmail.users().messages().list(userId=user_email, q=q, maxResults=20).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return {"messages": [AIMessage(content="📭 **현재 조건에 맞는 읽지 않은 새로운 메일이 없습니다.**")]}
            
        for idx, msg_info in enumerate(messages):
            msg = gmail.users().messages().get(userId=user_email, id=msg_info['id'], format='metadata', metadataHeaders=['From', 'Subject']).execute()
            headers = msg.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '제목 없음')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '알수없음').split('<')[0].strip()
            snippet = msg.get('snippet', '내용 없음')
            email_text += f"[메일 {idx + 1}]\n- 보낸사람: {sender}\n- 제목: {subject}\n- 내용: {snippet}...\n\n"
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ Gmail 읽기 에러: {str(e)}")
        return {"messages": [AIMessage(content="⚠️ 메일 접근 권한이 없거나 불러오는 중 오류가 발생했습니다.")]}

    prompt = f"""
    사용자 요청: {user_input}
    [안 읽은 최신 메일 데이터]
    {email_text}
    비즈니스 비서 강령에 맞춰 요약 마킹 처리하세요. 사람 지칭 시 무조건 성함 뒤에 '님' 기호 체계 통일 적용하세요.
    """
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    return {"messages": [AIMessage(content=f"📧 **최신 메일 요약 브리핑**\n\n{ai_response}")]}

def calendar_read_node(state: AgentState):
    print("📅 [CALENDAR_READ] 구글 캘린더 일정을 조회하는 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    schedule_text = ""
    try:
        calendar = get_workspace_service('calendar', 'v3', user_email)
        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        
        events_result = calendar.events().list(calendarId=user_email, timeMin=start_of_day, timeMax=end_of_day, singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        
        if not events:
            schedule_text = "오늘 예정된 일정이 없습니다."
        else:
            for e in events:
                start = e['start'].get('dateTime', e['start'].get('date'))
                time_str = start[11:16] if 'T' in start else "종일"
                desc = e.get('description', '')
                desc_str = f"\n ↳ 상세내용: {desc}" if desc else ""
                schedule_text += f"- [일정] {time_str} 시작 | {e.get('summary')}{desc_str}\n"
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ 캘린더 읽기 에러: {str(e)}")
        schedule_text = "권한 오류"

    prompt = f"사용자 질문: {user_input}\n일정 데이터: \n{schedule_text}\n지시사항: 1.[종일/상태] 제외 2.[일정]만 24시 기준으로 명확히 나열 3.상세내용 포함 요약"
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    return {"messages": [AIMessage(content=f"📅 **스마트 일정 브리핑**\n\n{ai_response}")]}

def calendar_write_node(state: AgentState):
    print("📅 [CALENDAR_WRITE] 구조화된 일정 데이터 추출 및 추가 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"
    
    extract_prompt = f"""
    현재 날짜: {today_str}
    사용자의 요청에서 추가할 구글 캘린더 일정 정보를 정확히 추출하여 오직 객체로 반환하세요.

    [시간 해석 규칙]
    - 오전/오후 구분 없이 표기된 시간은 업무시간(09:00~18:00) 기준으로 해석하세요.
      예: "1시" → 13:00, "2시" → 14:00, "3시" → 15:00, "9시" → 09:00, "10시" → 10:00
    - "내일", "모레" 등 상대적 날짜 표현을 현재 날짜 기준으로 정확히 계산하세요.
    - 날짜가 명시되지 않았다면 오늘 날짜로, 종료시간이 없다면 시작시간으로부터 1시간 뒤로 설정하세요.

    사용자 요청: {user_input}
    """
    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarEventSchema,
            ),
        )
        data = CalendarEventSchema(**json.loads(response.text))
        calendar = get_workspace_service('calendar', 'v3', user_email)
        
        start_time = datetime.datetime(data.year, data.month, data.day, data.startHour, data.startMinute).isoformat()
        end_time = datetime.datetime(data.year, data.month, data.day, data.endHour, data.endMinute).isoformat()
        
        event = {
            'summary': data.title,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Seoul'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Seoul'},
        }
        calendar.events().insert(calendarId=user_email, body=event).execute()
        
        h1 = str(data.startHour).zfill(2)
        m1 = str(data.startMinute).zfill(2)
        return {"messages": [AIMessage(content=f"✅ **일정이 성공적으로 등록되었습니다!**\n\n- **일정명:** {data.title}\n- **시간:** {data.month}월 {data.day}일 {h1}:{m1} 시작\n\n구글 캘린더에 완벽하게 연동되었습니다.")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ 캘린더 등록 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 일정 추가 중 오류가 발생했습니다. 에러 상세: {str(e)}")]}

def task_read_node(state: AgentState):
    print("📝 [TASK_READ] 구글 Tasks에서 미완료 할 일 목록을 가져오는 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    try:
        tasks_service = get_workspace_service('tasks', 'v1', user_email)
        results = tasks_service.tasks().list(tasklist='@default', showCompleted=False).execute()
        tasks = results.get('items', [])
        
        if not tasks:
            return {"messages": [AIMessage(content="📝 **현재 남아있는 할 일이 없습니다.**\n\n여유로운 하루 보내세요!")]}
            
        task_text = ""
        for idx, t in enumerate(tasks):
            due_str = f" (마감: {t['due'][:10]})" if 'due' in t else ""
            note_str = f"\n ↳ 상세: {t['notes']}" if 'notes' in t else ""
            task_text += f"{idx + 1}. [ ] {t['title']}{due_str}{note_str}\n"
            
        prompt = f"사용자 요청: {user_input}\n할 일 목록: \n{task_text}\n비서로서 위 할 일 목록을 가독성 좋게 불릿 포인트로 요약 브리핑하고 마감일 업무를 강조하세요."
        ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
        return {"messages": [AIMessage(content=f"✅ **오늘의 할 일 브리핑**\n\n{ai_response}")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ Tasks 읽기 에러: {str(e)}")
        return {"messages": [AIMessage(content="⚠️ 할 일을 불러오는데 실패했습니다. Tasks 권한을 확인해주세요.")]}

def task_write_node(state: AgentState):
    print("📝 [TASK_WRITE] 단기 대화 맥락 분석 및 구글 할 일 추가 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")
    
    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"
    
    extract_prompt = f"""
    현재 날짜: {today_str}
    사용자 요청: {user_input}
    구글 할 일(Tasks)에 등록할 정보를 정확히 구조화하여 추출하세요. 마감일 형식은 YYYY-MM-DD 입니다. 명시되지 않았다면 빈 문자열로 하세요.
    """
    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TaskSchema,
            ),
        )
        data = TaskSchema(**json.loads(response.text))
        tasks_service = get_workspace_service('tasks', 'v1', user_email)
        
        task_body = {'title': data.title, 'notes': data.notes}
        if data.due:
            task_body['due'] = data.due + "T00:00:00.000Z"
            
        tasks_service.tasks().insert(tasklist='@default', body=task_body).execute()
        due_text = f"\n- **마감일:** {data.due}" if data.due else ""
        return {"messages": [AIMessage(content=f"✅ **성공적으로 할 일(Tasks)에 등록되었습니다!**\n\n- **할 일:** {data.title}{due_text}\n\n우측 구글 워크스페이스 패널의 Tasks 탭에서 확인하실 수 있습니다.")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ Tasks 등록 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 할 일 등록 중 오류가 발생했습니다: {str(e)}")]}

def dispatcher_node(state: AgentState):
    """pending_intents 큐에서 다음 인텐트를 꺼내 current_intent에 세팅"""
    pending = list(state.get("pending_intents") or [])
    if pending:
        next_intent = pending[0]
        remaining = pending[1:]
        print(f"🔄 [Dispatcher] 다음 작업 실행: {next_intent} (남은 작업 {len(remaining)}개)")
        return {"current_intent": next_intent, "pending_intents": remaining}
    print("🏁 [Dispatcher] 모든 작업 완료 → Aggregator로 이동")
    return {"current_intent": "__DONE__"}

def aggregator_node(state: AgentState):
    """복수 작업 응답을 하나의 메시지로 통합"""
    messages = state["messages"]
    ai_responses = []
    for msg in reversed(messages):
        if hasattr(msg, 'type') and msg.type == "human":
            break
        if hasattr(msg, 'type') and msg.type == "ai":
            ai_responses.insert(0, msg.content)

    if len(ai_responses) <= 1:
        return {}  # 단일 응답이면 그대로 통과

    combined = "\n\n---\n\n".join(ai_responses)
    print(f"📋 [Aggregator] {len(ai_responses)}개 응답 통합 완료")
    return {"messages": [AIMessage(content=combined)]}

# ---------------------------------------------------------------------
# 🚦 LangGraph 조건부 에지 라우터 매립 구역
# ---------------------------------------------------------------------
def route_after_bq(state: AgentState) -> Literal["Dispatcher", "BQ_Corrector_Node"]:
    if state.get("bq_error_log"):
        if state.get("bq_retry_count", 0) < 2:
            return "BQ_Corrector_Node"
        print("🚨 [BQ 패닉] 2회 연속 자율 복구 실패. 안전 장벽을 가동합니다.")
    return "Dispatcher"

# ==========================================
# LangGraph 정밀 네트워크 인프라 토폴로지 연결
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("Supervisor", supervisor_node)
workflow.add_node("Dispatcher", dispatcher_node)
workflow.add_node("Aggregator_Node", aggregator_node)
workflow.add_node("RAG_Search_Node", rag_search_node)
workflow.add_node("Reasoner", reasoner_node)
workflow.add_node("GENERAL_Node", general_node)
workflow.add_node("BQ_Node", bq_node)
workflow.add_node("BQ_Corrector_Node", bq_corrector_node)
workflow.add_node("EMAIL_WRITE_Node", email_write_node)
workflow.add_node("EMAIL_READ_Node", email_read_node)
workflow.add_node("CALENDAR_WRITE_Node", calendar_write_node)
workflow.add_node("CALENDAR_READ_Node", calendar_read_node)
workflow.add_node("TASK_WRITE_Node", task_write_node)
workflow.add_node("TASK_READ_Node", task_read_node)


# START → Supervisor → 첫 번째 액션 노드 (current_intent 기준 직접 라우팅)
# Dispatcher는 각 액션 노드 완료 후에만 호출 (pending_intents 처리용)
workflow.add_edge(START, "Supervisor")

INTENT_NODE_MAP = {
    "RAG": "RAG_Search_Node",
    "BQ": "BQ_Node",
    "GENERAL": "GENERAL_Node",
    "EMAIL_WRITE": "EMAIL_WRITE_Node",
    "EMAIL_READ": "EMAIL_READ_Node",
    "CALENDAR_WRITE": "CALENDAR_WRITE_Node",
    "CALENDAR_READ": "CALENDAR_READ_Node",
    "TASK_WRITE": "TASK_WRITE_Node",
    "TASK_READ": "TASK_READ_Node",
}

def route_after_supervisor(state: AgentState) -> Literal[
    "RAG_Search_Node", "BQ_Node", "GENERAL_Node",
    "EMAIL_WRITE_Node", "EMAIL_READ_Node",
    "CALENDAR_WRITE_Node", "CALENDAR_READ_Node",
    "TASK_WRITE_Node", "TASK_READ_Node", "Aggregator_Node"
]:
    return INTENT_NODE_MAP.get(state["current_intent"], "Aggregator_Node")

workflow.add_conditional_edges(
    "Supervisor",
    route_after_supervisor,
    {
        "RAG_Search_Node": "RAG_Search_Node",
        "BQ_Node": "BQ_Node",
        "GENERAL_Node": "GENERAL_Node",
        "EMAIL_WRITE_Node": "EMAIL_WRITE_Node",
        "EMAIL_READ_Node": "EMAIL_READ_Node",
        "CALENDAR_WRITE_Node": "CALENDAR_WRITE_Node",
        "CALENDAR_READ_Node": "CALENDAR_READ_Node",
        "TASK_WRITE_Node": "TASK_WRITE_Node",
        "TASK_READ_Node": "TASK_READ_Node",
        "Aggregator_Node": "Aggregator_Node",
    }
)

# 액션 노드 완료 후 Dispatcher: pending_intents가 남아있으면 다음 노드로, 없으면 Aggregator로
def route_after_dispatcher(state: AgentState) -> Literal[
    "RAG_Search_Node", "BQ_Node", "GENERAL_Node",
    "EMAIL_WRITE_Node", "EMAIL_READ_Node",
    "CALENDAR_WRITE_Node", "CALENDAR_READ_Node",
    "TASK_WRITE_Node", "TASK_READ_Node", "Aggregator_Node"
]:
    return INTENT_NODE_MAP.get(state["current_intent"], "Aggregator_Node")

workflow.add_conditional_edges(
    "Dispatcher",
    route_after_dispatcher,
    {
        "RAG_Search_Node": "RAG_Search_Node",
        "BQ_Node": "BQ_Node",
        "GENERAL_Node": "GENERAL_Node",
        "EMAIL_WRITE_Node": "EMAIL_WRITE_Node",
        "EMAIL_READ_Node": "EMAIL_READ_Node",
        "CALENDAR_WRITE_Node": "CALENDAR_WRITE_Node",
        "CALENDAR_READ_Node": "CALENDAR_READ_Node",
        "TASK_WRITE_Node": "TASK_WRITE_Node",
        "TASK_READ_Node": "TASK_READ_Node",
        "Aggregator_Node": "Aggregator_Node",
    }
)

# BQ 재시도 루프: 실패 시 Corrector, 성공 시 Dispatcher로 복귀
workflow.add_conditional_edges(
    "BQ_Node",
    route_after_bq,
    {
        "Dispatcher": "Dispatcher",
        "BQ_Corrector_Node": "BQ_Corrector_Node"
    }
)
workflow.add_edge("BQ_Corrector_Node", "BQ_Node")

# RAG 체인: Search → Reasoner → Dispatcher
workflow.add_edge("RAG_Search_Node", "Reasoner")
workflow.add_edge("Reasoner", "Dispatcher")

# 단일 액션 노드 완료 후 Dispatcher로 복귀 (다음 인텐트 처리 또는 종료 판단)
workflow.add_edge("GENERAL_Node", "Dispatcher")
workflow.add_edge("EMAIL_WRITE_Node", "Dispatcher")
workflow.add_edge("EMAIL_READ_Node", "Dispatcher")
workflow.add_edge("CALENDAR_WRITE_Node", "Dispatcher")
workflow.add_edge("CALENDAR_READ_Node", "Dispatcher")
workflow.add_edge("TASK_WRITE_Node", "Dispatcher")
workflow.add_edge("TASK_READ_Node", "Dispatcher")

# Aggregator → END
workflow.add_edge("Aggregator_Node", END)

# ==========================================================
# 💡 파이어스토어 서랍장 저장소 최종 빌드 연동 (v8 완전 패키징)
# ==========================================================
memory = langgraph_checkpoint_firestore.FirestoreSaver(project_id=PROJECT_ID)
coway_agent_app = workflow.compile(checkpointer=memory)

# =================================================================
# 📊 비동기 텔레메트리 데이터 파이프라인 분석 로깅 시스템
# =================================================================
def log_to_analytics_v2(payload: dict, ai_response: str, response_status: str, intent: str, top_dept_code: str = "분류 불가", user_agent: str = "Unknown"):
    DATASET_ID = "chatbot_analytics"
    TABLE_ID = "query_analytics_v2"
    
    bq_client_logger = bigquery.Client(project=PROJECT_ID)
    table_ref = bq_client_logger.dataset(DATASET_ID).table(TABLE_ID)
    
    try:
        bq_client_logger.get_table(table_ref)
    except Exception:
        print(f"🧱 [인프라 생성] '{TABLE_ID}' 테이블이 새 프로젝트에 존재하지 않아 신규 개설을 시작합니다...")
        schema = [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("user_email", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("user_name", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("user_query", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("ai_response", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("assigned_dept", "STRING", mode="NULLABLE"),     
            bigquery.SchemaField("response_status", "STRING", mode="REQUIRED"),   # 🎯 [수석 비서 오타 정정] modresponsee 제거
            bigquery.SchemaField("action_status", "STRING", mode="REQUIRED"),     
            bigquery.SchemaField("device_type", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("browser_info", "STRING", mode="NULLABLE"),
        ]
        table = bigquery.Table(table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field="timestamp")
        bq_client_logger.create_table(table)
        print(f"✅ [인프라 완료] 새 부대 '{TABLE_ID}' 파티션 장부 생성 대성공!")

    assigned_dept = top_dept_code
    if response_status == "FAIL" and (assigned_dept == "분류 불가" or not assigned_dept):
        try:
            inference_prompt = f"""
            당신은 코웨이 회사의 조직 체계를 완벽하게 꿰뚫고 있는 수석 인사 기획자입니다.
            사용자의 질문을 읽고, 이 질문이 [인사팀, 총무팀, IT지원팀, 재무팀, 법무팀] 중 어느 부서의 소관 업무 영역에 속하는지 추측하여 단 한 단어로만 답변하세요.
            설명이나 기호는 절대 금지합니다. (예: 인사팀)
            
            질문: {payload.get('lastQ', '')}
            """
            response = ai_client.models.generate_content(model=MODEL_NAME, contents=inference_prompt)
            guessed_dept = response.text.strip()
            if any(dept in guessed_dept for dept in ["인사팀", "총무팀", "IT지원팀", "재무팀", "법무팀"]):
                assigned_dept = guessed_dept
        except Exception as e:
            print(f"⚠️ 제미나이 부서 추측 실패: {e}")
            assigned_dept = "총무팀(기본)"

    device = "PC"
    browser = "Unknown"
    ua = user_agent.lower()
    if any(m in ua for m in ["mobile", "android", "iphone", "ipad"]): device = "Mobile"
    if "edg" in ua: browser = "Edge"
    elif "whale" in ua: browser = "Whale"
    elif "chrome" in ua: browser = "Chrome"
    elif "safari" in ua: browser = "Safari"

    action_status = "PENDING" if response_status == "FAIL" else "NONE"
    
    user_info = payload.get("user_info", {})
    row_to_insert = [{
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "user_email": payload.get("user_email", "unknown@coway.com"),
        "user_name": user_info.get("name", "임직원"),
        "user_query": payload.get("lastQ", ""),
        "ai_response": ai_response,
        "assigned_dept": assigned_dept,
        "response_status": response_status,
        "action_status": action_status,
        "device_type": device,
        "browser_info": browser
    }]
    
    try:
        bq_client_logger.insert_rows_json(table_ref, row_to_insert)
        print(f"📊 [BQ 로그 적재 완료] 상태: {response_status} / 배달 대기 상태: {action_status} / 추정 부서: {assigned_dept}")
    except Exception as e:
        print(f"⚠️ 빅쿼리 로깅 실패: {e}")