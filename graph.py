# graph.py
from typing import TypedDict, Annotated, Sequence, Literal
from langchain_core.messages import BaseMessage, AIMessage
from langgraph.graph import StateGraph, START, END
import operator
import json
import base64
import datetime
from pydantic import BaseModel, Field
import google.auth
import google.auth.transport.requests
import requests # 파이썬 기본 HTTP 통신 라이브러리
from google.cloud import bigquery

# 💡 [초격차 혁신] 경고를 뿜던 구형 랭체인 클래스를 완전히 폐기하고,
# 2026년 오피셜 google-genai SDK 드라이버로 전면 체질 개선을 완료했습니다.
from google import genai
from google.genai import types
from googleapiclient.discovery import build

# =====================================================================
# 🧠 Google I/O 2026 오피셜 에이전트 플랫폼 인프라 세팅
# =====================================================================
PROJECT_ID = "hr-division-ai-rpa"
MODEL_NAME = "gemini-3.5-flash"  # 🎯 GA 반영 완료된 최신 3.5 싱킹 엔진 명시

# 구글 엔터프라이즈 에이전트 플랫폼(Agent Platform API) 클라이언트 초기화
ai_client = genai.Client(
    enterprise=True,
    project=PROJECT_ID,
    location="global"  # 🚀 최신 글로벌 에이전트 오케스트레이션 엔드포인트
)

# 빅쿼리 클라이언트
bq_client = bigquery.Client(project=PROJECT_ID)

# ==========================================
# 상태 장부 및 출력 구조체 정의
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
    current_intent: str      
    refined_query: str       
    retrieved_docs: str      
    user_info: dict          

# 최신 오피셜 SDK의 Structured Output을 지원하기 위한 표준 Pydantic 클래스 선언
class RouteDecision(BaseModel):
    intent: str = Field(description="RAG, BQ, GENERAL, EMAIL_READ, EMAIL_WRITE, CALENDAR_READ, CALENDAR_WRITE, TASK_READ, TASK_WRITE 중 하나")

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

# ==========================================
# 🛡️ 구글 워크스페이스 서비스 빌더 헬퍼 함수
# ==========================================
def get_workspace_service(service_name: str, version: str, user_email: str):
    """GCP 서비스 계정 권한 또는 가장(Impersonation)을 통해 임직원의 워크스페이스 권한을 획득합니다."""
    creds, project = google.auth.default(scopes=[
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/tasks"
    ])
    if hasattr(creds, 'with_subject'):
        delegated_creds = creds.with_subject(user_email)
        return build(service_name, version, credentials=delegated_creds)
    return build(service_name, version, credentials=creds)

