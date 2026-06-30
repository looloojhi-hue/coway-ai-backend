import os
from google import genai  # 💡 최신 2026년형 GenAI SDK 로드
from google.cloud import bigquery
from google.cloud.bigquery import QueryJobConfig, ArrayQueryParameter, ScalarQueryParameter

# =====================================================================
# ⚙️ GCP 및 환경 변수 설정 (신규 프로젝트 완전 무결성 락인)
# =====================================================================
PROJECT_ID = "gcp-cw-ai-chatbot"
DATASET_ID = "hrga_rag_data"
TABLE_ID = "knowledge_master"

bq_client = bigquery.Client(project=PROJECT_ID)
# 💡 [리전 실측 복구] 프로님의 필드 테스트에서 완벽히 검증된 "us" 멀티 리전 엔드포인트 고정!
ai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="us")
LITE_MODEL = "gemini-3.1-flash-lite"

# =====================================================================
# 🧠 1단계: 사용자 질문을 벡터로 변환 (Embedding)
# =====================================================================
def get_query_embedding(text: str) -> list:
    """사용자의 질문을 문서 임베딩과 동일한 차원의 숫자 배열로 변환합니다."""
    from google.genai import types
    
    response = ai_client.models.embed_content(
        model="gemini-embedding-2",  
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=3072)  # 🎯 3072 차원 강제 일치!
    )
    
    print(f"📏 현재 파이썬이 만든 질문 벡터 차원 (숫자 B): {len(response.embeddings[0].values)}")
    return response.embeddings[0].values

# =====================================================================
# 🚀 2단계: 하이브리드 RAG 검색 (Vector + Keyword + ACL)
# =====================================================================
_QUERY_STOPWORDS = {
    '알려줘', '알려주세요', '알려줄', '설명해줘', '설명해주세요', '정리해줘', '정리해주세요',
    '뭐야', '뭔가요', '무엇인가요', '어떻게', '어떤', '어디서', '언제', '왜', '이게', '그게',
    '해줘', '해주세요', '좀', '부탁드려요', '도와줘', '있어', '있나요', '있어요',
}

_BROAD_QUERY_PATTERNS = [
    '알려줘', '알려주세요', '설명해줘', '설명해주세요', '정리해줘', '정리해주세요',
    '전체', '종류', '목록', '리스트', '항목', '어떤게 있', '어떤 것들',
]

def is_broad_query(query: str) -> bool:
    """포괄적인 정보 요청 쿼리 여부를 판별합니다."""
    return any(pattern in query for pattern in _BROAD_QUERY_PATTERNS)

def _expand_query(user_query: str) -> str | None:
    """광범위 쿼리에 대해 LLM으로 보조 검색 쿼리를 동적 생성합니다. 실패 시 하드코딩 폴백."""
    _fallback_pairs = [
        ('복리후생 제도', '코웨이 복리후생 규정 전체'),
        ('복리후생 규정', '코웨이 복리후생 제도 안내'),
        ('복리후생', '복지포인트 경조사 휴가 지원'),
        ('출장 규정', '출장비 여비 지급 기준'),
        ('휴가 제도', '연차 특별휴가 출산휴가'),
    ]
    try:
        from google.genai import types as _types
        import json as _json
        prompt = f"""다음 질문에 대해 사내 규정 문서 검색에 사용할 보조 검색어를 1개 생성하세요.
원문 질문과 다른 관점의 유사어/확장어를 사용해 검색 커버리지를 넓히는 것이 목표입니다.
JSON 형식으로만 답하세요: {{"expansion": "검색어"}}

질문: {user_query}"""
        response = ai_client.models.generate_content(
            model=LITE_MODEL,
            contents=prompt,
            config=_types.GenerateContentConfig(response_mime_type="application/json"),
        )
        result = _json.loads(response.text)
        expansion = result.get("expansion", "").strip()
        if expansion and expansion != user_query:
            print(f"🔀 [RAG Expand] LLM 확장 쿼리: '{expansion}'")
            return expansion
    except Exception as e:
        print(f"⚠️ [RAG Expand] LLM 확장 실패, 폴백 사용: {e}")

    q = user_query.replace('알려줘', '').replace('설명해줘', '').replace('정리해줘', '').strip()
    for keyword, expansion in _fallback_pairs:
        if keyword in q:
            return expansion
    return None

