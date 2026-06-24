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
MODEL_NAME = "gemini-3.5-flash"       # RAG Reasoner + BQ 리포트 전용 (품질 최우선)
LITE_MODEL = "gemini-3.1-flash-lite"  # 라우팅·추출·요약·워크스페이스 전용 (~83% 비용 절감)

# 구글 엔터프라이즈 에이전트 플랫폼 클라이언트 초기화
ai_client = genai.Client(
    enterprise=True,
    project=PROJECT_ID,
    location="global"  # 🚀 최신 글로벌 에이전트 오케스트레이션 엔드포인트
)

# 빅쿼리 클라이언트
bq_client = bigquery.Client(project=PROJECT_ID)

# BQ 접근 권한 체크 대상 데이터셋 (공유 권한 설정된 데이터셋들)
_BQ_PROTECTED_DATASETS = ["hrga_travel_data", "hrga_cost_data"]

def check_bq_access(user_email: str) -> bool:
    """
    GCP BQ 데이터셋의 공유(ACL) 설정을 직접 읽어 요청 사용자의 접근 권한을 확인.
    서비스 계정은 ACL 조회 권한만 사용하며, 실제 BQ 쿼리 실행 전에 호출됨.
    GCP 콘솔의 데이터셋 공유 권한이 단일 소스 오브 트루스로 작동함.
    """
    try:
        for dataset_id in _BQ_PROTECTED_DATASETS:
            dataset = bq_client.get_dataset(f"{PROJECT_ID}.{dataset_id}")
            for entry in dataset.access_entries:
                if entry.entity_type == "userByEmail" and entry.entity_id == user_email:
                    print(f"✅ [BQ ACL] {user_email} → {dataset_id} 접근 허용 (role: {entry.role})")
                    return True
        print(f"🚫 [BQ ACL] {user_email} → 보호된 데이터셋에 권한 없음")
        return False
    except Exception as e:
        print(f"⚠️ [BQ ACL] 권한 체크 중 오류 — 안전을 위해 거부: {e}")
        return False


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
    bq_error_log: str   # BQ SQL 에러 추적 (SQL 교정용)
    bq_retry_count: int # BQ 재시도 카운터 (무한루프 방지)
    last_failed_intent: str       # 마지막 실패 인텐트 ("BQ", "RAG" 등)
    last_error_type: str          # 실패 유형: "PERMISSION" | "NOT_FOUND" | "SQL_ERROR" | ""
    fallback_suggested: bool      # 폴백 제안 중복 방지 플래그
    awaiting_fallback_query: str  # 폴백 확인 대기 중인 원본 쿼리
    calendar_free_suggestions: dict   # CALENDAR_FREE 추천 슬롯 {slots, original_request, attendee_names}
    calendar_selected_slot: dict      # 사용자 선택 슬롯 → CALENDAR_WRITE 직행 경로

# Structured Output 지원을 위한 표준 Pydantic 클래스 선언
class RouteDecision(BaseModel):
    intents: List[str] = Field(description="수행할 작업 목록. 각 항목은 RAG, BQ, GENERAL, EMAIL_READ, EMAIL_WRITE, EMAIL_SEND, EMAIL_SEARCH, EMAIL_REPLY, CALENDAR_READ, CALENDAR_WRITE, CALENDAR_RSVP, CALENDAR_UPDATE, CALENDAR_DELETE, CALENDAR_FREE, TASK_READ, TASK_WRITE, TASK_ACTION, DRIVE_SEARCH, DRIVE_LIST, PEOPLE_SEARCH, SHEET_READ, SHEET_WRITE, DOCS_CREATE 중 하나. 복수 작업 요청 시 순서대로 모두 포함.")
    confidence: float = Field(description="분류 확신도 0.0~1.0. 명확한 요청은 0.9 이상, 애매한 요청은 0.5 미만.")
    source_tier: str = Field(description="데이터 소스 계층: 'official'(BQ/RAG 공식 데이터), 'personal'(Drive/Sheets/Docs 개인 파일), 'action'(메일/캘린더/할일 등 액션), 'general'(일반 대화)")

class RefinedQuery(BaseModel):
    query: str = Field(description="명사 위주의 핵심 키워드 조합")

class CalendarReadSchema(BaseModel):
    startYear: int
    startMonth: int
    startDay: int
    endYear: int
    endMonth: int
    endDay: int

class CalendarEventSchema(BaseModel):
    year: int
    month: int
    day: int
    endYear: int   # 종료 날짜 (단일 날짜면 시작과 동일)
    endMonth: int
    endDay: int
    isAllDay: bool  # True: 종일 이벤트(휴가·연차·공휴일·재택 등). isHalfDay와 동시에 True 불가
    isHalfDay: bool  # True: 반차(오전/오후 4시간). isAllDay와 동시에 True 불가
    halfDayPeriod: str  # isHalfDay=True일 때만 유효. "morning"(오전반차 09-13) 또는 "afternoon"(오후반차 14-18)
    title: str
    startHour: int   # isAllDay=True 또는 isHalfDay=True면 0
    startMinute: int
    endHour: int
    endMinute: int
    attendees: str   # 초대할 참석자 (이름 또는 이메일, 콤마 구분, 없으면 빈 문자열)
    isOnline: bool   # True: 구글 미트 링크 자동 생성 (온라인 미팅/화상회의/비대면 요청 시)

class TaskSchema(BaseModel):
    title: str
    notes: str
    due: str

class EmailComposeSchema(BaseModel):
    to: str           # 수신자 이메일, 복수면 콤마 구분
    cc: str           # 참조 이메일, 없으면 빈 문자열
    subject: str      # 메일 제목
    body: str         # 메일 본문 (HTML 아닌 순수 텍스트)
    send_now: bool    # True: 즉시 발송, False: 임시보관함 저장

class EmailSearchSchema(BaseModel):
    gmail_query: str  # Gmail 검색 쿼리 (from:user@co.kr, subject:보고서 등)
    max_results: int  # 최대 조회 건수 (1~20)

class EmailReplySchema(BaseModel):
    search_query: str  # 회신할 메일 찾기용 검색어 (제목 or 발신자 이름/이메일)
    reply_body: str    # 회신 내용
    reply_all: bool    # True: 전체 회신, False: 보낸사람에게만

class CalendarDeleteSchema(BaseModel):
    search_query: str       # 삭제할 이벤트 검색어 (제목 일부 or 날짜 설명)
    target_year: int        # 이벤트 날짜 (0이면 미지정)
    target_month: int
    target_day: int

class CalendarUpdateSchema(BaseModel):
    search_query: str       # 수정할 이벤트 검색어
    target_year: int        # 검색 기준 날짜 (0이면 미지정, 이번 주 내 탐색)
    target_month: int
    target_day: int
    new_title: str          # 새 제목 (변경 없으면 빈 문자열)
    new_year: int           # 새 날짜 (0이면 변경 없음)
    new_month: int
    new_day: int
    new_start_hour: int     # 새 시작 시 (-1이면 변경 없음)
    new_start_minute: int
    new_end_hour: int
    new_end_minute: int
    attendees_add: str      # 추가할 참석자 이메일 (콤마 구분, 없으면 빈 문자열)

class CalendarFreeBusySchema(BaseModel):
    startYear: int
    startMonth: int
    startDay: int
    endYear: int
    endMonth: int
    endDay: int

class TaskActionSchema(BaseModel):
    search_title: str   # 대상 할일 검색어 (부분 매치)
    action: str         # "complete" | "delete" | "update"
    new_title: str      # action=update 시 새 제목 (변경 없으면 빈 문자열)
    new_due: str        # action=update 시 새 마감일 YYYY-MM-DD (없으면 빈 문자열)
    new_notes: str      # action=update 시 새 메모 (없으면 빈 문자열)

class DriveSearchSchema(BaseModel):
    query: str          # 검색할 파일명 or 키워드
    max_results: int    # 최대 조회 건수 (1~20)
    file_type: str      # "any" | "doc" | "sheet" | "slide" | "pdf" | "folder"

class PeopleSearchSchema(BaseModel):
    query: str          # 검색할 임직원 이름, 부서명, 직책 키워드
    max_results: int    # 최대 조회 건수 (1~10)

class SheetReadSchema(BaseModel):
    file_name: str      # 스프레드시트 파일 이름 또는 키워드
    range: str          # 읽을 셀 범위 (예: "Sheet1!A1:E10", 미지정이면 "Sheet1!A1:Z100")

class SheetWriteSchema(BaseModel):
    file_name: str      # 스프레드시트 파일 이름 또는 키워드
    sheet_name: str     # 시트 탭 이름 (기본: "Sheet1")
    row_data: List[str] # 추가할 한 행의 데이터 (컬럼 순서대로)

class DocsCreateSchema(BaseModel):
    title: str          # 구글 Docs 문서 제목
    content: str        # 문서 본문 내용 (마크다운 없는 순수 텍스트)

class CalendarFreeSlot(BaseModel):
    label: str = Field(description="추천 레이블 (예: '추천 1')")
    date: str = Field(description="날짜 YYYY-MM-DD 형식")
    start_hour: int = Field(description="시작 시각 24시간제")
    start_minute: int = Field(description="시작 분")
    end_hour: int = Field(description="종료 시각 24시간제")
    end_minute: int = Field(description="종료 분")
    reason: str = Field(description="이 시간대를 추천하는 이유")