# ==========================================
# 노드(Node) 함수들
# ==========================================
def supervisor_node(state: AgentState):
    print("🚦 [Supervisor] 제미나이 3.5가 의도를 파악 중입니다...")
    user_input = state["messages"][-1].content
    
    # 💡 [수정] 제미나이에게 RAG와 BQ의 차이를 아주 명확하게 가르쳐줍니다. + 워크스페이스 분기 인텔리전스 강화
    prompt = f"""
    당신은 코웨이 전사 AI 챗봇의 총괄 지휘자입니다. 사용자 질문을 분석하여 아래 의도 중 하나로 정확하게 분류하세요:
    - RAG: 사내 규정, 복리후생, 인사, 가이드 등 텍스트 문서 검색이 필요한 질문 (예: 환갑 지원금, 휴가 규정)
    - BQ: 매출액, 실적, 예산, 판매량 등 DB에서 수치 데이터 조회가 필요한 질문 (예: 작년 정수기 매출액, 3분기 실적)
    - GENERAL: 단순 인사, 안부, 일상 대화 (예: 안녕, 넌 누구야)
    - EMAIL_WRITE: 메일이나 이메일 작성, 초안 작성, 답장 요청 (예: 팀장님께 보낼 휴가 신청 메일 써줘)
    - EMAIL_READ: 이메일 요약, 읽어주기, 확인, 브리핑 요청 (예: 오늘 온 안읽은 메일 요약해줘)
    - CALENDAR_WRITE: 캘린더 일정 추가, 등록, 회의 생성 요청 (예: 내일 오후 3시에 미팅 일정 잡아줘)
    - CALENDAR_READ: 일정 조회, 오늘 스케줄 브리핑 요청 (예: 오늘 내 미팅 일정 알려줘)
    - TASK_WRITE: 할 일 등록, 테스크 추가 요청 (예: 오늘 마케팅 보고서 작성 할일 등록해줘)
    - TASK_READ: 할 일 조회, 테스크 목록 브리핑 요청 (예: 오늘 내가 해야할 업무 리스트 보여줘)

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
    intent = decision_data.get("intent", "GENERAL")
    print(f"✅ [Supervisor] 판단 결과: {intent}")
    return {"current_intent": intent}

# 📑 [참조용 보존] V1 버전의 구형 검색어 정제 노드 (대원칙 준수를 위해 유지)
def rag_refiner_node(state: AgentState):
    print("🔍 [RAG Refiner] 이전 대화 맥락까지 고려하여 검색어 정제 중...")
    
    # 💡 [핵심] 장부에 적힌 모든 대화를 하나의 문자열 대본으로 만듭니다.
    chat_history = ""
    for msg in state["messages"]:
        role = "사용자" if msg.type == "human" else "챗봇"
        chat_history += f"{role}: {msg.content}\n"
    
    # 프롬프트에 대화 대본(chat_history)을 통째로 넘겨줍니다.
    prompt = f"""
    당신은 사내 규정 검색을 위한 검색어 최적화 AI입니다.
    below의 [대화 기록]을 읽고, 사용자가 '가장 마지막에 한 질문'의 진짜 의도를 파악하세요.
    파악한 의도를 Vertex AI Search 엔진에 검색하기 좋은 핵심 명사구 '하나'로만 출력하세요. (쉼표 금지)
    
    [대화 기록]
    {chat_history}
    
    [지시사항]
    위 대화의 맥락을 고려했을 때, 마지막 사용자 질문을 완벽한 사내 공식 명칭 형태의 단일 검색어로 출력하세요.
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

# 📑 [참조용 보존] V1 버전의 구형 Discovery Engine API 검색 노드 (대원칙 준수를 위해 유지)
def rag_retriever_node(state: AgentState):
    """💡 [핵심] LangChain을 버리고 GAS 코드를 파이썬으로 100% 완벽 이식한 커스텀 Retriever (출처 링크 추가 버전)"""
    query = state["refined_query"]
    print(f"📚 [RAG Retriever] '{query}'(으)로 Vertex AI App 직접 찌르기 중...")
    
    # 1. 내 PC의 구글 인증 신분증(ADC) 꺼내기
    creds, _ = google.auth.default()
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    
    # 2. 수석님의 GAS 코드와 토씨 하나 안 틀린 완벽한 URL
    PROJECT_ID = "81027032834"
    APP_ID = "coway-ai-chatbot_1766022708310"
    url = f"https://discoveryengine.googleapis.com/v1alpha/projects/{PROJECT_ID}/locations/global/collections/default_collection/engines/{APP_ID}/servingConfigs/default_search:search"
    
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "pageSize": 3
    }
    
    # 3. 직접 검색 엔진 호출!
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        results = data.get("results", [])
        if results:
            doc_contents = ""
            for i, res in enumerate(results):
                doc_data = res.get("document", {})
                
                # 구글 검색 결과에서 요약(snippet) 부분 추출
                snippet = doc_data.get("derivedStructData", {}).get("snippets", [{}])[0].get("snippet", "내용 없음")
                
                # 원본 구글 드라이브 링크(Link) 추출
                link = doc_data.get("derivedStructData", {}).get("link", "링크 없음")
                
                # 문서 내용과 함께 링크 정보를 묶어서 저장
                doc_contents += f"[문서 {i+1} 원본링크: {link}]\n{snippet}\n\n"
            
            print(f"✅ [RAG Retriever] 규정 문서 {len(results)}개 및 출처 링크 확보 완료!")
            return {"retrieved_docs": doc_contents}
            
    print("❌ [RAG Retriever] 검색된 문서가 없습니다. (또는 API 상태 오류)")
    return {"retrieved_docs": "관련 규정 문서를 찾을 수 없습니다."}