def hybrid_search_bq(user_query: str, user_email: str, top_k: int = 6) -> tuple:
    """빅쿼리 내장 머신러닝 기능을 활용해 권한이 통과된 문서 중 최고 적합도를 찾습니다."""
    query_vector = get_query_embedding(user_query)

    # 핵심 키워드 다중 추출: 불용어 제거 후 최대 4개 단어를 OR 패턴으로 결합
    clean_query = ''.join(e for e in user_query if e.isalnum() or e.isspace())
    words = [w for w in clean_query.split() if w not in _QUERY_STOPWORDS and len(w) > 1]
    core_keyword = '|'.join(words[:4]) if words else (clean_query.strip() or user_query)
    
    # 👑 아키텍트의 예술: 빅쿼리 하이브리드 SQL (문법 규정 준수 버전)
    sql = f"""
    WITH 
    -- 1. 빅쿼리 제약 조건에 맞춰 VECTOR_SEARCH 첫 번째 인자에 순수 서브쿼리를 직접 주입
    vector_results AS (
        SELECT query.query_vector, base.*, distance
        FROM VECTOR_SEARCH(
            (
                SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`
                WHERE allowed_groups = 'employee_all@coway.com' 
                   OR allowed_groups LIKE CONCAT('%', @user_email, '%')
            ),
            'embedding',
            (SELECT @query_vector AS query_vector),
            top_k => 20,
            distance_type => 'COSINE'
        )
    )
    -- 2. 하이브리드 리랭킹 (의미 점수 + 정확한 키워드가 있으면 가산점 부여)
    SELECT 
        doc_name, 
        doc_url, 
        content, 
        last_modified,
        dept_code,
        distance,
        # 핵심 키워드가 마크다운 본문에 있으면 0.15점 보너스 부여
        IF(REGEXP_CONTAINS(content, @core_keyword), 0.15, 0) AS keyword_boost,
        (1.0 - distance) + IF(REGEXP_CONTAINS(content, @core_keyword), 0.15, 0) AS hybrid_score
    FROM vector_results
    ORDER BY hybrid_score DESC
    LIMIT @top_k
    """

    job_config = QueryJobConfig(
        query_parameters=[
            ArrayQueryParameter("query_vector", "FLOAT64", query_vector),
            ScalarQueryParameter("user_email", "STRING", user_email),
            ScalarQueryParameter("core_keyword", "STRING", core_keyword),
            ScalarQueryParameter("top_k", "INT64", top_k),
        ]
    )

    results = bq_client.query(sql, job_config=job_config).result()

    # =====================================================================
    # 📦 3단계: LLM에게 먹여줄 '지식 캡슐(Context)' 조립
    # =====================================================================
    retrieved_docs = []
    seen_urls = set()
    top_dept_code = "분류 불가"

    for i, row in enumerate(results):
        if i == 0:
            top_dept_code = row.dept_code if row.dept_code else "분류 불가"
        seen_urls.add(row.doc_url)
        doc_info = (
            f"[문서명]: {row.doc_name}\n"
            f"[담당부서]: {row.dept_code}\n"
            f"[최종수정일]: {row.last_modified}\n"
            f"[문서URL]: {row.doc_url}\n"
            f"[상세 내용]:\n{row.content}\n"
        )
        retrieved_docs.append(doc_info)
        print(f"🎯 [RAG 1차] {row.doc_name} (Hybrid: {row.hybrid_score:.4f})")

    # 쿼리 확장: 광범위 질문은 보조 쿼리로 2차 검색해 누락 문서 보완
    expanded_query = _expand_query(user_query)
    if expanded_query and is_broad_query(user_query):
        print(f"🔄 [RAG 쿼리확장] 2차 검색: '{expanded_query}'")
        try:
            exp_vector = get_query_embedding(expanded_query)
            exp_clean = ''.join(e for e in expanded_query if e.isalnum() or e.isspace())
            exp_words = [w for w in exp_clean.split() if w not in _QUERY_STOPWORDS and len(w) > 1]
            exp_keyword = '|'.join(exp_words[:4]) if exp_words else expanded_query
            exp_top_k = max(top_k, 6)
            exp_job = QueryJobConfig(
                query_parameters=[
                    ArrayQueryParameter("query_vector", "FLOAT64", exp_vector),
                    ScalarQueryParameter("user_email", "STRING", user_email),
                    ScalarQueryParameter("core_keyword", "STRING", exp_keyword),
                    ScalarQueryParameter("top_k", "INT64", exp_top_k),
                ]
            )
            exp_results = bq_client.query(sql, job_config=exp_job).result()
            added = 0
            for row in exp_results:
                if row.doc_url not in seen_urls:
                    seen_urls.add(row.doc_url)
                    doc_info = (
                        f"[문서명]: {row.doc_name}\n"
                        f"[담당부서]: {row.dept_code}\n"
                        f"[최종수정일]: {row.last_modified}\n"
                        f"[문서URL]: {row.doc_url}\n"
                        f"[상세 내용]:\n{row.content}\n"
                    )
                    retrieved_docs.append(doc_info)
                    added += 1
                    print(f"🎯 [RAG 2차 보완] {row.doc_name} (Hybrid: {row.hybrid_score:.4f})")
            print(f"📌 [쿼리확장] 2차에서 {added}개 추가 문서 보완")
        except Exception as e:
            print(f"⚠️ [쿼리확장] 2차 검색 실패, 1차 결과만 사용: {e}")

    context_text = "\n\n---\n\n".join(retrieved_docs)
    return context_text, top_dept_code