class CalendarSuggestionsOutput(BaseModel):
    analysis_text: str = Field(description="여유 시간 분석 및 추천 3개를 포함한 마크다운 텍스트. 마지막에 '추천 N번으로 해줘' 형식 선택 안내 포함.")
    suggestions: List[CalendarFreeSlot] = Field(description="추천 슬롯 3개 목록")
    attendee_names: str = Field(description="초대할 참석자 이름, 쉼표 구분. 없으면 빈 문자열")

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

    # Pre-check 0: CALENDAR_FREE 추천 슬롯 선택 응답 감지 ("추천 N번")
    _free_sugg = state.get("calendar_free_suggestions")
    if isinstance(_free_sugg, dict) and _free_sugg.get("slots"):
        _slot_match = re.search(r'추천\s*([1-3])\s*번', user_input.strip())
        if _slot_match:
            _idx = int(_slot_match.group(1)) - 1
            _slots = _free_sugg["slots"]
            if 0 <= _idx < len(_slots):
                _selected = dict(_slots[_idx])
                _selected["original_request"] = _free_sugg.get("original_request", user_input)
                _selected["attendee_names"] = _free_sugg.get("attendee_names", "")
                print(f"✅ [Supervisor] 추천 {_idx + 1}번 선택 → CALENDAR_WRITE 직접 라우팅")
                return {
                    "current_intent": "CALENDAR_WRITE",
                    "top_intent": "CALENDAR_WRITE",
                    "pending_intents": [],
                    "calendar_selected_slot": _selected,
                    "calendar_free_suggestions": {},
                    "bq_retry_count": 0,
                    "bq_error_log": "",
                    "last_failed_intent": "",
                    "last_error_type": "",
                    "fallback_suggested": False,
                    "awaiting_fallback_query": "",
                    "sources": []
                }

    # Pre-check 1: BQ 권한 실패 후 Drive 폴백 확인 응답 감지
    if state.get("fallback_suggested") and state.get("awaiting_fallback_query"):
        user_input_lower = user_input.strip().lower()
        if any(kw in user_input_lower for kw in _DRIVE_CONFIRM_KEYWORDS):
            original_query = state.get("awaiting_fallback_query", user_input)
            print(f"✅ [Supervisor] BQ 폴백 Drive 확인 응답 감지 → DRIVE_SEARCH (원본 쿼리: {original_query})")
            return {
                "current_intent": "DRIVE_SEARCH",
                "top_intent": "DRIVE_SEARCH",
                "pending_intents": [],
                "bq_retry_count": 0,
                "bq_error_log": "",
                "last_failed_intent": "",
                "last_error_type": "",
                "fallback_suggested": False,
                "awaiting_fallback_query": "",
                "sources": []
            }

    prompt = f"""
    당신은 코웨이 전사 AI 챗봇의 총괄 지휘자입니다.
    사용자 질문을 분석하여 수행해야 할 모든 작업을 intents 목록에 순서대로 담아 반환하세요.
    단일 요청이면 항목 1개, 복수 요청이면 2개 이상을 포함하세요.

    [의도 분류 기준]
    - RAG: 사내 규정, 복리후생, 인사, 가이드 등 텍스트 문서 검색
    - BQ: 매출액, 실적, 예산, 판매량 등 수치 데이터 조회 (예: 집행비용 분석, 출장현황 분석)
      ⚠️ "출장 비용", "해외출장 비용" 등 비용 키워드만으로 BQ 분류 금지. 반드시 "현황", "분석", "실적", "얼마 썼어" 등 실데이터 조회 문맥이 명확해야 BQ. 정책/한도/기준 문의는 RAG.
    - GENERAL: 단순 인사, 안부, 일상 대화 (예: 안녕, 넌 누구야)
    [메일 관련]
    - EMAIL_WRITE: 메일/이메일 초안 작성, 임시보관함 저장 요청 (수신자가 불명확하거나 검토 후 발송 원할 때)
    - EMAIL_SEND: 메일/이메일을 즉시 발송 요청 (수신자가 명확하고 "보내줘", "발송해줘" 등 즉시 전송 의도가 명확할 때)
    - EMAIL_SEARCH: 메일 검색·찾기 요청 (예: 지난주 김과장 메일 찾아줘, 계약서 관련 메일 검색해줘)
    - EMAIL_REPLY: 특정 메일에 회신·답장 요청 (예: 어제 받은 보고서 메일에 회신해줘)
    - EMAIL_READ: 이메일 전체 요약, 브리핑, 읽지 않은 메일 확인 요청
    [캘린더 관련]
    - CALENDAR_WRITE: 캘린더/일정/스케줄 새로 추가·등록·생성 요청 (예: 내일 오후 3시 미팅 일정 잡아줘)
    - CALENDAR_READ: 캘린더/일정 조회·확인 요청 (예: 오늘 내 미팅 일정 알려줘)
    - CALENDAR_RSVP: 캘린더 초대 일정의 참석 여부 업데이트 요청 (예: 참석 여부 확인 안한 것들 참석으로 체크해줘)
    - CALENDAR_UPDATE: 기존 일정 수정·변경 요청 (예: 내일 팀회의 시간 3시로 바꿔줘, 참석자 추가해줘)
    - CALENDAR_DELETE: 기존 일정 삭제·취소 요청 (예: 다음주 워크샵 일정 삭제해줘)
    - CALENDAR_FREE: 일정 여유 시간·빈 시간 조회, 미팅 가능 시간 확인 요청 (예: 이번주 빈 시간 알려줘, 언제 미팅 잡을 수 있어?)
    [할일(Tasks) 관련]
    - TASK_WRITE: 할일·할 일·해야 할 일·테스크·태스크·투두·TODO·to-do 등록·추가 요청
    - TASK_READ: 할일·할 일·해야 할 일·테스크 목록 조회·확인 요청
    - TASK_ACTION: 할일 완료 처리, 삭제, 수정 요청 (예: 보고서 작성 할일 완료로 표시해줘, 할일 제목 바꿔줘)
    [임직원 디렉토리 관련]
    - PEOPLE_SEARCH: 코웨이 임직원 검색, 연락처 조회, 담당자 찾기, 이메일 주소 확인 요청
      → "김철수 연락처 알려줘", "총무팀 담당자 이메일 찾아줘", "홍길동 부서 어디야", "OOO 캘린더에 초대하고 싶어"

    [구글 스프레드시트(Sheets) 관련]
    - SHEET_READ: 구글 스프레드시트 파일의 데이터 조회·읽기 요청
      → "스프레드시트 데이터 읽어줘", "시트에서 데이터 가져와줘", "엑셀 파일 내용 확인해줘"
    - SHEET_WRITE: 구글 스프레드시트에 데이터 입력·추가 요청
      → "스프레드시트에 데이터 추가해줘", "시트에 행 입력해줘", "엑셀에 항목 추가해줘"

    [구글 문서(Docs) 관련]
    - DOCS_CREATE: 구글 Docs 새 문서 생성 요청
      → "구글 Docs 문서 만들어줘", "문서 작성해줘", "보고서 초안 Docs로 만들어줘"

    [구글 드라이브 관련]
    - DRIVE_SEARCH: 사용자가 본인의 구글 드라이브에서 직접 파일을 검색 요청할 때만 사용
      → 반드시 "드라이브", "내 드라이브", "공유 드라이브", "내 파일", "내 문서함" 등의 명시적 드라이브 키워드가 포함되어야 함
      → 예: "내 드라이브에서 결산 보고서 찾아줘", "공유 드라이브에서 기안서 파일 검색해줘"
    - DRIVE_LIST: 사용자가 본인의 드라이브 목록·공유 파일을 명시적으로 요청할 때만 사용
      → 반드시 "드라이브", "내 파일", "공유받은 파일" 등의 명시적 드라이브 키워드가 포함되어야 함
      → 예: "드라이브 최근 파일 보여줘", "공유받은 파일 목록 알려줘"
    [★ 핵심 구분 규칙 - 반드시 준수]
    1. "할일", "할 일", "해야 할 일", "테스크", "태스크", "투두", "to-do", "체크리스트" 키워드 → TASK_WRITE/TASK_READ/TASK_ACTION (절대로 CALENDAR로 분류 금지)
    2. "참석 여부", "참석으로 체크", "참석 확인", "초대 수락", "미응답 일정", "참석 체크" 키워드 → 반드시 CALENDAR_RSVP (CALENDAR_WRITE가 아님)
    3. "일정 등록", "일정 추가", "일정 잡아", "캘린더 추가", "캘린더 등록" 등 새 이벤트 생성 키워드 → CALENDAR_WRITE
    4. "일정 삭제", "일정 취소", "일정 지워" → CALENDAR_DELETE / "일정 변경", "일정 수정", "시간 바꿔" → CALENDAR_UPDATE
    5. "빈 시간", "여유 시간", "언제 가능", "미팅 가능 시간" → CALENDAR_FREE
    6. "보내줘", "발송해줘" + 수신자 명확 → EMAIL_SEND / 수신자 불명확하거나 검토 후 보내고 싶다면 → EMAIL_WRITE
    7. "회신", "답장", "답변 메일" → EMAIL_REPLY / "메일 찾아줘", "메일 검색" → EMAIL_SEARCH
    8. "할일 완료", "완료로 표시", "삭제해줘(할일)", "할일 수정" → TASK_ACTION
    9. "이름 + 연락처/이메일/부서/직책/전화번호 찾아줘" → PEOPLE_SEARCH
       "이름 + 캘린더 초대" 요청 시 (일정 제목·날짜·시간 미포함) → PEOPLE_SEARCH 단독. 대상자 확인 후 사용자가 일정 상세를 입력하면 그때 CALENDAR_WRITE 실행.
       "이름 + 일정 제목 + 날짜 + 시간" 모두 명시된 경우에만 → CALENDAR_WRITE 직접 (예: "김영훈님과 내일 오후 3시 팀미팅 잡아줘")
    10. [🔒 드라이브 격리 원칙 — 절대 준수]
       DRIVE_SEARCH / DRIVE_LIST는 질문에 "드라이브", "내 드라이브", "공유 드라이브", "내 파일", "내 문서함" 중
       하나 이상이 명시적으로 포함된 경우에만 사용하세요.
       "규정 찾아줘", "문서 알려줘", "파일 어디 있어" 처럼 드라이브를 명시하지 않은 문서 관련 질문은
       반드시 RAG로 분류하세요. 드라이브와 사내 지식베이스(RAG)는 절대로 혼용하지 마세요.
    11. 사용자가 "A도 해주고 B도 해줘" 형태로 두 가지를 동시 요청하면 intents에 [A_INTENT, B_INTENT] 순서로 모두 포함하세요.
    12. [🔒 Sheets 격리 원칙] SHEET_READ/SHEET_WRITE는 반드시 "스프레드시트", "구글 시트", "Google Sheets", "시트 파일" 중 하나가 명시된 경우에만 사용.
        "예산 조회", "데이터 보여줘" 같이 스프레드시트를 명시하지 않은 요청은 BQ 또는 RAG로 분류할 것.
        예: "구글 시트에서 예산 데이터 읽어줘" → SHEET_READ / "스프레드시트에 추가해줘" → SHEET_WRITE
    13. [🔒 Docs 격리 원칙] DOCS_CREATE는 반드시 "구글 Docs", "Google Docs", "Docs 문서", "Docs로 만들어줘" 중 하나가 명시된 경우에만 사용.
        "회의록 정리해줘", "보고서 써줘" 처럼 Docs를 명시하지 않으면 GENERAL로 분류할 것.
    14. [🔒 CALENDAR_FREE 우선 원칙] "비어있는 시간에", "여유 시간에", "빈 시간에", "내 스케줄 보고" + "일정 잡아줘/등록해줘" 조합은 반드시 CALENDAR_FREE만 발행. CALENDAR_WRITE 동시 발행 절대 금지.
        추천 후 사용자가 "추천 N번으로 해줘" 등을 선택하면 그때 CALENDAR_WRITE가 실행됨.
        예: "비어있는 시간에 정해인님과 30분 잡아줘" → ["PEOPLE_SEARCH", "CALENDAR_FREE"] (CALENDAR_WRITE 포함 금지)

    [복수 요청 예시]
    - "할일에 등록하고 캘린더에도 추가해줘" → ["TASK_WRITE", "CALENDAR_WRITE"]
    - "메일 요약하고 오늘 일정도 알려줘" → ["EMAIL_READ", "CALENDAR_READ"]
    - "김과장한테 보고서 완료 메일 바로 보내줘" → ["EMAIL_SEND"]
    - "내일 팀회의 시간 2시로 바꿔줘" → ["CALENDAR_UPDATE"]
    - "이번주 빈 시간 알려줘" → ["CALENDAR_FREE"]
    - "보고서 작성 할일 완료 처리해줘" → ["TASK_ACTION"]
    - "내 드라이브에서 2025 결산 파일 찾아줘" → ["DRIVE_SEARCH"]  ← "드라이브" 명시 필수
    - "출장 규정 알려줘" → ["RAG"]  ← 드라이브 미언급이므로 반드시 RAG
    - "해외출장 비용 알려줘" → ["RAG"]  ← 비용 기준/한도 문의는 규정 문서 조회
    - "해외출장 현황 분석해줘" → ["BQ"]  ← 실데이터 조회 문맥 명확
    - "연차 문서 어디 있어?" → ["RAG"]  ← 드라이브 미언급이므로 반드시 RAG
    - "스프레드시트 데이터 읽어줘" → ["SHEET_READ"]
    - "시트에 데이터 추가해줘" → ["SHEET_WRITE"]
    - "구글 Docs 보고서 만들어줘" → ["DOCS_CREATE"]
    - "내일 오후 3시 화상회의 일정 잡아줘" → ["CALENDAR_WRITE"]  ← 온라인 미팅도 CALENDAR_WRITE
    - "김영훈님 캘린더 초대해줘" → ["PEOPLE_SEARCH"]  ← 일정 상세 없음, 대상자 확인 먼저
    - "김영훈님과 내일 오후 3시 인더남 회의 잡아줘" → ["CALENDAR_WRITE"]  ← 이름+제목+날짜+시간 모두 있으면 직접

    [confidence 산출 기준]
    - 0.9 이상: 키워드가 명확하고 인텐트가 분명한 경우
    - 0.7~0.9: 대체로 명확하나 약간 애매한 경우
    - 0.5~0.7: 두 가지 인텐트 가능성이 있어 애매한 경우
    - 0.5 미만: 질문이 너무 모호하거나 의도 파악 불가

    [source_tier 산출 기준]
    - "official": RAG, BQ 인텐트 → 사내 공식 데이터 소스
    - "personal": DRIVE_SEARCH, DRIVE_LIST, SHEET_READ, SHEET_WRITE, DOCS_CREATE → 개인 파일
    - "action": EMAIL_*, CALENDAR_*, TASK_* → 액션 수행
    - "general": GENERAL → 일반 대화

    질문: {user_input}
    """

    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RouteDecision,
            ),
        )
    except Exception as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            print(f"⚠️ [Supervisor] Gemini 쿼터 초과 (429) — 임시 응답 반환")
            raise ValueError("RESOURCE_EXHAUSTED")
        raise
    decision_data = json.loads(response.text)
    intents = decision_data.get("intents", ["GENERAL"])
    confidence = float(decision_data.get("confidence", 1.0))
    source_tier = decision_data.get("source_tier", "general")
    if not intents:
        intents = ["GENERAL"]

    # 확신도 낮고 공식 데이터 소스일 때 먼저 사용자에게 의도 확인
    if confidence < 0.6 and source_tier == "official" and len(intents) == 1:
        clarify_msg = (
            "💡 요청을 좀 더 명확히 해주시면 더 정확하게 안내드릴 수 있어요!\n\n"
            "- **수치/실적 데이터** 조회라면 → 'BQ 데이터 조회해줘'\n"
            "- **규정/가이드 문서** 검색이라면 → '사내 지식베이스에서 찾아줘'\n"
            "- **내 드라이브 파일** 검색이라면 → '드라이브에서 찾아줘'"
        )
        return {
            "messages": [AIMessage(content=clarify_msg)],
            "current_intent": "GENERAL",
            "top_intent": "GENERAL",
            "pending_intents": [],
            "bq_retry_count": 0,
            "bq_error_log": "",
            "last_failed_intent": "",
            "last_error_type": "",
            "fallback_suggested": False,
            "awaiting_fallback_query": "",
            "sources": []
        }

    print(f"✅ [Supervisor] 판단 결과: {intents} (confidence={confidence:.2f}, tier={source_tier})")
    return {
        "current_intent": intents[0],
        "top_intent": intents[0],
        "pending_intents": intents[1:],
        "bq_retry_count": 0,
        "bq_error_log": "",
        "last_failed_intent": "",
        "last_error_type": "",
        "fallback_suggested": False,
        "awaiting_fallback_query": "",
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
        model=LITE_MODEL,
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
    from rag_node import hybrid_search_bq, is_broad_query

    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "employee_all@coway.com")

    # 광범위 질문("알려줘", "정리해줘" 등)은 더 많은 문서를 검색해 누락 방지
    top_k = 8 if is_broad_query(user_input) else 5
    print(f"📊 [RAG] 쿼리 유형: {'광범위' if top_k == 8 else '핀포인트'} → top_k={top_k}")
    context_text, top_dept_code = hybrid_search_bq(user_input, user_email, top_k=top_k)
    
    if not context_text:
        context_text = "시스템에 등록된 사내 규정이나 관련 문서를 찾을 수 없거나, 해당 문서를 열람할 권한이 없습니다."
        print("⚠️ 관련 문서를 찾지 못했습니다.")
        
    return {"retrieved_docs": context_text, "top_dept_code": top_dept_code}