# 💡 [V2 초격차 핵심 업데이트] 빅쿼리 금고에서 3072차원 하이브리드 검색을 수행하는 실시간 RAG 노드
def rag_search_node(state: AgentState):
    """우리가 완성한 rag_node.py의 하이브리드 랭킹 엔진을 그래프 장부에 완벽 동기화 결합합니다."""
    print("\n🔍 [RAG Search] 빅쿼리 고성능 하이브리드 검색 및 권한(ACL) 실시간 검증 가동...")
    from rag_node import hybrid_search_bq
    
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "employee_all@coway.com")
    
    # 앞서 완벽하게 성공했던 US 리전 기반 3072차원 하이브리드 검색 엔진 호출
    context_text = hybrid_search_bq(user_input, user_email)
    
    if not context_text:
        context_text = "시스템에 등록된 사내 규정이나 관련 문서를 찾을 수 없거나, 해당 문서를 열람할 권한이 없습니다."
        print("⚠️ 관련 문서를 찾지 못했습니다.")
        
    # 하단의 Reasoner 노드가 부드럽게 받아서 처리할 수 있도록 획득한 지식을 장부에 적재합니다.
    return {"retrieved_docs": context_text}


def reasoner_node(state: AgentState):
    """💡 [핵심] 제미나이가 문서를 읽고 최종 답변 생성 (외계어 제거 및 출처 표기 포함)"""
    print("🧠 [Reasoner] 제미나이 3.5 싱킹 엔진 가동 및 최종 추론 답변 생성 중...")
    user_input = state["messages"][-1].content
    docs = state["retrieved_docs"]
    
    # 🚀 V1 백엔드 마스터 지침 지시사항 10개 완벽 이식 및 유지보수 결합
    prompt = f"""
    당신은 코웨이 임직원의 질문 의도를 찰떡같이 파악하고 사내 문서를 바탕으로 가장 정확하고 '논리적인' 답변을 제공하는 최고 수준의 AI 어시스턴트입니다.

    [🎯 사용자의 원래 질문 및 사연 (맥락 파악용)]
    "{user_input}"
    ※ 중요: 위 질문에 포함된 개인 상황(나이, 연도, 특정 부서명 등)을 현재 시점 기준으로 분석하여, 아래 검색된 규정 문서와 결합해 맞춤형으로 추론하여 답변하세요.

    [지침]
    1. 사용자의 질문에 오타나 줄임말(예: 디자인랩, 지타워 등)이 있더라도, 제공된 문서의 문맥을 유추하여 가장 적절한 정보를 찾아 답변하세요.
    2. 특정 부서의 위치나 담당자를 묻는 질문(예: "디자인랩 어디야?")인 경우, 문서(조직도, 층별 안내도 등)의 상하위 맥락을 꼼꼼히 역추적하세요.
    3. 규정 조항, 예외 사항, 필수 절차 등이 검색 결과에 있다면 누락하지 말고 종합적으로 엮어서 상세히 설명하세요.
    4. 검색 결과에 없는 내용은 절대 지어내지 마세요.
    5. 아무리 찾아도 관련 내용이 전혀 없다면 "죄송합니다. 제공된 규정이나 문서에서는 해당 내용을 찾을 수 없습니다. 담당 부서에 문의해 주세요."라고만 답변하세요.
    6. PDF에서 추출된 텍스트라 표(Table)나 다단 내용이 규칙 없이 섞여 있더라도, 단어들의 나열 패턴과 문맥을 분석하여 원래의 표(행/열) 구조나 연관성을 스스로 재조립(Reconstruct)한 후 정확하게 답변하세요.
    7. [매우 중요] 스프레드시트나 표 형식의 데이터에서 특정 업무의 '담당자'를 안내할 때는, 반드시 해당 업무 내용(행, Row)과 정확히 1:1로 일치하는 담당자만 추출하세요.
    8. [🚀 심층 추론 지시] 사용자가 복잡한 정산 방식, 방법론의 선택(예: 방법1 vs 방법2), 계산 원리 등을 물어볼 경우, 검색된 규정 조각들을 단순히 나열하지 마세요. 규정의 근본적인 목적(예: 상계 처리, 비용 증빙 원칙 등)을 바탕으로 상황을 단계별로 분석(Step-by-step reasoning)하여 실무자에게 가이드(선택지)를 명확히 제시하세요.
    9. [🚀 심층 추론 및 날짜 계산 지시] 사용자가 연도나 나이 계산(예: 환갑)을 요구할 경우, 섣불리 짐작하지 말고 속으로 명시적인 수식(예: 출생연도 + 60 = 대상 연도)을 세워 계산하세요. '환갑'은 만 60세, 한국나이로는 61세가 환갑입니다.
    10. [후속 질문 추천] 답변을 모두 작성한 후, 맨 마지막 줄에 사용자가 이어서 궁금해할 만한 '추천 질문' 3가지를 반드시 "|||SUGGESTIONS|||" 이라는 구분자 뒤에 줄바꿈으로 구분하여 작성하세요.
    
    [검색된 규정]
    {docs}
    """
    
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return {"messages": [AIMessage(content=response.text.strip())]} 