# =====================================================================
# 🌐 LangGraph 연동용 에이전트 노드 함수
# =====================================================================
def rag_search_node(state: dict) -> dict:
    """LangGraph에서 RAG 검색이 필요할 때 호출되는 진입점(Node)입니다."""
    # 🧱 [무결성 패치] 꼬여있던 단선 변수 구조를 타파하고 마스터 장부(AgentState)와 인풋 라인 100% 동기화
    user_query = state["messages"][-1].content
    user_email = state.get("user_info", {}).get("email", "employee_all@coway.com")
    
    print(f"\n🔍 [RAG 에이전트 가동] 질의: '{user_query}' / 권한: '{user_email}'")
    
    # 💡 [보안/동기화 튜닝 완료] 리턴되는 두 개의 보따리를 완벽하게 분리 패키징 수선!
    context_text, top_dept_code = hybrid_search_bq(user_query, user_email)
    
    if not context_text:
        context_text = "시스템에 등록된 사내 규정이나 관련 문서를 찾을 수 없거나, 해당 문서를 열람할 권한이 없습니다."
        print("⚠️ 관련 문서를 찾지 못했습니다.")
        
    # 🧱 [무결성 패치] 사출방 키 이름을 마스터 장부 규격인 'retrieved_docs'로 완벽 정렬 매핑
    return {"retrieved_docs": context_text, "top_dept_code": top_dept_code}

# =====================================================================
# 🧪 (단독 테스트용) 로컬 슛팅 로직
# =====================================================================
if __name__ == "__main__":
    from langchain_core.messages import HumanMessage
    
    # 💡 [로컬 샌드박스 튜닝 완료] 실제 장부 스키마와 완벽 동일 매핑하여 데이터 꼬임 현상 원천 차단
    test_state = {
        "messages": [HumanMessage(content="총무팀 출장 규정 알려줘")],
        "user_info": {"email": "looloojhi@coway.com"}
    }
    
    result_state = rag_search_node(test_state)
    print("\n[LLM에게 전달될 최종 Context 조각]")
    print(result_state["retrieved_docs"])
    print(f"🎯 추정 탑 부서코드: {result_state.get('top_dept_code')}")