def reasoner_node(state: AgentState):
    print("🧠 [Reasoner] 제미나이 3.5 싱킹 엔진 가동 및 최종 추론 답변 생성 중...")
    user_input = get_last_human_input(state)
    raw_docs = state["retrieved_docs"]

    # 같은 doc_url의 청크(chunk)를 하나로 병합 → LLM 인용번호와 소스카드 수 일치
    docs = raw_docs
    if raw_docs and "\n\n---\n\n" in raw_docs:
        raw_blocks = re.split(r'\n\n---\n\n', raw_docs)
        url_order = []      # 고유 URL 순서 보존
        url_to_block = {}   # url → 대표 블록 텍스트
        for block in raw_blocks:
            url_m = re.search(r'^\[문서URL\]:\s*(https?://\S+)', block, re.MULTILINE)
            u = url_m.group(1).strip() if url_m else f"__nurl_{len(url_order)}"
            if u not in url_to_block:
                url_to_block[u] = block
                url_order.append(u)
            else:
                # 같은 문서의 추가 청크: 상세 내용만 이어 붙임
                extra_m = re.search(r'\[상세 내용\]:\n(.*)', block, re.DOTALL)
                if extra_m:
                    url_to_block[u] += "\n" + extra_m.group(1).strip()
        docs = "\n\n---\n\n".join(url_to_block[u] for u in url_order)
        print(f"🔗 [Reasoner] 청크 병합: {len(raw_blocks)}개 → {len(url_order)}개 고유 문서")

    # ──────────────────────────────────────────────────────────────────────
    # 소스카드 기반 선 추출 → 번호 부여는 그 이후에만
    # (인용번호와 소스카드 수 불일치 버그의 근본 원인 수정:
    #  기존 코드는 num_docs를 전체 블록 수로 먼저 설정한 뒤 소스카드를 나중에 빌드해서
    #  num_docs > len(extracted_sources) 인 상태로 LLM에 전달 → [5][6][7] 유효 통과 → 소스카드 없음)
    # ──────────────────────────────────────────────────────────────────────
    extracted_sources = []
    valid_blocks = []   # URL이 유효한 블록만 = 실제 소스카드로 표시될 블록

    for block in (re.split(r'\n\n---\n\n', docs) if docs else []):
        name_m = re.search(r'^\[문서명\]:\s*(.+)', block, re.MULTILINE)
        url_m  = re.search(r'^\[문서URL\]:\s*(https?://\S+)', block, re.MULTILINE)
        if name_m and url_m:
            n, u = name_m.group(1).strip(), url_m.group(1).strip()
            if u not in [s.get('doc_url') for s in extracted_sources]:
                extracted_sources.append({"doc_name": n, "doc_url": u, "links": ""})
                valid_blocks.append(block)
            # 같은 URL 중복: 소스카드 하나이므로 valid_blocks에도 추가하지 않음
        # URL 없는 블록: LLM 번호 부여 목록에서 제외 → 인용번호 생성 불가

    # Vertex AI / 레거시 마크다운 / fallback 포맷 보조 처리 (BQ 포맷 외 경로 대응)
    if docs and not extracted_sources:
        for url in re.findall(r'원본링크:\s*(https?://[^\s\]\n]+)', docs):
            if url not in [s.get('doc_url') for s in extracted_sources]:
                extracted_sources.append({"doc_name": "사내 규정 지식 파일", "doc_url": url.strip(), "links": ""})
        for name, url in re.findall(r'\[((?:\[[^\]]*\]|[^\]\n])+)\]\((https?://[^\s)]+)\)', docs):
            if url not in [s.get('doc_url') for s in extracted_sources]:
                clean_name = name.replace("원본링크:", "").replace("출처:", "").strip()
                extracted_sources.append({"doc_name": clean_name, "doc_url": url.strip(), "links": ""})
        if not extracted_sources:
            for idx, url in enumerate(re.findall(r'(https?://[^\s\n\)]+)', docs)):
                if url not in [s.get('doc_url') for s in extracted_sources]:
                    extracted_sources.append({"doc_name": f"참고 사규 지침서 {idx+1}", "doc_url": url.strip(), "links": ""})

    # 번호 부여: 소스카드와 1:1 대응하는 블록만 → num_docs = 소스카드 수와 정확히 일치
    num_docs = len(valid_blocks) if valid_blocks else len(extracted_sources)
    if valid_blocks:
        numbered_docs = "\n\n---\n\n".join(
            f"[문서 {i}]\n{block.strip()}"
            for i, block in enumerate(valid_blocks, 1)
        )
    else:
        numbered_docs = docs or ""

    print(f"📎 [Reasoner] 추출된 출처 {len(extracted_sources)}개 / LLM 제공 문서 {num_docs}개: {[s['doc_name'] for s in extracted_sources]}")

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
    당신은 코웨이(Coway) 임직원의 질문 의도를 찰떡같이 파악하고 사내 지식베이스를 바탕으로 가장 정확하고 '논리적인' 답변을 제공하는 최고 수준의 전사 통합 AI 챗봇입니다.

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
    
    11. [🔒 출처 인용 규칙]
    - 아래 검색된 규정 문서는 총 {num_docs}개입니다. 각 문서는 [문서 1], [문서 2], ... 로 표시됩니다.
    - 문서 내용을 인용할 때 반드시 해당 번호만 문장 끝에 표기하세요. (예: [1], [2])
    - ⚠️ 절대 준수: 문서가 {num_docs}개이므로 [1]~[{num_docs}] 번호만 사용 가능합니다. [{num_docs + 1}] 이상은 존재하지 않으므로 절대 사용하지 마세요.
    - 🚫 혼동 금지: 문서 내부의 항목 번호(예: "5. 부당 수령 주의사항", "6. 시차출퇴근제", "7. 출산휴가" 같은 FAQ 목차 번호)는 문서 인용번호가 아닙니다. 오직 [문서 N]으로 표시된 N만 인용번호로 사용하세요.
    - 문서 내의 여러 항목이 동일한 문서에서 왔더라도 번호를 나눠 쓰지 마세요. 동일 문서라면 항상 같은 번호를 사용하세요.
    - 절대로 사용자 화면에 "주요 출처", "참조 문서" 같은 텍스트 리스트를 직접 출력하지 마십시오. (하단 카드로 자동 표시됩니다.)
    - 답변 맨 마지막 줄(|||SUGGESTIONS||| 바로 위)에 아래 포맷으로 출처 JSON을 사출하세요. 이때 검색에서 제공된 모든 문서를 빠짐없이 포함하세요 — 임의로 걸러내지 마세요.
    - 포맷 규격:
    [SOURCE_REPORTS] [{{"doc_name": "실제 문서 이름 1", "doc_url": "해당 문서의 구글드라이브 URL"}}, {{"doc_name": "실제 문서 이름 2", "doc_url": "해당 문서의 구글드라이브 URL"}}]

    [📋 도표 시각화 지침]
    - 수치·비율·통계가 포함된 내용은 마크다운 표(| 헤더 | 헤더 | ... |)로 정리하면 가독성이 높아집니다.
    - [CHART_DATA] 차트 JSON은 RAG 답변에서 절대 사출하지 마세요. 차트는 BigQuery 정형데이터 분석 전용입니다.

    [검색된 규정] (총 {num_docs}개 문서)
    {numbered_docs}
    """
    
    response = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    final_response_text = response.text.strip()

    # 유효 범위 초과 인용번호 제거 (예: 문서 1개인데 [2][3] 사용 시 자동 제거)
    if num_docs > 0:
        final_response_text = re.sub(
            r'\[(\d+)\]',
            lambda m: '' if int(m.group(1)) > num_docs else m.group(0),
            final_response_text
        )

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
    response = ai_client.models.generate_content(model=LITE_MODEL, contents=prompt)
    return {"messages": [AIMessage(content=response.text.strip())]}

def bq_node(state: AgentState):
    print("📊 [BQ] 제미나이 3.5 기반 자율형 다차원 데이터 애널리스트 모드 기동...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # GCP BQ 데이터셋 공유(ACL) 설정 기반 접근 권한 실검증
    # 서비스 계정이 쿼리를 대신 실행하므로, GCP 콘솔 공유 권한을 코드 레벨에서 직접 확인
    if not check_bq_access(user_email):
        return {"messages": [AIMessage(content=(
            "⚠️ **데이터 조회 권한이 없습니다.**\n\n"
            f"'{user_email}' 계정은 해당 데이터에 접근할 권한이 부여되어 있지 않습니다.\n"
            "데이터 조회 권한이 필요하시면 담당 부서에 문의해 주세요."
        ))]}

    system_message = rf"""
    당신은 코웨이의 최고 데이터 분석가(Chief Data Analyst)입니다.
    사용자의 질문에 답변하기 위해 BigQuery 데이터베이스에 직접 유효한 표준 SQL 쿼리를 작성하고 실행하세요.
    
    [사용자 정보]
    현재 질문하는 사용자: '{user_email}' (GCP BQ 데이터셋 ACL 검증 완료)

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
          ⑤ 🔴 [비용 구조 집계 — 누락 금지] 비용 구조 분석 섹션 작성을 위해 아래 항목을 반드시 포함하세요.
             - COALESCE(SUM(SAFE_CAST(lodge_amt AS FLOAT64)), 0) AS total_lodge_amt
             - COALESCE(SUM(SAFE_CAST(meal_amt AS FLOAT64)), 0) AS total_meal_amt
             - COALESCE(SUM(SAFE_CAST(daily_allowance AS FLOAT64)), 0) AS total_daily_amt
             - 교통비 13개 COALESCE 합산 AS total_transport_amt
             → 전체 또는 trip_type별 집계(GROUP BY trip_type) 형태로 포함하세요.
          ⑥ 🔴 [국내 purpose_detail] WHERE trip_type LIKE '%국내%' 조건으로 purpose_detail GROUP BY 건수·비용
          ⑦ 🔴 [해외 purpose_detail] WHERE trip_type LIKE '%해외%' 조건으로 purpose_detail GROUP BY 건수·비용
        * 🚨 [trip_type 필터 안전 규칙] trip_type의 실제 DB 저장값은 '국내', '해외', '국내출장', '해외출장' 등 다를 수 있습니다.
          반드시 exact match(= '국내') 대신 LIKE 패턴(LIKE '%국내%', LIKE '%해외%')을 사용하세요.
        * 🚨 [다중 SQL 지원] 세미콜론(;)으로 구분된 여러 SELECT 문을 생성할 수 있습니다.
          시스템이 각 문장을 개별 실행하여 모든 결과를 LLM에 전달합니다. UNION ALL로 억지로 합치지 마세요.

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
            model=LITE_MODEL,
            contents=[{"role": "system", "parts": [{"text": system_message}]}, {"role": "user", "parts": [{"text": user_input}]}]
        ).text
        generated_sql = sql_response.replace("```sql", "").replace("```", "").strip()
        print(f"🔍 [BQ Generated SQL]:\n{generated_sql}")
    print(f"🔍 [BQ] 사용자({user_email})의 권한 범위 내에서 데이터 탐색 및 SQL 실행 중...")

    try:
        # 다중 SQL 문장(;으로 구분)을 개별 실행 후 합산 — BigQuery는 마지막 결과만 반환하므로 직접 분리 실행
        sql_statements = []
        for raw in re.split(r';\s*\n', generated_sql.rstrip(';')):
            stmt = raw.strip()
            if stmt and re.search(r'\bSELECT\b', stmt, re.IGNORECASE):
                sql_statements.append(stmt)

        if len(sql_statements) <= 1:
            query_job = bq_client.query(generated_sql.rstrip(';'))
            query_results = [dict(row) for row in query_job.result()]
            is_multi_query = False
        else:
            print(f"📊 [BQ] 다중 SQL {len(sql_statements)}개 감지 → 개별 실행 후 합산")
            query_results = {}
            is_multi_query = True
            last_exec_error = None
            for i, stmt in enumerate(sql_statements):
                try:
                    job = bq_client.query(stmt)
                    rows = [dict(row) for row in job.result()]
                    query_results[f"Q{i+1}"] = rows
                    print(f"  ✅ Q{i+1}: {len(rows)}행 수신")
                except Exception as seg_err:
                    last_exec_error = seg_err
                    query_results[f"Q{i+1}_error"] = str(seg_err)[:300]
                    print(f"  ⚠️ Q{i+1} 오류: {str(seg_err)[:100]}")
            # 전체 실패 시 원본 에러 재발생
            has_any_success = any(isinstance(v, list) for v in query_results.values())
            if not has_any_success and last_exec_error:
                raise last_exec_error

        # 데이터 품질 상태 판정
        if is_multi_query:
            has_data = any(isinstance(v, list) and len(v) > 0 for v in query_results.values())
            data_status_guard = "NORMAL" if has_data else "EMPTY"
        else:
            if not query_results or len(query_results) == 0:
                data_status_guard = "EMPTY"
            elif len(query_results) <= 1:
                data_status_guard = "INSUFFICIENT"
            else:
                data_status_guard = "NORMAL"

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
           - 본부별 집행 규모 TOP 순위, 출장 목적지 집중 지역, 주요 출장 목적(purpose_category) 분류
           - 출장 목적 세부(purpose_detail) 상위 빈도 분류 — 국내 출장과 동일하게 해외도 반드시 포함하세요
        3. 국내 출장 분석 (국내 데이터가 있을 때)
           - 본부별 집행 규모, 출장 빈도 TOP 순위, 출장목적 세부(purpose_detail) 지출 패턴 특이사항
        4. 비용 구조 분석 (항목별: 교통비·숙박비·식비·일비 비중)
           - 데이터 JSON에 total_lodge_amt, total_meal_amt, total_daily_amt, total_transport_amt 컬럼이 있으면 반드시 비율로 환산하여 분석하세요.
           - 이 컬럼들이 데이터에 없을 때만 "데이터 없음"으로 기재하세요. 있으면 절대 생략하지 마세요.
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
          ③ 비용 항목별 구성 비율 (교통비/숙박비/식비/일비) (bar) — total_transport_amt, total_lodge_amt, total_meal_amt, total_daily_amt 데이터 사용
          ④ 출장 빈도 TOP 본부 (trip_count 기준 bar)
          - ③번 차트는 total_lodge_amt 등 비용 집계 컬럼이 데이터에 있으면 반드시 그려야 합니다.
          - 데이터에 destination, purpose_category, purpose_detail 등 추가 차원이 있으면 추가 차트를 더 그리세요.
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

        # Access Denied: RAG 폴백 시도 → 없으면 Drive 제안
        if "Access Denied" in err_str or "403" in err_str or "accessDenied" in err_str:
            print("🔒 [BQ] Access Denied → 사내 지식베이스 폴백 시도 중...")
            original_query = get_last_human_input(state)
            rag_context = ""
            try:
                from rag_node import hybrid_search_bq
                rag_email = state["user_info"].get("email", "employee_all@coway.com")
                rag_context, _ = hybrid_search_bq(original_query, rag_email)
            except Exception as rag_err:
                print(f"⚠️ [BQ→RAG 폴백] 오류: {rag_err}")

            if rag_context:
                rag_answer_prompt = f"""사용자 질문: {original_query}
아래 사내 지식베이스 문서를 바탕으로 정확하고 친절하게 답변하세요.

[사내 지식베이스 문서]
{rag_context}
"""
                try:
                    rag_answer = ai_client.models.generate_content(model=MODEL_NAME, contents=rag_answer_prompt).text
                except Exception:
                    rag_answer = rag_context
                return {
                    "messages": [AIMessage(content=rag_answer + "\n\n---\n> 📚 **사내 지식베이스 기반 안내 | BQ 권한이 없어 사내 지식베이스 기반으로 안내해드렸습니다**")],
                    "bq_error_log": "",
                    "bq_retry_count": 0,
                    "last_failed_intent": "BQ",
                    "last_error_type": "",
                    "fallback_suggested": False,
                    "awaiting_fallback_query": "",
                    "refined_query": ""
                }
            else:
                # RAG도 없음 → Drive 폴백 제안
                fallback_msg = (
                    "🔒 **BQ 데이터 접근 권한이 없습니다.**\n\n"
                    f"사내 지식베이스에서도 **'{original_query}'** 관련 문서를 찾지 못했습니다.\n\n"
                    "📂 개인 드라이브에서 관련 파일을 찾아드릴까요?"
                )
                return {
                    "messages": [AIMessage(content=fallback_msg)],
                    "bq_error_log": "",
                    "bq_retry_count": 0,
                    "last_failed_intent": "BQ",
                    "last_error_type": "PERMISSION",
                    "fallback_suggested": True,
                    "awaiting_fallback_query": original_query,
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
    response = ai_client.models.generate_content(model=LITE_MODEL, contents=prompt)
    corrected_sql = response.text.replace("```sql", "").replace("```", "").strip()
    print(f"♻️ [BQ Corrector] 교정 완료된 신규 SQL 사출 (시도 카운트: {retry_cnt}/2)")
    
    return {"refined_query": corrected_sql, "bq_retry_count": retry_cnt}

def email_write_node(state: AgentState):
    print("✉️ [EMAIL_WRITE] 이메일 초안 작성 및 임시보관함 저장 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    compose_prompt = f"""
현재 날짜: {datetime.datetime.now().strftime('%Y-%m-%d')}
사용자 소속: 코웨이 임직원 ({user_email})
사용자 요청: {user_input}

코웨이 비즈니스 메일 형식에 맞게 메일을 작성하세요.
수신자가 명시되지 않은 경우 to는 빈 문자열, cc는 빈 문자열로 설정하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=compose_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailComposeSchema,
            ),
        )
        data = EmailComposeSchema(**json.loads(response.text))

        from email.mime.text import MIMEText
        msg = MIMEText(data.body, 'plain', 'utf-8')
        msg['Subject'] = data.subject
        msg['From'] = user_email
        if data.to:
            msg['To'] = data.to
        if data.cc:
            msg['Cc'] = data.cc

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        gmail = get_workspace_service('gmail', 'v1', user_email)
        gmail.users().drafts().create(userId='me', body={"message": {"raw": raw}}).execute()
        print("✅ [EMAIL_WRITE] Gmail 임시보관함 저장 완료")

        to_line = f"\n- **받는사람:** {data.to}" if data.to else ""
        cc_line = f"\n- **참조:** {data.cc}" if data.cc else ""
        return {"messages": [AIMessage(content=f"✨ **이메일 초안이 작성되어 임시보관함에 저장되었습니다!**{to_line}{cc_line}\n- **제목:** {data.subject}\n\n---\n\n{data.body}\n\n💡 Gmail 임시보관함에서 검토 후 발송하세요.")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ EMAIL_WRITE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 이메일 초안 작성 중 오류: {str(e)}")]}

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
    ai_response = ai_client.models.generate_content(model=LITE_MODEL, contents=prompt).text
    return {"messages": [AIMessage(content=f"📧 **최신 메일 요약 브리핑**\n\n{ai_response}")]}

def email_send_node(state: AgentState):
    print("📤 [EMAIL_SEND] 이메일 즉시 발송 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    compose_prompt = f"""
현재 날짜: {datetime.datetime.now().strftime('%Y-%m-%d')}
발신자: {user_email}
사용자 요청: {user_input}

수신자(to), 참조(cc), 제목(subject), 본문(body)을 추출하세요.
수신자가 명확히 명시된 경우에만 send_now=true로 설정하세요.
본문은 코웨이 비즈니스 메일 형식으로 작성하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=compose_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailComposeSchema,
            ),
        )
        data = EmailComposeSchema(**json.loads(response.text))

        if not data.to:
            return {"messages": [AIMessage(content="⚠️ 수신자(받는사람)가 명확하지 않습니다. 누구에게 보낼지 이메일 주소를 포함해서 다시 요청해 주세요.\n\n예: \"김철수(chulsu.kim@coway.com)에게 보고서 완료 메일 보내줘\"")]}

        from email.mime.text import MIMEText
        msg = MIMEText(data.body, 'plain', 'utf-8')
        msg['Subject'] = data.subject
        msg['From'] = user_email
        msg['To'] = data.to
        if data.cc:
            msg['Cc'] = data.cc

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        gmail = get_workspace_service('gmail', 'v1', user_email)
        gmail.users().messages().send(userId='me', body={"raw": raw}).execute()
        print("✅ [EMAIL_SEND] 이메일 발송 완료")

        cc_line = f"\n- **참조:** {data.cc}" if data.cc else ""
        return {"messages": [AIMessage(content=f"✅ **이메일이 성공적으로 발송되었습니다!**\n\n- **받는사람:** {data.to}{cc_line}\n- **제목:** {data.subject}\n\n---\n\n{data.body}")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ EMAIL_SEND 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 이메일 발송 중 오류: {str(e)}")]}


def email_search_node(state: AgentState):
    print("🔍 [EMAIL_SEARCH] Gmail 검색 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now()
    search_prompt = f"""
현재 날짜: {now.strftime('%Y-%m-%d')}
사용자 요청: {user_input}

사용자의 메일 검색 의도에 맞는 Gmail 검색 쿼리를 작성하세요.
Gmail 검색 연산자 예시: from:user@co.kr, subject:보고서, is:unread, after:2026/06/01, before:2026/06/30
복합 조건은 AND로 연결하세요. max_results는 기본 10.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=search_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailSearchSchema,
            ),
        )
        data = EmailSearchSchema(**json.loads(response.text))
        gmail = get_workspace_service('gmail', 'v1', user_email)

        results = gmail.users().messages().list(
            userId='me', q=data.gmail_query, maxResults=min(data.max_results, 20)
        ).execute()
        messages = results.get('messages', [])

        if not messages:
            return {"messages": [AIMessage(content=f"🔍 **검색 결과가 없습니다.**\n\n검색 조건: `{data.gmail_query}`")]}

        email_text = ""
        for idx, msg_info in enumerate(messages):
            msg = gmail.users().messages().get(
                userId='me', id=msg_info['id'], format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()
            headers = msg.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '제목 없음')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '알수없음')
            date_str = next((h['value'] for h in headers if h['name'] == 'Date'), '')[:16]
            snippet = msg.get('snippet', '')[:100]
            email_text += f"[{idx+1}] {subject}\n  발신: {sender}\n  날짜: {date_str}\n  내용: {snippet}...\n\n"

        summary_prompt = f"사용자 요청: {user_input}\n검색된 메일:\n{email_text}\n검색 결과를 간결하게 정리해 주세요."
        ai_response = ai_client.models.generate_content(model=LITE_MODEL, contents=summary_prompt).text
        return {"messages": [AIMessage(content=f"🔍 **메일 검색 결과** ({len(messages)}건)\n\n{ai_response}")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ EMAIL_SEARCH 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 메일 검색 중 오류: {str(e)}")]}


def email_reply_node(state: AgentState):
    print("↩️ [EMAIL_REPLY] 회신 메일 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    reply_prompt = f"""
사용자 요청: {user_input}
회신할 메일을 찾기 위한 검색어, 회신 내용, 전체 회신 여부를 추출하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=reply_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailReplySchema,
            ),
        )
        data = EmailReplySchema(**json.loads(response.text))
        gmail = get_workspace_service('gmail', 'v1', user_email)

        results = gmail.users().messages().list(
            userId='me', q=data.search_query, maxResults=1
        ).execute()
        messages = results.get('messages', [])

        if not messages:
            return {"messages": [AIMessage(content=f"⚠️ 회신할 메일을 찾지 못했습니다.\n\n검색 조건: `{data.search_query}`\n\n더 구체적인 제목이나 발신자를 알려주세요.")]}

        orig = gmail.users().messages().get(userId='me', id=messages[0]['id'], format='metadata',
                                             metadataHeaders=['From', 'Subject', 'Message-ID', 'To']).execute()
        headers = orig.get('payload', {}).get('headers', [])
        orig_from = next((h['value'] for h in headers if h['name'] == 'From'), '')
        orig_subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
        orig_msg_id = next((h['value'] for h in headers if h['name'] == 'Message-ID'), '')
        orig_to = next((h['value'] for h in headers if h['name'] == 'To'), '')

        subject = orig_subject if orig_subject.startswith('Re:') else f"Re: {orig_subject}"
        to_addr = orig_from
        if data.reply_all and orig_to:
            to_addr = f"{orig_from}, {orig_to}"

        ai_body_prompt = f"원본 메일 발신자: {orig_from}\n원본 제목: {orig_subject}\n회신 지시사항: {data.reply_body}\n\n코웨이 비즈니스 메일 형식으로 회신 본문을 작성하세요."
        reply_body_text = ai_client.models.generate_content(model=LITE_MODEL, contents=ai_body_prompt).text

        from email.mime.text import MIMEText
        msg = MIMEText(reply_body_text, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = user_email
        msg['To'] = to_addr
        if orig_msg_id:
            msg['In-Reply-To'] = orig_msg_id
            msg['References'] = orig_msg_id

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        thread_id = orig.get('threadId', '')
        gmail.users().messages().send(userId='me', body={"raw": raw, "threadId": thread_id}).execute()

        return {"messages": [AIMessage(content=f"↩️ **회신 메일이 발송되었습니다!**\n\n- **받는사람:** {to_addr}\n- **제목:** {subject}\n\n---\n\n{reply_body_text}")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ EMAIL_REPLY 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 회신 처리 중 오류: {str(e)}")]}


def calendar_read_node(state: AgentState):
    print("📅 [CALENDAR_READ] 구글 캘린더 일정을 조회하는 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now(datetime.timezone.utc)
    today_str = f"{now.year}년 {now.month}월 {now.day}일"

    # LLM으로 조회 날짜 범위 추출 (미지정 시 오늘 하루로 기본값)
    range_prompt = f"""
    현재 날짜: {today_str}
    사용자 요청에서 캘린더 조회 날짜 범위를 추출하세요.

    [날짜 범위 규칙]
    - "오늘", 날짜 미지정 → startYear/Month/Day = endYear/Month/Day = 오늘
    - "내일" → 내일 날짜로 start/end 동일
    - "이번 주" → 이번 주 월요일 ~ 일요일
    - "이번 달", "이번달" → 이번 달 1일 ~ 말일
    - "6월 17일~20일", "17일부터 20일까지" → start=17일, end=20일
    - "다음 주" → 다음 주 월요일 ~ 일요일
    - "6월" → 6월 1일 ~ 6월 30일

    사용자 요청: {user_input}
    """
    try:
        range_response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=range_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarReadSchema,
            ),
        )
        rng = CalendarReadSchema(**json.loads(range_response.text))
    except Exception:
        # 파싱 실패 시 오늘 하루로 fallback
        rng = CalendarReadSchema(
            startYear=now.year, startMonth=now.month, startDay=now.day,
            endYear=now.year, endMonth=now.month, endDay=now.day,
        )

    time_min = datetime.datetime(rng.startYear, rng.startMonth, rng.startDay, 0, 0, 0,
                                  tzinfo=datetime.timezone.utc).isoformat()
    time_max = datetime.datetime(rng.endYear, rng.endMonth, rng.endDay, 23, 59, 59,
                                  tzinfo=datetime.timezone.utc).isoformat()
    is_single_day = (rng.startYear == rng.endYear and rng.startMonth == rng.endMonth and rng.startDay == rng.endDay)
    if is_single_day:
        range_label = f"{rng.startMonth}월 {rng.startDay}일"
    else:
        range_label = f"{rng.startMonth}월 {rng.startDay}일 ~ {rng.endMonth}월 {rng.endDay}일"

    schedule_text = ""
    try:
        calendar = get_workspace_service('calendar', 'v3', user_email)
        events_result = calendar.events().list(
            calendarId=user_email,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        if not events:
            schedule_text = f"{range_label} 예정된 일정이 없습니다."
        else:
            for e in events:
                start = e['start'].get('dateTime', e['start'].get('date', ''))
                if 'T' in start:
                    date_part = f"{int(start[5:7])}월 {int(start[8:10])}일"
                    time_part = start[11:16]
                    time_str = f"{date_part} {time_part}" if not is_single_day else time_part
                else:
                    date_part = f"{int(start[5:7])}월 {int(start[8:10])}일"
                    time_str = f"{date_part} 종일" if not is_single_day else "종일"
                desc = e.get('description', '')
                desc_str = f"\n ↳ 상세내용: {desc}" if desc else ""
                schedule_text += f"- {time_str} | {e.get('summary', '(제목 없음)')}{desc_str}\n"
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ 캘린더 읽기 에러: {str(e)}")
        schedule_text = "일정 조회 중 오류가 발생했습니다."

    prompt = f"사용자 질문: {user_input}\n조회 범위: {range_label}\n일정 데이터:\n{schedule_text}\n지시사항: 날짜·시간 기준으로 명확히 나열하고 상세내용 포함하여 요약하세요."
    ai_response = ai_client.models.generate_content(model=LITE_MODEL, contents=prompt).text
    return {"messages": [AIMessage(content=f"📅 **{range_label} 일정 브리핑**\n\n{ai_response}")]}

def calendar_write_node(state: AgentState):
    print("📅 [CALENDAR_WRITE] 구조화된 일정 데이터 추출 및 추가 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 추천 슬롯 선택으로 직행하는 경우 — LLM 시간 추출 없이 슬롯 데이터 직접 사용
    selected_slot = state.get("calendar_selected_slot") or {}
    if selected_slot.get("date"):
        try:
            date_str = selected_slot["date"]
            year, month, day = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
            s_h, s_m = selected_slot["start_hour"], selected_slot["start_minute"]
            e_h, e_m = selected_slot["end_hour"], selected_slot["end_minute"]
            start_time = datetime.datetime(year, month, day, s_h, s_m).isoformat()
            end_time   = datetime.datetime(year, month, day, e_h, e_m).isoformat()
            date_label = f"{month}월 {day}일 {s_h:02d}:{s_m:02d} ~ {e_h:02d}:{e_m:02d}"

            # 제목 추출 (원래 요청 기반 짧은 LLM 호출)
            original_req = selected_slot.get("original_request", user_input)
            title_resp = ai_client.models.generate_content(
                model=LITE_MODEL,
                contents=f"사용자 요청: {original_req}\n미팅 일정 제목을 15자 이내 명사구로 추출하세요. 없으면 '팀 미팅' 반환.",
            )
            title = title_resp.text.strip().strip('"').strip("'")[:30] or "팀 미팅"

            # 참석자 이름 → 이메일 변환
            attendee_names_raw = selected_slot.get("attendee_names", "")
            attendee_emails = []
            if attendee_names_raw:
                raw_list = [a.strip() for a in attendee_names_raw.split(',') if a.strip()]
                attendee_emails, ambig_msg = _resolve_attendees_or_disambiguate(raw_list, user_email)
                if ambig_msg:
                    return {"messages": [AIMessage(content=ambig_msg)], "calendar_selected_slot": {}}
                attendee_emails = [em for em in attendee_emails if em and em != user_email]

            calendar = get_workspace_service('calendar', 'v3', user_email)
            event = {
                'summary': title,
                'start': {'dateTime': start_time, 'timeZone': 'Asia/Seoul'},
                'end':   {'dateTime': end_time,   'timeZone': 'Asia/Seoul'},
            }
            if attendee_emails:
                event['attendees'] = [{'email': em} for em in attendee_emails]

            send_updates = 'all' if attendee_emails else 'none'
            calendar.events().insert(calendarId=user_email, body=event, sendUpdates=send_updates).execute()

            attendee_line = ""
            if attendee_emails:
                attendee_display = attendee_names_raw or ', '.join(attendee_emails)
                attendee_line = f"\n- **초대된 참석자:** {attendee_display} (초대 메일 자동 발송)"
            print(f"✅ [CALENDAR_WRITE] 추천 슬롯 직행 등록 완료: {title} ({date_label})")
            return {
                "messages": [AIMessage(content=f"✅ **일정이 성공적으로 등록되었습니다!**\n\n- **일정명:** {title}\n- **기간:** {date_label}{attendee_line}\n\n구글 캘린더에 완벽하게 연동되었습니다.")],
                "calendar_selected_slot": {},
            }
        except Exception as e:
            if "AUTH_REQUIRED_FOR:" in str(e):
                raise
            print(f"⚠️ [CALENDAR_WRITE] 슬롯 직행 등록 에러: {e}")
            return {"messages": [AIMessage(content=f"⚠️ 일정 추가 중 오류가 발생했습니다: {e}")], "calendar_selected_slot": {}}

    # 의도 검증: 새 일정 생성 요청이 아닌 경우 명확화 요청 반환
    NON_CREATE_KEYWORDS = ["참석 여부", "참석으로", "참석 체크", "초대 수락", "미응답", "참석 확인",
                           "삭제해", "취소해", "변경해", "수정해", "지워", "없애"]
    if any(kw in user_input for kw in NON_CREATE_KEYWORDS):
        print(f"⚠️ [CALENDAR_WRITE] 비생성 요청 감지 → 명확화 유도")
        return {"messages": [AIMessage(content="📅 죄송합니다. 말씀하신 내용은 새 일정 추가가 아닌 것 같습니다.\n\n저는 현재 다음 캘린더 작업을 지원합니다:\n- **일정 등록/추가**: \"내일 오후 3시 팀 미팅 일정 잡아줘\"\n- **휴가·연차 등록**: \"6월 20일~22일 연차 등록해줘\"\n- **반차 등록**: \"오늘 오후 반차 추가해줘\"\n- **일정 조회**: \"이번 주 일정 알려줘\"\n- **초대 참석 처리**: \"참석 안 한 일정들 수락해줘\"\n\n원하시는 작업을 구체적으로 말씀해 주세요!")]}

    # 시간 미지정 가드: 종일 이벤트가 아닌 미팅/회의인데 시간 정보가 없으면 먼저 문의
    ALLDAY_SAFE_KEYWORDS = ["연차", "휴가", "재택", "외근", "공휴일", "반차", "종일", "워크샵", "하루"]
    has_time_info = bool(re.search(r'\d+시|\d+분|오전|오후|아침|저녁|점심|새벽', user_input))
    is_allday_safe = any(kw in user_input for kw in ALLDAY_SAFE_KEYWORDS)
    if not has_time_info and not is_allday_safe:
        print(f"⚠️ [CALENDAR_WRITE] 시간 정보 없음 → 날짜/시간 문의")
        return {"messages": [AIMessage(content=(
            "📅 일정을 잡겠습니다!\n\n"
            "**날짜와 시간**을 알려주세요.\n\n"
            "- 몇 월 며칠인지\n"
            "- 시작 시간과 종료 시간 (또는 '1시간' 등 소요 시간)\n\n"
            "예: '내일 오후 3시~5시' 또는 '6월 20일 오전 10시부터 1시간'"
        ))]}

    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"

    extract_prompt = f"""
    현재 날짜: {today_str}
    사용자의 요청에서 추가할 구글 캘린더 일정 정보를 정확히 추출하여 오직 객체로 반환하세요.

    [이벤트 유형 분류 규칙 — 우선순위 순서대로 적용]

    ① 반차 판단 (isHalfDay=true, isAllDay=false)
    - "반차", "오전반차", "오후반차", "오전 반차", "오후 반차" 키워드가 포함된 경우
    - halfDayPeriod 결정:
        * "오전반차" / "오전 반차" → halfDayPeriod: "morning"
        * "오후반차" / "오후 반차" / 그냥 "반차" (오전/오후 구분 없음) → halfDayPeriod: "afternoon"
          (날짜 없이 그냥 "반차"는 이미 출근한 상황으로 판단 → 오후 반차로 처리)
    - startHour/startMinute/endHour/endMinute 모두 0으로 설정 (시스템이 자동 고정)

    ② 종일 이벤트 판단 (isAllDay=true, isHalfDay=false)
    - 휴가, 연차, 공휴일, 재택근무, 외근일 등 시간 지정이 없는 하루 또는 여러 날 이벤트
    - startHour/startMinute/endHour/endMinute 모두 0으로 설정

    ③ 시간 지정 이벤트 (isAllDay=false, isHalfDay=false)
    - 미팅, 회의, 약속, 교육 등 구체적 시간이 있는 이벤트

    [날짜 규칙]
    - "6월 17일~20일", "17일부터 20일까지" 같이 범위가 주어지면 endYear/endMonth/endDay에 마지막 날짜를 넣으세요.
    - 단일 날짜(예: "6월 17일 휴가")면 endYear/endMonth/endDay = year/month/day와 동일하게 설정하세요.
    - 날짜 미지정이면 오늘(현재 날짜)로 설정하세요.

    [시간 해석 규칙 — isAllDay=false이고 isHalfDay=false인 경우]
    - 오전/오후 구분 없이 표기된 시간은 업무시간(09:00~18:00) 기준으로 해석하세요.
      예: "1시" → 13:00, "2시" → 14:00, "3시" → 15:00, "9시" → 09:00, "10시" → 10:00
    - "내일", "모레" 등 상대적 날짜 표현을 현재 날짜 기준으로 정확히 계산하세요.
    - 종료시간이 없다면 시작시간으로부터 1시간 뒤로 설정하세요.

    [isOnline 규칙]
    - "온라인", "화상회의", "화상 미팅", "미트", "Meet", "비대면", "Zoom", "화상", "원격" 키워드 포함 시 isOnline=true
    - 그 외 모든 경우 isOnline=false

    사용자 요청: {user_input}
    """
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarEventSchema,
            ),
        )
        data = CalendarEventSchema(**json.loads(response.text))
        calendar = get_workspace_service('calendar', 'v3', user_email)

        if data.isAllDay:
            # 종일 이벤트 (휴가·연차·재택 등): Google Calendar API는 end가 exclusive이므로 마지막날 +1일
            end_date_obj = datetime.date(data.endYear, data.endMonth, data.endDay) + datetime.timedelta(days=1)
            event = {
                'summary': data.title,
                'start': {'date': f"{data.year:04d}-{data.month:02d}-{data.day:02d}"},
                'end': {'date': end_date_obj.strftime('%Y-%m-%d')},
            }
            if data.year == data.endYear and data.month == data.endMonth and data.day == data.endDay:
                date_label = f"{data.month}월 {data.day}일 (종일)"
            else:
                date_label = f"{data.month}월 {data.day}일 ~ {data.endMonth}월 {data.endDay}일 (종일)"

        elif data.isHalfDay:
            # 반차: 오전(09:00~13:00) / 오후(14:00~18:00) 고정
            if data.halfDayPeriod == "morning":
                s_hour, s_min, e_hour, e_min = 9, 0, 13, 0
                period_label = "오전 반차 (09:00 ~ 13:00)"
            else:  # afternoon (기본값)
                s_hour, s_min, e_hour, e_min = 14, 0, 18, 0
                period_label = "오후 반차 (14:00 ~ 18:00)"
            start_time = datetime.datetime(data.year, data.month, data.day, s_hour, s_min).isoformat()
            end_time = datetime.datetime(data.year, data.month, data.day, e_hour, e_min).isoformat()
            event = {
                'summary': data.title,
                'start': {'dateTime': start_time, 'timeZone': 'Asia/Seoul'},
                'end': {'dateTime': end_time, 'timeZone': 'Asia/Seoul'},
            }
            date_label = f"{data.month}월 {data.day}일 {period_label}"

        else:
            # 시간 지정 이벤트 (미팅·회의 등)
            start_time = datetime.datetime(data.year, data.month, data.day, data.startHour, data.startMinute).isoformat()
            end_time = datetime.datetime(data.endYear, data.endMonth, data.endDay, data.endHour, data.endMinute).isoformat()
            event = {
                'summary': data.title,
                'start': {'dateTime': start_time, 'timeZone': 'Asia/Seoul'},
                'end': {'dateTime': end_time, 'timeZone': 'Asia/Seoul'},
            }
            h1, m1 = str(data.startHour).zfill(2), str(data.startMinute).zfill(2)
            h2, m2 = str(data.endHour).zfill(2), str(data.endMinute).zfill(2)
            if data.year == data.endYear and data.month == data.endMonth and data.day == data.endDay:
                date_label = f"{data.month}월 {data.day}일 {h1}:{m1} ~ {h2}:{m2}"
            else:
                date_label = f"{data.month}월 {data.day}일 {h1}:{m1} ~ {data.endMonth}월 {data.endDay}일 {h2}:{m2}"

        # 참석자 처리: 동명이인 감지 → 선택 요청, 단독 매칭 → 자동 해석 후 초대
        if data.attendees:
            raw_list = [a.strip() for a in data.attendees.split(',') if a.strip()]
            attendee_emails, ambig_msg = _resolve_attendees_or_disambiguate(raw_list, user_email)
            if ambig_msg:
                return {"messages": [AIMessage(content=ambig_msg)]}
            attendee_emails = [em for em in attendee_emails if em and em != user_email]
        else:
            attendee_emails = []

        if attendee_emails:
            event['attendees'] = [{'email': em} for em in attendee_emails]

        # 온라인 미팅 요청 시 Google Meet 링크 자동 생성
        if getattr(data, 'isOnline', False):
            event['conferenceData'] = {
                'createRequest': {
                    'requestId': f"meet-{user_email}-{data.year}{data.month:02d}{data.day:02d}",
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                }
            }

        send_updates = 'all' if attendee_emails else 'none'
        insert_kwargs = {'calendarId': user_email, 'body': event, 'sendUpdates': send_updates}
        if getattr(data, 'isOnline', False):
            insert_kwargs['conferenceDataVersion'] = 1
        created_event = calendar.events().insert(**insert_kwargs).execute()

        attendee_line = ""
        if attendee_emails:
            # 화면에는 원본 이름(data.attendees)을 표시 — 이메일 주소 직접 노출 방지
            attendee_display = data.attendees if data.attendees else ', '.join(attendee_emails)
            attendee_line = f"\n- **초대된 참석자:** {attendee_display} (초대 메일 자동 발송)"
        meet_line = ""
        if getattr(data, 'isOnline', False):
            meet_url = created_event.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri', '')
            if meet_url:
                meet_line = f"\n- **Google Meet 링크:** {meet_url}"
        return {"messages": [AIMessage(content=f"✅ **일정이 성공적으로 등록되었습니다!**\n\n- **일정명:** {data.title}\n- **기간:** {date_label}{attendee_line}{meet_line}\n\n구글 캘린더에 완벽하게 연동되었습니다.")]}
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
        ai_response = ai_client.models.generate_content(model=LITE_MODEL, contents=prompt).text
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

    # 의도 검증: 새 할일 생성 요청이 아닌 경우 명확화 요청 반환
    NON_CREATE_TASK_KEYWORDS = ["완료로 표시", "완료 처리", "삭제해", "지워", "없애", "취소해", "수정해", "변경해", "목록 보여", "조회", "확인해줘"]
    if any(kw in user_input for kw in NON_CREATE_TASK_KEYWORDS):
        print(f"⚠️ [TASK_WRITE] 비생성 요청 감지 → 명확화 유도")
        return {"messages": [AIMessage(content="📝 죄송합니다. 말씀하신 내용은 새 할일 추가가 아닌 것 같습니다.\n\n현재 지원하는 할일(Tasks) 기능:\n- **할일 추가**: \"파이썬 교육 자료 정리 할일로 추가해줘\"\n- **마감일 지정**: \"내일까지 보고서 작성 할일 등록해줘\"\n- **할일 목록 조회**: \"내 할일 목록 보여줘\"\n\n원하시는 작업을 구체적으로 말씀해 주세요!")]}

    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"

    extract_prompt = f"""
    현재 날짜: {today_str}
    사용자 요청: {user_input}
    구글 할 일(Tasks)에 등록할 정보를 정확히 구조화하여 추출하세요. 마감일 형식은 YYYY-MM-DD 입니다. 명시되지 않았다면 빈 문자열로 하세요.
    """
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
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


def task_action_node(state: AgentState):
    print("✅ [TASK_ACTION] 할 일 작업(완료/수정/삭제) 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    action_prompt = f"""
현재 날짜: {datetime.datetime.now().strftime('%Y-%m-%d')}
사용자 요청: {user_input}

수행할 작업(action)을 결정하세요:
- complete: 완료 처리
- delete: 삭제
- update: 제목/마감일/메모 수정

search_title: 찾을 할일 제목 키워드
action: complete / delete / update 중 하나
new_title: 수정 시 새 제목 (수정 아니면 빈 문자열)
new_due: 수정 시 새 마감일 YYYY-MM-DD (없으면 빈 문자열)
new_notes: 수정 시 새 메모 (없으면 빈 문자열)
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=action_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TaskActionSchema,
            ),
        )
        data = TaskActionSchema(**json.loads(response.text))
        tasks_service = get_workspace_service('tasks', 'v1', user_email)

        task_lists = tasks_service.tasklists().list().execute()
        all_tasks = []
        for tl in task_lists.get('items', []):
            tl_id = tl['id']
            tl_title = tl.get('title', '')
            result = tasks_service.tasks().list(tasklist=tl_id, showCompleted=False).execute()
            for t in result.get('items', []):
                t['_tasklist_id'] = tl_id
                t['_tasklist_title'] = tl_title
                all_tasks.append(t)

        keyword = data.search_title.strip().lower()
        matched = [t for t in all_tasks if keyword in t.get('title', '').lower()] if keyword else all_tasks[:5]

        if not matched:
            return {"messages": [AIMessage(content=f"⚠️ 할 일 목록에서 **'{data.search_title}'**을(를) 찾지 못했습니다.\n\n현재 진행 중인 할 일 키워드를 더 정확히 알려주세요.")]}

        task = matched[0]
        task_id = task['id']
        tasklist_id = task['_tasklist_id']
        task_title = task.get('title', '')

        if data.action == 'complete':
            tasks_service.tasks().patch(tasklist=tasklist_id, task=task_id, body={'status': 'completed'}).execute()
            return {"messages": [AIMessage(content=f"✅ **'{task_title}'** 할 일을 완료 처리했습니다!")]}
        elif data.action == 'delete':
            tasks_service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
            return {"messages": [AIMessage(content=f"🗑️ **'{task_title}'** 할 일을 삭제했습니다.")]}
        elif data.action == 'update':
            patch_body = {}
            if data.new_title:
                patch_body['title'] = data.new_title
            if data.new_due:
                patch_body['due'] = data.new_due + "T00:00:00.000Z"
            if data.new_notes:
                patch_body['notes'] = data.new_notes
            if not patch_body:
                return {"messages": [AIMessage(content="⚠️ 수정할 내용을 파악하지 못했습니다. 새 제목, 마감일 또는 메모를 알려주세요.")]}
            tasks_service.tasks().patch(tasklist=tasklist_id, task=task_id, body=patch_body).execute()
            changed = []
            if data.new_title:
                changed.append(f"제목: {data.new_title}")
            if data.new_due:
                changed.append(f"마감일: {data.new_due}")
            if data.new_notes:
                changed.append(f"메모: {data.new_notes}")
            return {"messages": [AIMessage(content=f"✏️ **'{task_title}'** 할 일을 수정했습니다.\n\n변경 내용: {', '.join(changed)}")]}
        else:
            return {"messages": [AIMessage(content=f"⚠️ 작업 유형을 파악하지 못했습니다. 완료/삭제/수정 중 어떤 작업을 원하시는지 명확히 알려주세요.")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ TASK_ACTION 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 할 일 작업 중 오류: {str(e)}")]}


def calendar_rsvp_node(state: AgentState):
    print("📅 [CALENDAR_RSVP] 미응답 캘린더 초대 일정 참석 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now()
    today_str = f"{now.year}년 {now.month}월 {now.day}일"

    # LLM으로 대상 날짜 범위 파싱
    try:
        range_response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=f"""현재 날짜: {today_str}
사용자 요청에서 조회할 날짜 범위를 추출하세요. 명시 없으면 이번 달 전체로 설정하세요.
사용자 요청: {user_input}""",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarReadSchema,
            ),
        )
        r = CalendarReadSchema(**json.loads(range_response.text))
        time_min = datetime.datetime(r.startYear, r.startMonth, r.startDay, 0, 0, 0).isoformat() + "+09:00"
        time_max = datetime.datetime(r.endYear, r.endMonth, r.endDay, 23, 59, 59).isoformat() + "+09:00"
    except Exception:
        # 기본값: 이번 달 전체
        time_min = datetime.datetime(now.year, now.month, 1).isoformat() + "+09:00"
        next_month = (now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        time_max = (next_month - datetime.timedelta(seconds=1)).isoformat() + "+09:00"

    try:
        calendar = get_workspace_service('calendar', 'v3', user_email)

        events_result = calendar.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            maxResults=100
        ).execute()

        pending_events = []
        for event in events_result.get('items', []):
            for attendee in event.get('attendees', []):
                if attendee.get('self', False) and attendee.get('responseStatus') == 'needsAction':
                    pending_events.append(event)
                    break

        if not pending_events:
            return {"messages": [AIMessage(content="✅ 해당 기간에 참석 여부가 미확인된 초대 일정이 없습니다.")]}

        accepted_names = []
        for event in pending_events:
            new_attendees = [
                dict(a, responseStatus='accepted') if a.get('self', False) else a
                for a in event.get('attendees', [])
            ]
            calendar.events().patch(
                calendarId='primary',
                eventId=event['id'],
                body={'attendees': new_attendees},
                sendUpdates='none'
            ).execute()
            accepted_names.append(event.get('summary', '(제목 없음)'))

        event_list = "\n".join(f"- {name}" for name in accepted_names)
        return {"messages": [AIMessage(content=f"✅ **{len(accepted_names)}개 일정을 참석으로 처리했습니다!**\n\n{event_list}\n\n모든 일정의 참석 여부가 업데이트되었습니다.")]}

    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ RSVP 처리 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 참석 처리 중 오류가 발생했습니다: {str(e)}")]}


def calendar_delete_node(state: AgentState):
    print("🗑️ [CALENDAR_DELETE] 캘린더 일정 삭제 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now()
    parse_prompt = f"""
현재 날짜: {now.strftime('%Y-%m-%d')}
사용자 요청: {user_input}
삭제할 일정의 키워드(search_query)와 날짜(target_year, target_month, target_day)를 추출하세요.
날짜가 명확하지 않으면 오늘 날짜를 사용하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=parse_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarDeleteSchema,
            ),
        )
        data = CalendarDeleteSchema(**json.loads(response.text))
        cal = get_workspace_service('calendar', 'v3', user_email)

        target_date = datetime.date(data.target_year, data.target_month, data.target_day)
        time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
        time_max = datetime.datetime.combine(target_date + datetime.timedelta(days=7), datetime.time.min).isoformat() + 'Z'

        events_result = cal.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime', maxResults=20
        ).execute()
        events = events_result.get('items', [])

        keyword = data.search_query.strip().lower()
        matched = [e for e in events if keyword in e.get('summary', '').lower()] if keyword else events[:3]

        if not matched:
            return {"messages": [AIMessage(content=f"⚠️ 삭제할 일정을 찾지 못했습니다.\n\n검색어: '{data.search_query}'\n날짜 범위: {target_date} 전후 7일")]}

        if len(matched) > 1:
            event_list = "\n".join(f"- {e.get('summary', '제목없음')} ({e['start'].get('dateTime', e['start'].get('date', ''))[:16]})" for e in matched[:5])
            return {"messages": [AIMessage(content=f"⚠️ **{len(matched)}개**의 일정이 검색되었습니다. 더 구체적인 일정 이름이나 날짜를 알려주세요.\n\n{event_list}")]}

        event = matched[0]
        event_title = event.get('summary', '제목없음')
        event_time = event['start'].get('dateTime', event['start'].get('date', ''))[:16]
        cal.events().delete(calendarId='primary', eventId=event['id']).execute()
        return {"messages": [AIMessage(content=f"🗑️ **'{event_title}'** 일정이 삭제되었습니다.\n\n- 삭제된 일정: {event_title}\n- 일정 시간: {event_time}")]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ CALENDAR_DELETE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 일정 삭제 중 오류: {str(e)}")]}


def calendar_update_node(state: AgentState):
    print("✏️ [CALENDAR_UPDATE] 캘린더 일정 수정 처리 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now()
    parse_prompt = f"""
현재 날짜: {now.strftime('%Y-%m-%d')}
사용자 요청: {user_input}
수정할 일정의 검색어(search_query)와 기존 날짜(target_year, target_month, target_day),
새 제목(new_title, 변경 없으면 빈 문자열), 새 날짜/시간(new_year, new_month, new_day, new_start_hour, new_start_minute, new_end_hour, new_end_minute),
추가 참석자(attendees_add, 이메일 쉼표 구분, 없으면 빈 문자열)를 추출하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=parse_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarUpdateSchema,
            ),
        )
        data = CalendarUpdateSchema(**json.loads(response.text))
        cal = get_workspace_service('calendar', 'v3', user_email)

        target_date = datetime.date(data.target_year, data.target_month, data.target_day)
        time_min = datetime.datetime.combine(target_date - datetime.timedelta(days=1), datetime.time.min).isoformat() + 'Z'
        time_max = datetime.datetime.combine(target_date + datetime.timedelta(days=7), datetime.time.min).isoformat() + 'Z'

        events_result = cal.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime', maxResults=20
        ).execute()
        events = events_result.get('items', [])

        keyword = data.search_query.strip().lower()
        matched = [e for e in events if keyword in e.get('summary', '').lower()] if keyword else events[:3]

        if not matched:
            return {"messages": [AIMessage(content=f"⚠️ 수정할 일정을 찾지 못했습니다.\n\n검색어: '{data.search_query}'")]}

        event = matched[0]
        event_id = event['id']
        patch_body = {}

        if data.new_title:
            patch_body['summary'] = data.new_title

        if data.new_year and data.new_month and data.new_day:
            tz_str = event['start'].get('timeZone', 'Asia/Seoul')
            new_start = datetime.datetime(data.new_year, data.new_month, data.new_day, data.new_start_hour, data.new_start_minute)
            new_end = datetime.datetime(data.new_year, data.new_month, data.new_day, data.new_end_hour, data.new_end_minute)
            patch_body['start'] = {'dateTime': new_start.isoformat(), 'timeZone': tz_str}
            patch_body['end'] = {'dateTime': new_end.isoformat(), 'timeZone': tz_str}

        if data.attendees_add:
            raw_inputs = [e.strip() for e in data.attendees_add.split(',') if e.strip()]
            resolved_emails, ambig_msg = _resolve_attendees_or_disambiguate(raw_inputs, user_email)
            if ambig_msg:
                return {"messages": [AIMessage(content=ambig_msg)]}
            existing_attendees = event.get('attendees', [])
            existing_emails = {a['email'] for a in existing_attendees}
            for em in resolved_emails:
                if em and em not in existing_emails:
                    existing_attendees.append({'email': em})
            patch_body['attendees'] = existing_attendees

        if not patch_body:
            return {"messages": [AIMessage(content="⚠️ 수정할 내용을 파악하지 못했습니다. 변경할 제목, 시간, 또는 참석자를 알려주세요.")]}

        cal.events().patch(calendarId='primary', eventId=event_id, body=patch_body, sendUpdates='all').execute()

        orig_title = event.get('summary', '제목없음')
        changes = []
        if data.new_title:
            changes.append(f"제목: {orig_title} → {data.new_title}")
        if 'start' in patch_body:
            changes.append(f"시간: {patch_body['start']['dateTime'][:16]}")
        if data.attendees_add:
            changes.append(f"참석자 추가: {data.attendees_add}")
        return {"messages": [AIMessage(content=f"✏️ **'{orig_title}'** 일정이 수정되었습니다!\n\n" + "\n".join(f"- {c}" for c in changes))]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ CALENDAR_UPDATE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 일정 수정 중 오류: {str(e)}")]}


def calendar_free_node(state: AgentState):
    print("🕐 [CALENDAR_FREE] 일정 여유 시간 조회 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    now = datetime.datetime.now()
    parse_prompt = f"""
현재 날짜: {now.strftime('%Y-%m-%d')}
사용자 요청: {user_input}
조회할 기간의 시작(startYear, startMonth, startDay)과 끝(endYear, endMonth, endDay)을 추출하세요.
기간이 명확하지 않으면 오늘부터 7일간으로 설정하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=parse_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarFreeBusySchema,
            ),
        )
        data = CalendarFreeBusySchema(**json.loads(response.text))
        cal = get_workspace_service('calendar', 'v3', user_email)

        time_min = datetime.datetime(data.startYear, data.startMonth, data.startDay, 0, 0).isoformat() + '+09:00'
        time_max = datetime.datetime(data.endYear, data.endMonth, data.endDay, 23, 59).isoformat() + '+09:00'

        freebusy_result = cal.freebusy().query(body={
            "timeMin": time_min, "timeMax": time_max,
            "timeZone": "Asia/Seoul",
            "items": [{"id": "primary"}]
        }).execute()

        busy_periods = freebusy_result.get('calendars', {}).get('primary', {}).get('busy', [])

        start_date = datetime.date(data.startYear, data.startMonth, data.startDay)
        end_date = datetime.date(data.endYear, data.endMonth, data.endDay)

        busy_prompt = f"""
사용자의 요청: {user_input}
오늘 날짜: {start_date}
조회 기간: {start_date} ~ {end_date}
바쁜 시간대 (KST 기준):
{chr(10).join(f"- {b['start'][:16]} ~ {b['end'][:16]}" for b in busy_periods) if busy_periods else "없음"}

[analysis_text 작성 지침]
- 업무시간(09:00~18:00) 기준 여유 시간대를 분석하고 [추천 1] [추천 2] [추천 3] 형식으로 각각의 특징 포함
- 마지막 줄에 반드시 안내: "원하시는 번호로 말씀해 주세요. 예: '추천 2번으로 해줘'"
- 한국어로 친절하게 마크다운 작성

[suggestions 작성 지침]
- 각 추천의 date(YYYY-MM-DD), start_hour/start_minute, end_hour/end_minute을 정확히 포함
- 사용자 요청의 미팅 시간(예: 30분)을 반영하여 종료 시각 계산

[attendee_names 작성 지침]
- 사용자 요청에서 초대할 참석자 이름만 추출, 쉼표 구분. 없으면 빈 문자열.
"""
        result = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=busy_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CalendarSuggestionsOutput,
            ),
        )
        output = CalendarSuggestionsOutput(**json.loads(result.text))
        suggestions_data = {
            "slots": [s.model_dump() for s in output.suggestions],
            "attendee_names": output.attendee_names,
            "original_request": user_input,
        }
        return {
            "messages": [AIMessage(content=f"🕐 **일정 여유 시간 분석** ({start_date} ~ {end_date})\n\n{output.analysis_text}")],
            "calendar_free_suggestions": suggestions_data,
            "calendar_selected_slot": {},
        }
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ CALENDAR_FREE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 여유 시간 조회 중 오류: {str(e)}")]}


_CALENDAR_AMBIG_MARKER = "동명이인 선택 필요"


def lookup_employee_candidates(name: str, user_email: str) -> list:
    """
    이름으로 임직원 후보 최대 5명 조회.
    이미 이메일 형식이면 단일 항목 리스트 반환.
    반환 형식: [{'name': str, 'email': str, 'dept': str, 'title': str}]
    """
    if '@' in name:
        return [{'name': name, 'email': name, 'dept': '', 'title': ''}]
    try:
        people_service = get_workspace_service('people', 'v1', user_email)
        results = people_service.people().searchDirectoryPeople(
            query=name,
            readMask='names,emailAddresses,organizations',
            sources=['DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE'],
            pageSize=5
        ).execute()
        candidates = []
        for p in results.get('people', []):
            p_name = p.get('names', [{}])[0].get('displayName', '') if p.get('names') else ''
            emails = p.get('emailAddresses', [])
            email_val = emails[0].get('value', '') if emails else ''
            org = p.get('organizations', [{}])[0] if p.get('organizations') else {}
            if email_val:
                candidates.append({
                    'name': p_name,
                    'email': email_val,
                    'dept': org.get('department', ''),
                    'title': org.get('title', '')
                })
        return candidates
    except Exception as e:
        print(f"⚠️ [People] 후보 조회 실패 ({name}): {e}")
        return []


def resolve_employee_email(name_or_email: str, user_email: str) -> str:
    """단건 이름→이메일 변환. 동명이인 시 첫 번째 반환 (단순 참고용 — 캘린더 초대에는 사용 금지)."""
    candidates = lookup_employee_candidates(name_or_email, user_email)
    if candidates:
        print(f"👤 [People] '{name_or_email}' → {candidates[0]['email']}")
        return candidates[0]['email']
    return name_or_email


def _resolve_attendees_or_disambiguate(raw_list: list, user_email: str) -> tuple:
    """
    참석자 이름 목록을 이메일로 변환.
    동명이인 발견 시 즉시 빈 리스트 + 사용자에게 보여줄 안내 메시지 반환.
    정상 처리 시 (resolved_emails, "") 반환.
    반환: (resolved_emails: list[str], ambig_message: str)
    """
    resolved = []
    ambig_blocks = []

    for raw in raw_list:
        if '@' in raw:
            resolved.append(raw)
            continue

        candidates = lookup_employee_candidates(raw, user_email)

        if len(candidates) == 0:
            print(f"⚠️ [People] '{raw}' 검색 결과 없음 — 원본 그대로 사용")
            resolved.append(raw)

        elif len(candidates) == 1:
            print(f"👤 [People] '{raw}' → {candidates[0]['email']} (단독 매칭)")
            resolved.append(candidates[0]['email'])

        else:
            # 동명이인: 완전 일치하는 이름만 걸러서 1명이면 통과, 아니면 선택 요청
            exact = [c for c in candidates if c['name'] == raw]
            if len(exact) == 1:
                print(f"👤 [People] '{raw}' 동명이인 중 정확 일치 1명 → {exact[0]['email']}")
                resolved.append(exact[0]['email'])
            else:
                # 선택 안내 블록 구성
                target = exact if exact else candidates
                lines = [f"**'{raw}'** 님이 여러 명 검색됩니다:"]
                for i, c in enumerate(target, 1):
                    info = f"{c['dept']} {c['title']}".strip() or "소속 정보 없음"
                    lines.append(f"  {i}. {c['name']} ({info}) — `{c['email']}`")
                ambig_blocks.append("\n".join(lines))

    if ambig_blocks:
        msg = (
            f"❓ **[{_CALENDAR_AMBIG_MARKER}]**\n\n"
            + "\n\n".join(ambig_blocks)
            + "\n\n"
            "이메일 주소를 직접 사용하거나 부서명을 붙여 다시 요청해 주세요.\n"
            "예: `\"개발팀 김철수`랑 이영희랑 내일 3시 팀미팅 잡아줘\"`\n"
            "또는: `\"kim.cs@coway.com이랑 이영희랑 내일 3시 팀미팅 잡아줘\"`"
        )
        return [], msg

    return resolved, ""


def people_search_node(state: AgentState):
    print("👥 [PEOPLE_SEARCH] 코웨이 임직원 디렉토리 검색 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    search_prompt = f"""
사용자 요청: {user_input}
검색할 임직원 이름, 부서명, 직책 키워드(query)와 최대 결과 수(max_results, 기본 5)를 추출하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=search_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PeopleSearchSchema,
            ),
        )
        data = PeopleSearchSchema(**json.loads(response.text))
        people_service = get_workspace_service('people', 'v1', user_email)

        results = people_service.people().searchDirectoryPeople(
            query=data.query,
            readMask='names,emailAddresses,organizations,phoneNumbers',
            sources=['DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE'],
            pageSize=min(data.max_results, 10)
        ).execute()

        people_list = results.get('people', [])

        if not people_list:
            return {"messages": [AIMessage(content=(
                f"👥 **'{data.query}'** 검색 결과가 없습니다.\n\n"
                "다른 이름이나 부서명으로 다시 검색해 주세요."
            ))]}

        # 캘린더 초대 요청 여부 감지
        INVITE_KEYWORDS = ["캘린더 초대", "일정 초대", "초대해", "초대 해", "초대하고"]
        is_invite_context = any(kw in user_input for kw in INVITE_KEYWORDS)

        # 공통: 이름[부서] 목록 구성 — displayName에 이미 부서가 포함된 경우 중복 방지
        parsed = []
        for p in people_list:
            name = p.get('names', [{}])[0].get('displayName', '이름없음') if p.get('names') else '이름없음'
            email_val = p.get('emailAddresses', [{}])[0].get('value', '') if p.get('emailAddresses') else ''
            org = p.get('organizations', [{}])[0] if p.get('organizations') else {}
            dept = org.get('department', '')
            parsed.append({'name': name, 'email': email_val, 'dept': dept})

        lines = []
        for i, p in enumerate(parsed):
            # displayName에 이미 부서가 포함된 경우(예: "김영훈[코스트관리팀/...]") 중복 추가 방지
            if p['dept'] and p['dept'] not in p['name']:
                dept_tag = f" [{p['dept']}]"
            else:
                dept_tag = ""
            lines.append(f"**{p['name']}**{dept_tag}")
        result_text = "\n".join(f"{i+1}. {line}" for i, line in enumerate(lines))

        # ── 캘린더 초대 컨텍스트 ──────────────────────────────
        if is_invite_context:
            first = parsed[0]
            # 예시용 순수 한글 이름 추출 (괄호 이전 부분)
            clean_name = first['name'].split('[')[0].strip()
            # 부서 표시: displayName에 없는 경우만 별도 표기
            if first['dept'] and first['dept'] not in first['name']:
                person_display = f"**{clean_name}** [{first['dept']}]"
            else:
                person_display = f"**{first['name']}**"

            if len(parsed) == 1:
                # 단독 매칭: 본인 확인 + 일정 상세 한 번에 요청
                return {"messages": [AIMessage(content=(
                    f"👥 {person_display} 님이 맞으신가요?\n\n"
                    f"맞으시면, 아래 내용을 알려주세요:\n\n"
                    f"- **일정 제목**: (예: 팀미팅, 업무협의)\n"
                    f"- **날짜**: (예: 내일, 6월 20일)\n"
                    f"- **시작~종료 시간**: (예: 오후 3시~4시)\n\n"
                    f"작성 예시: \"{clean_name}님과 내일 오후 3시~4시 팀미팅 잡아줘\""
                ))]}
            else:
                # 복수 매칭: 결과 목록 + 이름/부서/일정 상세 모두 한 번에 요청
                return {"messages": [AIMessage(content=(
                    f"👥 **'{data.query}'** 님이 여러 분 검색됩니다:\n\n{result_text}\n\n"
                    f"아래 내용을 모두 알려주시면 바로 캘린더를 생성해서 초대해드리겠습니다.\n\n"
                    f"- **부서명 + 이름**: (위 목록에서 해당하는 분의 부서명과 이름)\n"
                    f"- **일정 제목**: (예: 팀미팅, 업무협의)\n"
                    f"- **날짜**: (예: 내일, 6월 20일)\n"
                    f"- **시작~종료 시간**: (예: 오후 3시~4시)\n\n"
                    f"작성 예시: \"OO팀 {data.query}님과 내일 오후 3시~4시 업무 미팅 잡아줘\""
                ))]}

        # ── 일반 임직원 조회 컨텍스트 (이름[부서]만 표시) ──────
        return {"messages": [AIMessage(content=(
            f"👥 **임직원 검색 결과** ({len(people_list)}건)\n\n{result_text}"
        ))]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ PEOPLE_SEARCH 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 임직원 검색 중 오류: {str(e)}")]}


def sheet_read_node(state: AgentState):
    print("📊 [SHEET_READ] 구글 스프레드시트 데이터 조회 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 키워드 게이트: 명시적 스프레드시트 언급 없으면 차단
    user_input_lower = user_input.lower()
    if not any(kw in user_input_lower for kw in _SHEET_TRIGGER_KEYWORDS):
        return {"messages": [AIMessage(content=(
            "📊 스프레드시트 조회는 '구글 시트', '스프레드시트' 등을 명시해 주세요.\n"
            "사내 공식 데이터 조회라면 'BQ 데이터 조회해줘'로 요청해 주세요."
        ))]}

    extract_prompt = f"""
사용자 요청: {user_input}
읽을 스프레드시트 파일 이름(file_name)과 셀 범위(range, 예: "Sheet1!A1:E20")를 추출하세요.
범위 미지정이면 range를 "Sheet1!A1:Z100"으로 설정하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SheetReadSchema,
            ),
        )
        data = SheetReadSchema(**json.loads(response.text))

        drive_service = get_workspace_service('drive', 'v3', user_email)
        query = f"name contains '{data.file_name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        files = drive_service.files().list(q=query, pageSize=5, fields="files(id,name,webViewLink)").execute().get('files', [])

        if not files:
            return {"messages": [AIMessage(content=f"📊 **'{data.file_name}'** 스프레드시트를 드라이브에서 찾을 수 없습니다.\n\n파일명을 정확히 입력해 주세요.")]}

        if len(files) > 1:
            file_list = "\n".join(f"- [{f['name']}]({f.get('webViewLink','')})" for f in files[:5])
            return {"messages": [AIMessage(content=f"📊 **'{data.file_name}'** 파일이 여러 개 검색됩니다. 파일명을 더 정확히 알려주세요:\n\n{file_list}")]}

        file_id = files[0]['id']
        file_name = files[0]['name']
        file_url = files[0].get('webViewLink', '')
        sheets_service = get_workspace_service('sheets', 'v4', user_email)
        result = sheets_service.spreadsheets().values().get(spreadsheetId=file_id, range=data.range).execute()
        values = result.get('values', [])

        if not values:
            return {"messages": [AIMessage(content=f"📊 **{file_name}** — 지정 범위({data.range})에 데이터가 없습니다.\n\n[파일 열기]({file_url})")]}

        header = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        md_table = "| " + " | ".join(str(c) for c in header) + " |\n"
        md_table += "| " + " | ".join(["---"] * len(header)) + " |\n"
        for row in rows[:20]:
            padded = list(row) + [""] * (len(header) - len(row))
            md_table += "| " + " | ".join(str(c) for c in padded) + " |\n"

        extra = f"\n\n> 전체 {len(rows)}행 중 최대 20행 표시" if len(rows) > 20 else ""
        file_link = f"\n\n[📎 스프레드시트 열기]({file_url})" if file_url else ""
        return {"messages": [AIMessage(content=f"📊 **{file_name}** ({data.range})\n\n{md_table}{extra}{file_link}{_SHEET_DISCLAIMER}")]}

    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ SHEET_READ 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 스프레드시트 읽기 중 오류: {str(e)}")]}


def sheet_write_node(state: AgentState):
    print("📊 [SHEET_WRITE] 구글 스프레드시트 데이터 추가 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 키워드 게이트
    user_input_lower = user_input.lower()
    if not any(kw in user_input_lower for kw in _SHEET_TRIGGER_KEYWORDS):
        return {"messages": [AIMessage(content=(
            "✏️ 스프레드시트 데이터 추가는 '구글 시트', '스프레드시트' 등을 명시해 주세요."
        ))]}

    # 2단계 확인: 이미 확인 대기 중인 쓰기 작업인지 체크
    sheet_confirm = _get_sheet_write_confirmed(state)
    if sheet_confirm:
        try:
            file_id = sheet_confirm.get('file_id')
            file_name = sheet_confirm.get('file_name', '')
            file_url = sheet_confirm.get('file_url', '')
            sheet_name = sheet_confirm.get('sheet_name', 'Sheet1')
            row_data = sheet_confirm.get('row_data', [])
            sheets_service = get_workspace_service('sheets', 'v4', user_email)
            range_str = f"{sheet_name}!A:Z"
            sheets_service.spreadsheets().values().append(
                spreadsheetId=file_id, range=range_str,
                valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS',
                body={'values': [row_data]}
            ).execute()
            row_preview = " | ".join(str(c) for c in row_data)
            file_link = f"\n\n[📎 스프레드시트 열기]({file_url})" if file_url else ""
            return {"messages": [AIMessage(content=f"✅ **{file_name}** — 새 행이 추가되었습니다!\n\n추가된 데이터: `{row_preview}`{file_link}{_SHEET_DISCLAIMER}")]}
        except Exception as e:
            if "AUTH_REQUIRED_FOR:" in str(e):
                raise
            return {"messages": [AIMessage(content=f"⚠️ 스프레드시트 쓰기 중 오류: {str(e)}")]}

    extract_prompt = f"""
사용자 요청: {user_input}
추가할 스프레드시트 파일 이름(file_name), 시트 탭 이름(sheet_name, 기본 "Sheet1"), 추가할 행 데이터 배열(row_data)을 추출하세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SheetWriteSchema,
            ),
        )
        data = SheetWriteSchema(**json.loads(response.text))

        drive_service = get_workspace_service('drive', 'v3', user_email)
        query = f"name contains '{data.file_name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        files = drive_service.files().list(q=query, pageSize=5, fields="files(id,name,webViewLink)").execute().get('files', [])

        if not files:
            return {"messages": [AIMessage(content=f"📊 **'{data.file_name}'** 스프레드시트를 드라이브에서 찾을 수 없습니다.")]}

        if len(files) > 1:
            file_list = "\n".join(f"- [{f['name']}]({f.get('webViewLink','')})" for f in files[:5])
            return {"messages": [AIMessage(content=f"📊 파일이 여러 개 검색됩니다. 더 정확한 파일명을 알려주세요:\n\n{file_list}")]}

        file_id = files[0]['id']
        file_name = files[0]['name']
        file_url = files[0].get('webViewLink', '')
        row_preview = " | ".join(str(c) for c in data.row_data)
        pending = json.dumps({"file_id": file_id, "file_name": file_name, "file_url": file_url,
                               "sheet_name": data.sheet_name, "row_data": data.row_data}, ensure_ascii=False)
        confirm_msg = (
            f"📊 **{_SHEET_WRITE_CONFIRM_MARKER}?**\n\n"
            f"- **파일:** {file_name}\n"
            f"- **시트:** {data.sheet_name or 'Sheet1'}\n"
            f"- **추가될 데이터:** `{row_preview}`\n\n"
            f"추가하려면 **'네'** 라고 답해주세요.\n\n"
            f"__PENDING_SHEET_WRITE__{pending}__END__"
        )
        return {"messages": [AIMessage(content=confirm_msg)]}

    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ SHEET_WRITE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 스프레드시트 쓰기 중 오류: {str(e)}")]}


def docs_create_node(state: AgentState):
    print("📄 [DOCS_CREATE] 구글 Docs 문서 생성 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 키워드 게이트: 명시적 Docs 요청 없으면 의도 확인
    user_input_lower = user_input.lower()
    if not any(kw in user_input_lower for kw in _DOCS_TRIGGER_KEYWORDS):
        return {"messages": [AIMessage(content=(
            "📄 구글 Docs 문서 생성은 '구글 Docs', 'Docs 문서 만들어줘' 등으로 명시해 주세요.\n"
            "단순 요약이나 답변이 필요하시면 그냥 질문해 주세요!"
        ))]}

    extract_prompt = f"""
사용자 요청: {user_input}
생성할 구글 Docs 문서의 제목(title)과 본문 내용(content)을 추출하세요.
content는 마크다운 없이 순수 텍스트로 작성하세요. 내용 미지정 시 빈 문자열로 두세요.
"""
    try:
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=extract_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DocsCreateSchema,
            ),
        )
        data = DocsCreateSchema(**json.loads(response.text))
        docs_service = get_workspace_service('docs', 'v1', user_email)

        # 문서 생성
        doc = docs_service.documents().create(body={'title': data.title}).execute()
        doc_id = doc.get('documentId')

        # 본문 내용이 있으면 삽입
        if data.content:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': [{'insertText': {'location': {'index': 1}, 'text': data.content}}]}
            ).execute()

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return {"messages": [AIMessage(content=(
            f"📄 **Google Docs 문서가 생성되었습니다!**\n\n"
            f"- **제목:** {data.title}\n"
            f"- **링크:** [문서 열기]({doc_url})\n\n"
            f"위 링크를 클릭하면 바로 편집할 수 있습니다."
            f"{_DOCS_DISCLAIMER}"
        ))]}

    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ DOCS_CREATE 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ Google Docs 생성 중 오류: {str(e)}")]}