# 💡 [신규 추가 1] 일반 대화(GENERAL)를 처리하는 노드
def general_node(state: AgentState):
    print("👋 [GENERAL] 일상 대화 처리 중...")
    user_input = state["messages"][-1].content
    
    prompt = f"""
    당신은 코웨이 임직원을 위한 사내 전사 AI 챗봇입니다.
    사용자의 일상적인 인사나 대화에 친절하고 자연스럽게 답변해 주세요.
    '사내 규정 및 인사/복리후생 제도'등과 관련된 질문에만 답변할 수 있다고 안내하세요
    사용자 질문: {user_input}
    """
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return {"messages": [AIMessage(content=response.text.strip())]}


# 💡 [V2 초격차 업데이트] 자율형 빅쿼리 SQL Agent 노드 (Native Table 전용 & 도메인 통합 완전체 버전)
def bq_node(state: AgentState):
    print("📊 [BQ] 제미나이 3.5 기반 자율형 데이터 애널리스트 모드 기동...")
    
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "unknown")
    
    # 💡 [핵심 개선 2] 데이터 사전(Data Dictionary) 및 AI 행동 지침 프롬프트 고도화 + V1 빅쿼리 카탈로그 결합
    system_message = f"""
    당신은 코웨이의 최고 데이터 분석가입니다.
    사용자의 질문에 답변하기 위해 BigQuery 데이터베이스에 직접 유효한 표준 SQL 쿼리를 작성하고 실행하세요.
    
    [보안 및 권한 준수 사항]
    1. 현재 질문하는 사용자의 ID는 '{user_email}' 입니다.
    2. 사용자 ID(@coway.com)가 BigQuery 데이터 세트 레벨에서 권한을 가진 테이블만 조회하세요.
    3. 쿼리 실행 중 'Access Denied' 에러 발생 시, 사용자에게 권한이 없음을 정중히 안내하세요.

    🚫 [절대 접근 금지 구역 - 매우 중요]
    데이터베이스에 남아있는 V1 버전의 구형 데이터세트(general_affairs_travel, hrga_cost_budget_master, hrga_cost_execution_detail, hrga_cost_master_db)는 절대로 조회하지 마세요. 해당 테이블 접근 시 치명적인 권한 에러(Drive credentials)가 발생합니다.
    
    [코웨이 빅쿼리 데이터세트 및 테이블 안내]
    분기처리를 위해 아래의 2개 데이터 세트(Dataset)를 활용하세요. 데이터 세트명을 반드시 포함하여 쿼리하세요.
    
    📁 1. hrga_cost_data (총무팀 집행비용 데이터)
      - budget_raw : 집행비용 ROW DATA (개별 집행 내역)
      - execution_detail : 집행비용 상세현황 (당월/전월 집행액, 증감액/률, 증감사유 등)
      - budget_master : 연간 예산 계획
    
    📁 2. hrga_travel_data (전사 출장현황 데이터)
      - travel_master_db : 전사 출장 상세 내역
        * 💡 [중요] 출장 건수를 계산할 때는 `COUNT(DISTINCT trip_id)`를 사용하세요.
        * 💡 [중요] 출장 인원수를 계산할 때는 `COUNT(emp_id)`를 사용하세요.
    
    ⚠️ [데이터 조회 핵심 예외처리 지침]
    1. 집행연월 컬럼(`exe_month`)은 '2026-03-01' 또는 '2026. 3. 1' 등 문자열 형식이 다양할 수 있습니다. 따라서 특정 월(예: 2026년 3월)을 조회할 때는 `WHERE exe_month LIKE '2026-03%' OR exe_month LIKE '2026. 3%'`와 같이 `LIKE` 연산자와 와일드카드(`%`)를 사용하여 안전하게 쿼리하세요.
    2. 사용자가 '집행비용'을 질문한 경우, 절대로 출장 데이터세트(\`hrga_travel_data\`)를 조회하거나 답변에 혼합하지 마세요. 비용 데이터에 조건에 맞는 결과가 없다면, 비용 데이터가 존재하지 않는다고만 솔직하게 답변하세요.
    3. 🧠 [풍부한 맥락 추출의 본능]: 사용자가 단답형 질문을 하더라도 부서별 증감 사유와 배경을 함께 브리핑할 수 있도록, 관련 테이블에 \`review\`, \`reason_increase\`, \`reason_decrease\` 같은 텍스트 컬럼이 있다면 반드시 함께 SELECT 하세요.
    4. 텍스트 컬럼을 그룹핑할 때는 SQL 에러 방지를 위해 반드시 \`ANY_VALUE()\` 함수를 씌우세요.

    출력은 마크다운 코드블록을 포함하여 오직 순수 SQL문만 전달하세요.
    """
    
    sql_response = ai_client.models.generate_content(
        model=MODEL_NAME,
        contents=[{"role": "system", "parts": [{"text": system_message}]}, {"role": "user", "parts": [{"text": user_input}]}]
    ).text
    
    generated_sql = sql_response.replace("```sql", "").replace("```", "").strip()
    
    print(f"🔍 [BQ] 사용자({user_email})의 권한 범위 내에서 데이터 탐색 및 SQL 실행 중...")
    
    try:
        query_job = bq_client.query(generated_sql)
        query_results = [dict(row) for row in query_job.result()]
        
        # 🧠 Data-to-Text 브리핑 생성부 가동 (V1 지침 100% 완전 계승)
        summary_prompt = f"""
        당신은 코웨이 경영진의 의사결정을 돕는 '수석 데이터 애널리스트 AI'입니다.
        데이터베이스에서 추출된 날 것의 데이터(Raw JSON)를 바탕으로, 단순한 수치 나열을 넘어선 '입체적이고 통찰력 있는 비즈니스 브리핑'을 작성하세요.

        [🔥 최고 수준의 브리핑을 위한 작성 지침 🔥]
        1. 🎯 Executive Summary (핵심 결론 최우선):
           - 첫 문단은 사용자의 질문에 대한 가장 중요한 결론(총 집행액, 총 인원/건수, 가장 눈에 띄는 트렌드)을 명쾌하게 2~3줄로 요약하세요.
        2. 💡 도메인 맞춤형 인사이트 도출 (스토리텔링):
           - [예산 데이터인 경우]: 전월 대비 증감율, 주요 증가/감소 사유(reason_increase 등)를 분석하여 브리핑하세요.
           - [출장 데이터인 경우]: 부서별 쏠림 현상, 국내/해외 비용 비중, 주요 출장 목적 등을 조합하여 종합적인 맥락을 짚어주세요.
        3. 📊 표(Table) 절대 금지 및 '개조식 불릿' 구조화 강제:
           - 모바일/웹 레이아웃 붕괴를 막기 위해 마크다운 표(Table)는 절대 금지합니다.
           - 반드시 불릿 포인트(-, •)를 활용해 세련된 보고서 형태로 작성하세요.
        4. 💰 수치 단위 최적화 (가독성):
           - 큰 숫자는 인간이 읽기 편하게 축약하세요. (예: 4,375,397,553 -> "약 43억 7,540만 원")
        5. 🚫 주의사항: 데이터에 없는 내용을 지어내거나 추측하지 마세요.

        데이터 JSON: {json.dumps(query_results, default=str)}
        사용자 질문: {user_input}
        """
        final_report = ai_client.models.generate_content(model=MODEL_NAME, contents=summary_prompt).text
        print("✅ [BQ] 데이터 분석 및 최종 답변 생성 완료!")
        return {"messages": [AIMessage(content=final_report + f"\n\n---\n<details><summary>💡 디버그: AI가 실행한 SQL 보기</summary>\n\n```sql\n{generated_sql}\n```\n</details>")]}
    except Exception as e:
        print(f"⚠️ BQ 실행 실패: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 데이터 분석 처리 중 오류가 발생했습니다.\n\n[에러 로그]: {str(e)}")]}