DRIVE_TRIGGER_KEYWORDS = [
    "드라이브", "내 드라이브", "공유 드라이브", "내 파일", "내 문서함",
    "공유받은 파일", "공유받은 문서", "내 구글 드라이브", "drive"
]

_DRIVE_DISCLAIMER = "\n\n---\n> 📂 **개인/공유 드라이브 출처 — 사내 지식베이스 아님**"

# 드라이브 확인 흐름 상수
_SHEET_DISCLAIMER = "\n\n---\n> 📊 **개인 드라이브 스프레드시트 출처 — 사내 지식베이스 아님**"
_DOCS_DISCLAIMER = "\n\n---\n> 📄 **개인 드라이브 Docs 출처 — 사내 지식베이스 아님**"
_SHEET_TRIGGER_KEYWORDS = ["스프레드시트", "구글 시트", "google sheets", "시트 파일", "엑셀 파일"]
_DOCS_TRIGGER_KEYWORDS = ["구글 docs", "google docs", "docs 문서", "docs로", "구글독스"]
_SHEET_WRITE_CONFIRM_MARKER = "스프레드시트에 다음 데이터를 추가할까요"
_DRIVE_CONFIRM_KEYWORDS = [
    "네", "응", "맞아", "맞습니다", "맞아요", "진행", "해줘", "해주세요",
    "확인", "예", "yes", "ok", "ㅇㅇ", "그래", "그렇습니다"
]
_DRIVE_LIST_KEYWORDS = ["목록", "최근 파일", "공유받은 파일", "파일 목록", "list", "최근"]


def _get_sheet_write_confirmed(state: AgentState) -> dict:
    """시트 쓰기 확인 응답 감지. 확인됐으면 {'file_id','file_name','sheet_name','row_data'} 반환, 아니면 {}"""
    user_input = get_last_human_input(state).strip().lower()
    if not any(kw in user_input for kw in _DRIVE_CONFIRM_KEYWORDS):
        return {}
    for msg in reversed(state["messages"]):
        if hasattr(msg, 'type') and msg.type == 'ai' and _SHEET_WRITE_CONFIRM_MARKER in msg.content:
            import re
            m = re.search(r'__PENDING_SHEET_WRITE__({.*?})__END__', msg.content, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    pass
    return {}


def drive_search_node(state: AgentState):
    print("📁 [DRIVE_SEARCH] Google Drive 파일 검색 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 드라이브 명시 키워드 없으면 RAG 안내로 즉시 반환 (Supervisor 오분류 방어)
    if not any(kw in user_input for kw in DRIVE_TRIGGER_KEYWORDS):
        print("⚠️ [DRIVE_SEARCH] 드라이브 키워드 미감지 → RAG 안내 반환")
        return {"messages": [AIMessage(content=(
            "🔍 사내 규정이나 공식 문서를 찾으시는 건가요?\n\n"
            "코봇의 **사내 지식베이스(RAG)**는 공식 등록된 규정 문서 기반으로 답변드립니다.\n"
            "예: \"출장 규정 알려줘\", \"연차 사용 방법은?\"\n\n"
            "**개인 또는 공유 드라이브**에서 직접 파일을 검색하시려면 '드라이브'를 명시해 주세요.\n"
            "예: \"**내 드라이브**에서 결산 보고서 찾아줘\", \"**공유 드라이브**에서 기안서 검색해줘\""
        ))]}

    # 검색 범위 감지: user_input 기준 (LLM보다 키워드 직접 판단이 정확)
    SHARED_KEYWORDS = ["공유 드라이브", "공유드라이브", "공유받은", "shared drive"]
    MINE_KEYWORDS = ["내 드라이브", "내드라이브", "내 구글 드라이브", "내 파일", "내 문서함"]
    if any(kw in user_input for kw in SHARED_KEYWORDS):
        scope = "shared"
    elif any(kw in user_input for kw in MINE_KEYWORDS):
        scope = "mine"
    else:
        scope = "all"

    scope_label = "내 드라이브" if scope == "mine" else "공유 드라이브" if scope == "shared" else "전체 드라이브"
    print(f"✅ [DRIVE_SEARCH] 범위={scope_label} → 병렬 검색 진행")

    search_prompt = f"""
사용자 요청: {user_input}
구글 드라이브에서 검색할 핵심 키워드(query)와 파일 유형(file_type: doc/sheet/slide/pdf/folder/any 중 하나)을 추출하세요.

query 추출 규칙:
- 반드시 핵심 단어 1~2개만 추출 (조사·형용사·"관련"·"문서" 등 불필요한 단어 제외)
- 예: "출장 관련 보고서 찾아줘" → query = "출장"
- 예: "경비 정산 파일 검색해줘" → query = "경비 정산"
- 예: "2024 예산 계획서" → query = "예산 계획서"
max_results는 항상 10으로 설정하세요.
"""
    try:
        import concurrent.futures

        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=search_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DriveSearchSchema,
            ),
        )
        data = DriveSearchSchema(**json.loads(response.text))
        drive_svc = get_workspace_service('drive', 'v3', user_email)

        mime_map = {
            'doc': 'application/vnd.google-apps.document',
            'sheet': 'application/vnd.google-apps.spreadsheet',
            'slide': 'application/vnd.google-apps.presentation',
            'pdf': 'application/pdf',
            'folder': 'application/vnd.google-apps.folder',
        }

        # 파일 유형 필터
        mime_filter = ""
        if data.file_type and data.file_type not in ('any', 'all') and data.file_type in mime_map:
            mime_filter = f" and mimeType = '{mime_map[data.file_type]}'"

        # 범위별 소유권 필터
        # ※ scope="shared"는 sharedWithMe 미사용 — Team Drive 파일은 해당 플래그 없음.
        #   corpora='allDrives'만으로 공유 드라이브 포함 검색 처리.
        scope_filter = ""
        if scope == "mine":
            scope_filter = " and 'me' in owners"

        name_q = f"name contains '{data.query}' and trashed = false{mime_filter}{scope_filter}"
        full_q = f"fullText contains '{data.query}' and trashed = false{mime_filter}{scope_filter}"

        # 공유 드라이브 포함 공통 파라미터
        list_kwargs = dict(
            pageSize=10,
            fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            orderBy="recency desc",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        if scope in ("shared", "all"):
            list_kwargs["corpora"] = "allDrives"

        # 병렬 검색: name + fullText 동시 실행
        def _search(q):
            try:
                return drive_svc.files().list(q=q, **list_kwargs).execute().get('files', [])
            except Exception as sub_e:
                print(f"⚠️ [DRIVE_SEARCH] 서브쿼리 오류: {sub_e}")
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            name_future = ex.submit(_search, name_q)
            full_future = ex.submit(_search, full_q)
            name_files = name_future.result()
            full_files = full_future.result()

        # 중복 제거 (ID 기준) — fullText 결과 우선, name 결과로 보강
        seen_ids = set()
        merged = []
        for f in full_files + name_files:
            if f['id'] not in seen_ids:
                seen_ids.add(f['id'])
                merged.append(f)
        files = merged[:10]

        if not files:
            return {"messages": [AIMessage(content=(
                f"📁 **드라이브 검색 결과가 없습니다.**\n\n"
                f"검색어: **'{data.query}'** | 범위: **{scope_label}**\n\n"
                f"다른 키워드로 다시 검색해 주세요."
                f"{_DRIVE_DISCLAIMER}"
            ))]}

        type_label = {
            'application/vnd.google-apps.document': '📄 문서',
            'application/vnd.google-apps.spreadsheet': '📊 스프레드시트',
            'application/vnd.google-apps.presentation': '📑 프레젠테이션',
            'application/pdf': '📋 PDF',
            'application/vnd.google-apps.folder': '📁 폴더',
        }
        file_lines = []
        for f in files:
            icon = type_label.get(f.get('mimeType', ''), '📄')
            modified = f.get('modifiedTime', '')[:10]
            link = f.get('webViewLink', '')
            file_lines.append(f"{icon} [{f['name']}]({link}) — {modified}")

        pdf_notice = "\n\n> 💡 PDF·PPT 파일은 내부 내용이 아닌 **파일명 기준**으로만 검색됩니다."
        return {"messages": [AIMessage(content=(
            f"📁 **드라이브 검색 결과** ({len(files)}건 / {scope_label})\n\n"
            + "\n".join(file_lines)
            + pdf_notice
            + _DRIVE_DISCLAIMER
        ))]}

    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ DRIVE_SEARCH 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 드라이브 검색 중 오류: {str(e)}")]}


def drive_list_node(state: AgentState):
    print("📂 [DRIVE_LIST] Google Drive 최근 파일 목록 조회 중...")
    user_input = get_last_human_input(state)
    user_email = state["user_info"].get("email", "unknown")

    # 드라이브 명시 키워드 없으면 안내 반환 (Supervisor 오분류 방어)
    if not any(kw in user_input for kw in DRIVE_TRIGGER_KEYWORDS):
        print("⚠️ [DRIVE_LIST] 드라이브 키워드 미감지 → 안내 반환")
        return {"messages": [AIMessage(content=(
            "📂 드라이브 파일 목록을 조회하시려면 '드라이브'를 명시해 주세요.\n\n"
            "예: \"**내 드라이브** 최근 파일 보여줘\", \"**공유받은 파일** 목록 알려줘\""
        ))]}

    print("✅ [DRIVE_LIST] 드라이브 키워드 감지 → 즉시 목록 조회 진행")
    try:
        drive = get_workspace_service('drive', 'v3', user_email)

        shared_results = drive.files().list(
            q="sharedWithMe = true and trashed = false",
            pageSize=10,
            fields="files(id, name, mimeType, modifiedTime, owners, webViewLink, sharingUser)",
            orderBy="modifiedTime desc"
        ).execute()
        shared_files = shared_results.get('files', [])

        recent_results = drive.files().list(
            q="'me' in owners and trashed = false",
            pageSize=10,
            fields="files(id, name, mimeType, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc"
        ).execute()
        recent_files = recent_results.get('files', [])

        type_label = {
            'application/vnd.google-apps.document': '📄',
            'application/vnd.google-apps.spreadsheet': '📊',
            'application/vnd.google-apps.presentation': '📑',
            'application/pdf': '📋',
            'application/vnd.google-apps.folder': '📁',
        }

        shared_lines = []
        for f in shared_files[:5]:
            icon = type_label.get(f.get('mimeType', ''), '📄')
            sharer = f.get('sharingUser', {}).get('displayName', '알수없음')
            link = f.get('webViewLink', '')
            shared_lines.append(f"{icon} [{f['name']}]({link}) — 공유자: {sharer}")

        recent_lines = []
        for f in recent_files[:5]:
            icon = type_label.get(f.get('mimeType', ''), '📄')
            modified = f.get('modifiedTime', '')[:10]
            link = f.get('webViewLink', '')
            recent_lines.append(f"{icon} [{f['name']}]({link}) — {modified}")

        shared_section = "\n".join(shared_lines) if shared_lines else "공유받은 파일이 없습니다."
        recent_section = "\n".join(recent_lines) if recent_lines else "최근 파일이 없습니다."

        return {"messages": [AIMessage(content=(
            f"📂 **Google Drive 현황**\n\n"
            f"**공유받은 파일 (최근 5건)**\n{shared_section}\n\n"
            f"**내 파일 (최근 수정순 5건)**\n{recent_section}"
            f"{_DRIVE_DISCLAIMER}"
        ))]}
    except Exception as e:
        if "AUTH_REQUIRED_FOR:" in str(e):
            raise
        print(f"⚠️ DRIVE_LIST 에러: {str(e)}")
        return {"messages": [AIMessage(content=f"⚠️ 드라이브 목록 조회 중 오류: {str(e)}")]}


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
workflow.add_node("EMAIL_SEND_Node", email_send_node)
workflow.add_node("EMAIL_SEARCH_Node", email_search_node)
workflow.add_node("EMAIL_REPLY_Node", email_reply_node)
workflow.add_node("EMAIL_READ_Node", email_read_node)
workflow.add_node("CALENDAR_WRITE_Node", calendar_write_node)
workflow.add_node("CALENDAR_READ_Node", calendar_read_node)
workflow.add_node("CALENDAR_RSVP_Node", calendar_rsvp_node)
workflow.add_node("CALENDAR_UPDATE_Node", calendar_update_node)
workflow.add_node("CALENDAR_DELETE_Node", calendar_delete_node)
workflow.add_node("CALENDAR_FREE_Node", calendar_free_node)
workflow.add_node("TASK_WRITE_Node", task_write_node)
workflow.add_node("TASK_READ_Node", task_read_node)
workflow.add_node("TASK_ACTION_Node", task_action_node)
workflow.add_node("DRIVE_SEARCH_Node", drive_search_node)
workflow.add_node("DRIVE_LIST_Node", drive_list_node)
workflow.add_node("PEOPLE_SEARCH_Node", people_search_node)
workflow.add_node("SHEET_READ_Node", sheet_read_node)
workflow.add_node("SHEET_WRITE_Node", sheet_write_node)
workflow.add_node("DOCS_CREATE_Node", docs_create_node)


# START → Supervisor → 첫 번째 액션 노드 (current_intent 기준 직접 라우팅)
# Dispatcher는 각 액션 노드 완료 후에만 호출 (pending_intents 처리용)
workflow.add_edge(START, "Supervisor")

INTENT_NODE_MAP = {
    "RAG": "RAG_Search_Node",
    "BQ": "BQ_Node",
    "GENERAL": "GENERAL_Node",
    "EMAIL_WRITE": "EMAIL_WRITE_Node",
    "EMAIL_SEND": "EMAIL_SEND_Node",
    "EMAIL_SEARCH": "EMAIL_SEARCH_Node",
    "EMAIL_REPLY": "EMAIL_REPLY_Node",
    "EMAIL_READ": "EMAIL_READ_Node",
    "CALENDAR_WRITE": "CALENDAR_WRITE_Node",
    "CALENDAR_READ": "CALENDAR_READ_Node",
    "CALENDAR_RSVP": "CALENDAR_RSVP_Node",
    "CALENDAR_UPDATE": "CALENDAR_UPDATE_Node",
    "CALENDAR_DELETE": "CALENDAR_DELETE_Node",
    "CALENDAR_FREE": "CALENDAR_FREE_Node",
    "TASK_WRITE": "TASK_WRITE_Node",
    "TASK_READ": "TASK_READ_Node",
    "TASK_ACTION": "TASK_ACTION_Node",
    "DRIVE_SEARCH": "DRIVE_SEARCH_Node",
    "DRIVE_LIST": "DRIVE_LIST_Node",
    "PEOPLE_SEARCH": "PEOPLE_SEARCH_Node",
    "SHEET_READ": "SHEET_READ_Node",
    "SHEET_WRITE": "SHEET_WRITE_Node",
    "DOCS_CREATE": "DOCS_CREATE_Node",
}

def route_after_supervisor(state: AgentState) -> Literal[
    "RAG_Search_Node", "BQ_Node", "GENERAL_Node",
    "EMAIL_WRITE_Node", "EMAIL_SEND_Node", "EMAIL_SEARCH_Node", "EMAIL_REPLY_Node", "EMAIL_READ_Node",
    "CALENDAR_WRITE_Node", "CALENDAR_READ_Node", "CALENDAR_RSVP_Node",
    "CALENDAR_UPDATE_Node", "CALENDAR_DELETE_Node", "CALENDAR_FREE_Node",
    "TASK_WRITE_Node", "TASK_READ_Node", "TASK_ACTION_Node",
    "DRIVE_SEARCH_Node", "DRIVE_LIST_Node", "PEOPLE_SEARCH_Node",
    "SHEET_READ_Node", "SHEET_WRITE_Node", "DOCS_CREATE_Node", "Aggregator_Node"
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
        "EMAIL_SEND_Node": "EMAIL_SEND_Node",
        "EMAIL_SEARCH_Node": "EMAIL_SEARCH_Node",
        "EMAIL_REPLY_Node": "EMAIL_REPLY_Node",
        "EMAIL_READ_Node": "EMAIL_READ_Node",
        "CALENDAR_WRITE_Node": "CALENDAR_WRITE_Node",
        "CALENDAR_READ_Node": "CALENDAR_READ_Node",
        "CALENDAR_RSVP_Node": "CALENDAR_RSVP_Node",
        "CALENDAR_UPDATE_Node": "CALENDAR_UPDATE_Node",
        "CALENDAR_DELETE_Node": "CALENDAR_DELETE_Node",
        "CALENDAR_FREE_Node": "CALENDAR_FREE_Node",
        "TASK_WRITE_Node": "TASK_WRITE_Node",
        "TASK_READ_Node": "TASK_READ_Node",
        "TASK_ACTION_Node": "TASK_ACTION_Node",
        "DRIVE_SEARCH_Node": "DRIVE_SEARCH_Node",
        "DRIVE_LIST_Node": "DRIVE_LIST_Node",
        "PEOPLE_SEARCH_Node": "PEOPLE_SEARCH_Node",
        "SHEET_READ_Node": "SHEET_READ_Node",
        "SHEET_WRITE_Node": "SHEET_WRITE_Node",
        "DOCS_CREATE_Node": "DOCS_CREATE_Node",
        "Aggregator_Node": "Aggregator_Node",
    }
)