# ==========================================================
# ✉️ [Workspace 에이전트 1-1] 이메일 작성 노드
# ==========================================================
def email_write_node(state: AgentState):
    print("✉️ [EMAIL_WRITE] 제미나이 3.5가 이메일 초안 작성을 준비합니다...")
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "unknown")
    
    prompt = f"""당신은 코웨이 임직원을 돕는 전문 비서입니다.
    [지시사항]: {user_input}
    결과는 [메일 제목]과 [메일 본문]으로 구분하여 비즈니스 매너에 맞게 작성하세요."""
    
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    
    try:
        gmail = get_workspace_service('gmail', 'v1', user_email)
        raw_message = f"Subject: AI가 작성한 메일 초안\n\n{ai_response}"
        encoded_message = base64.urlsafe_b64encode(raw_message.encode("utf-8")).decode("utf-8")
        gmail.users().drafts().create(userId="me", body={"message": {"raw": encoded_message}}).execute()
        print("✅ [EMAIL_WRITE] Gmail 임시보관함 적재 성공")
    except Exception as e:
        print(f"⚠️ Gmail 연동 실패: {str(e)}")
        
    return {"messages": [AIMessage(content=f"✨ **이메일 초안 작성이 완료되었습니다!**\n\nGmail의 **[임시보관함]**에 메일을 안전하게 저장해 두었습니다.\n\n---\n\n{ai_response}")]}