# 액션 노드 완료 후 Dispatcher: pending_intents가 남아있으면 다음 노드로, 없으면 Aggregator로
def route_after_dispatcher(state: AgentState) -> Literal[
    "RAG_Search_Node", "BQ_Node", "GENERAL_Node",
    "EMAIL_WRITE_Node", "EMAIL_SEND_Node", "EMAIL_SEARCH_Node", "EMAIL_REPLY_Node", "EMAIL_READ_Node",
    "CALENDAR_WRITE_Node", "CALENDAR_READ_Node", "CALENDAR_RSVP_Node",
    "CALENDAR_UPDATE_Node", "CALENDAR_DELETE_Node", "CALENDAR_FREE_Node",
    "TASK_WRITE_Node", "TASK_READ_Node", "TASK_ACTION_Node",
    "DRIVE_SEARCH_Node", "DRIVE_LIST_Node", "PEOPLE_SEARCH_Node",
    "SHEET_READ_Node", "SHEET_WRITE_Node", "DOCS_CREATE_Node", "Aggregator_Node"
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
        "EMAIL_SEND_Node": "EMAIL_SEND_Node",
        "EMAIL_SEARCH_Node": "EMAIL_SEARCH_Node",
        "EMAIL_REPLY_Node": "EMAIL_REPLY_Node",
        "EMAIL_READ_Node": "EMAIL_READ_Node",
        "CALENDAR_WRITE_Node": "CALENDAR_WRITE_Node",
        "CALENDAR_READ_Node": "CALENDAR_READ_Node",
        "CALENDAR_RSVP_Node": "CALENDAR_RSVP_Node",
        "CALENDAR_UPDATE_Node": "CALENDAR_UPDATE_Node",
        "CALENDAR_DELETE_Node": "CALENDAR_DELETE_Node",
        "CALENDAR_FREE_Node": "CALENDAR_FREE_Node",
        "TASK_WRITE_Node": "TASK_WRITE_Node",
        "TASK_READ_Node": "TASK_READ_Node",
        "TASK_ACTION_Node": "TASK_ACTION_Node",
        "DRIVE_SEARCH_Node": "DRIVE_SEARCH_Node",
        "DRIVE_LIST_Node": "DRIVE_LIST_Node",
        "PEOPLE_SEARCH_Node": "PEOPLE_SEARCH_Node",
        "SHEET_READ_Node": "SHEET_READ_Node",
        "SHEET_WRITE_Node": "SHEET_WRITE_Node",
        "DOCS_CREATE_Node": "DOCS_CREATE_Node",
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
workflow.add_edge("EMAIL_SEND_Node", "Dispatcher")
workflow.add_edge("EMAIL_SEARCH_Node", "Dispatcher")
workflow.add_edge("EMAIL_REPLY_Node", "Dispatcher")
workflow.add_edge("CALENDAR_WRITE_Node", "Dispatcher")
workflow.add_edge("CALENDAR_READ_Node", "Dispatcher")
workflow.add_edge("CALENDAR_RSVP_Node", "Dispatcher")
workflow.add_edge("CALENDAR_UPDATE_Node", "Dispatcher")
workflow.add_edge("CALENDAR_DELETE_Node", "Dispatcher")
workflow.add_edge("CALENDAR_FREE_Node", "Dispatcher")
workflow.add_edge("TASK_WRITE_Node", "Dispatcher")
workflow.add_edge("TASK_READ_Node", "Dispatcher")
workflow.add_edge("TASK_ACTION_Node", "Dispatcher")
workflow.add_edge("DRIVE_SEARCH_Node", "Dispatcher")
workflow.add_edge("DRIVE_LIST_Node", "Dispatcher")
workflow.add_edge("PEOPLE_SEARCH_Node", "Dispatcher")
workflow.add_edge("SHEET_READ_Node", "Dispatcher")
workflow.add_edge("SHEET_WRITE_Node", "Dispatcher")
workflow.add_edge("DOCS_CREATE_Node", "Dispatcher")

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
            response = ai_client.models.generate_content(model=LITE_MODEL, contents=inference_prompt)
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