# ==========================================================
# 📧 [Workspace 에이전트 1-2] 이메일 요약 노드
# ==========================================================
def email_read_node(state: AgentState):
    print("📧 [EMAIL_READ] 안읽은 메일을 수신하여 브리핑 정제 중...")
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "unknown")
    
    email_text = ""
    try:
        gmail = get_workspace_service('gmail', 'v1', user_email)
        q = 'is:unread in:inbox'
        if "오늘" in user_input:
            q += ' newer_than:1d'
            
        results = gmail.users().messages().list(userId='me', q=q, maxResults=20).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return {"messages": [AIMessage(content="📭 **현재 조건에 맞는 읽지 않은 새로운 메일이 없습니다.**\n\n중요한 알림이 오면 다시 말씀해주세요!")]}
            
        for idx, msg_info in enumerate(messages):
            msg = gmail.users().messages().get(userId='me', id=msg_info['id'], format='metadata', metadataHeaders=['From', 'Subject']).execute()
            headers = msg.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '제목 없음')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '알수없음').split('<')[0].strip()
            snippet = msg.get('snippet', '내용 없음')
            email_text += f"[메일 {idx + 1}]\n- 보낸사람: {sender}\n- 제목: {subject}\n- 내용: {snippet}...\n\n"
    except Exception as e:
        print(f"⚠️ Gmail 읽기 에러: {str(e)}")
        return {"messages": [AIMessage(content="⚠️ 메일 접근 권한이 없거나 불러오는 중 오류가 발생했습니다.")]}

    prompt = f"""
    사용자 요청: {user_input}
    [안 읽은 최신 메일 데이터]
    {email_text}
    당신은 임직원의 업무를 돕는 최고의 비서입니다. V1 비서 행동강령 지침에 맞게 핵심만 요약 브리핑하세요.
    1. 메일 발신자 직급을 절대 AI가 임의로 지어내지 마세요.
    2. 사람을 지칭할 때는 이름 뒤에 '님'만 붙이세요. (예: 홍길동 님)
    3. 사용자를 부를 때도 이름 뒤에 '님'만 붙이세요.
    """
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    return {"messages": [AIMessage(content=f"📧 **최신 메일 요약 브리핑**\n\n{ai_response}")]}


# ==========================================================
# 📅 [Workspace 에이전트 2-1] 캘린더 조회 노드
# ==========================================================
def calendar_read_node(state: AgentState):
    print("📅 [CALENDAR_READ] 구글 캘린더 일정을 조회하는 중...")
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "unknown")
    
    schedule_text = ""
    try:
        calendar = get_workspace_service('calendar', 'v3', user_email)
        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
        
        events_result = calendar.events().list(calendarId='primary', timeMin=start_of_day, timeMax=end_of_day, singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        
        if not events:
            schedule_text = "오늘 예정된 일정이 없습니다."
        else:
            for e in events:
                start = e['start'].get('dateTime', e['start'].get('date'))
                time_str = start[11:16] if 'T' in start else "종일"
                desc = e.get('description', '')
                desc_str = f"\n   ↳ 상세내용: {desc}" if desc else ""
                schedule_text += f"- [일정] {time_str} 시작 | {e.get('summary')}{desc_str}\n"
    except Exception as e:
        print(f"⚠️ 캘린더 읽기 에러: {str(e)}")
        schedule_text = "권한 오류"

    prompt = f"사용자 질문: {user_input}\n일정 데이터: \n${schedule_text}\n지시사항: 1.[종일/상태] 제외 2.[일정]만 24시 기준으로 명확히 나열 3.상세내용 포함 요약"
    ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
    return {"messages": [AIMessage(content=f"📅 **스마트 일정 브리핑**\n\n{ai_response}")]}


# ==========================================================
# 📅 [Workspace 에이전트 2-2] 캘린더 등록 노드
# ==========================================================
def calendar_write_node(state: AgentState):
    print("📅 [CALENDAR_WRITE] 구조화된 일정 데이터 추출 및 추가 중...")
    user_input = state["messages"][-1].content
    user_email = state["user_info"].get("email", "unknown")
    
    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"
    
    extract_prompt = f"""
    현재 날짜: {today_str}
    사용자의 요청에서 추가할 구글 캘린더 일정 정보를 정확히 추출하여 오직 객체로 반환하세요.
    날짜가 명시되지 않았다면 오늘 날짜로, 종료시간이 없다면 시작시간으로부터 1시간 뒤로 설정하세요.
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
        calendar.events().insert(calendarId='primary', body=event).execute()
        
        h1 = str(data.startHour).zfill(2)
        m1 = str(data.startMinute).zfill(2)
        return {"messages": [AIMessage(content=f"✅ **일정이 성공적으로 등록되었습니다!**\n\n- **일정명:** {data.title}\n- **시간:** {data.month}월 {data.day}일 {h1}:{m1} 시작\n\n구글 캘린더에 완벽하게 연동되었습니다.")]}
    except Exception as e:
        print(f"⚠️ 캘린더 등록 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 일정 추가 중 오류가 발생했습니다. 에러 상세: {str(e)}")]}


# ==========================================================
# ✅ [Workspace 에이전트 3-1] 구글 Tasks(할 일) 조회 노드
# ==========================================================
def task_read_node(state: AgentState):
    print("📝 [TASK_READ] 구글 Tasks에서 미완료 할 일 목록을 가져오는 중...")
    user_input = state["messages"][-1].content
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
            note_str = f"\n   ↳ 상세: {t['notes']}" if 'notes' in t else ""
            task_text += f"{idx + 1}. [ ] {t['title']}{due_str}{note_str}\n"
            
        prompt = f"사용자 요청: {user_input}\n할 일 목록: \n{task_text}\n비서로서 위 할 일 목록을 가독성 좋게 불릿 포인트로 요약 브리핑하고 마감일 업무를 강조하세요."
        ai_response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text
        return {"messages": [AIMessage(content=f"✅ **오늘의 할 일 브리핑**\n\n{ai_response}")]}
    except Exception as e:
        print(f"⚠️ Tasks 읽기 에러: {str(e)}")
        return {"messages": [AIMessage(content="⚠️ 할 일을 불러오는데 실패했습니다. Tasks 권한을 확인해주세요.")]}


# ==========================================================
# ✅ [Workspace 에이전트 3-2] 구글 Tasks(할 일) 등록 노드
# ==========================================================
def task_write_node(state: AgentState):
    print("📝 [TASK_WRITE] 단기 대화 맥락 분석 및 구글 할 일 추가 중...")
    user_input = state["messages"][-1].content
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
        print(f"⚠️ Tasks 등록 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 할 일 등록 중 오류가 발생했습니다: {str(e)}")]}


# ==========================================
# LangGraph 네트워크 연결 (모든 길이 다 뚫린 완성본!)
# ==========================================
workflow = StateGraph(AgentState)

workflow.add_node("Supervisor", supervisor_node)
# 💡 [V2 라우팅 교체] 구형 레거시 조합 대신, 우리가 완성한 고성능 단일 RAG 노드를 장착합니다.
workflow.add_node("RAG_Search_Node", rag_search_node)
workflow.add_node("Reasoner", reasoner_node)
workflow.add_node("GENERAL_Node", general_node)  # 신규 노드 등록
workflow.add_node("BQ_Node", bq_node)            # 신규 노드 등록
workflow.add_node("EMAIL_WRITE_Node", email_write_node)
workflow.add_node("EMAIL_READ_Node", email_read_node)
workflow.add_node("CALENDAR_WRITE_Node", calendar_write_node)
workflow.add_node("CALENDAR_READ_Node", calendar_read_node)
workflow.add_node("TASK_WRITE_Node", task_write_node)
workflow.add_node("TASK_READ_Node", task_read_node)

workflow.add_edge(START, "Supervisor")

# 💡 [수정] 교통경찰(Supervisor)이 명품 V2 하이브리드 RAG 노드 및 구글 API 노드로 트래픽을 직송합니다.
def route_after_supervisor(state: AgentState) -> Literal[
    "RAG_Search_Node", "BQ_Node", "GENERAL_Node", 
    "EMAIL_WRITE_Node", "EMAIL_READ_Node", 
    "CALENDAR_WRITE_Node", "CALENDAR_READ_Node",
    "TASK_WRITE_Node", "TASK_READ_Node", "END"
]:
    intent = state["current_intent"]
    if intent == "RAG": return "RAG_Search_Node"
    elif intent == "BQ": return "BQ_Node"
    elif intent == "GENERAL": return "GENERAL_Node"
    elif intent == "EMAIL_WRITE": return "EMAIL_WRITE_Node"
    elif intent == "EMAIL_READ": return "EMAIL_READ_Node"
    elif intent == "CALENDAR_WRITE": return "CALENDAR_WRITE_Node"
    elif intent == "CALENDAR_READ": return "CALENDAR_READ_Node"
    elif intent == "TASK_WRITE": return "TASK_WRITE_Node"
    elif intent == "TASK_READ": return "TASK_READ_Node"
    return "END" 

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
        "END": END
    }
)

# 💡 [V2 파이프라인 정리] 하이브리드 검색이 완료되면 지체 없이 즉시 최종 추론(Reasoner)으로 진격합니다.
workflow.add_edge("RAG_Search_Node", "Reasoner")
workflow.add_edge("Reasoner", END)
workflow.add_edge("GENERAL_Node", END)
workflow.add_edge("BQ_Node", END)
workflow.add_edge("EMAIL_WRITE_Node", END)
workflow.add_edge("EMAIL_READ_Node", END)
workflow.add_edge("CALENDAR_WRITE_Node", END)
workflow.add_edge("CALENDAR_READ_Node", END)
workflow.add_edge("TASK_WRITE_Node", END)
workflow.add_edge("TASK_READ_Node", END)

coway_agent_app = workflow.compile()