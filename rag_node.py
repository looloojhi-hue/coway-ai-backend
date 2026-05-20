# rag_node.py
import os
from google import genai  # 💡 embed_and_load.py와 동일한 최신 2026년형 GenAI SDK 로드
from google.cloud import bigquery
from google.cloud.bigquery import QueryJobConfig, ArrayQueryParameter, ScalarQueryParameter

# =====================================================================
# ⚙️ GCP 및 환경 변수 설정
# =====================================================================
PROJECT_ID = "hr-division-ai-rpa"
DATASET_ID = "hrga_rag_data"
TABLE_ID = "knowledge_master"

bq_client = bigquery.Client(project=PROJECT_ID)
# 💡 [핵심 수정] gemini-embedding-2 모델이 배포 완료된 메인 리전(us-central1)으로 엔진 위치를 변경합니다.
ai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="us")

# =====================================================================
# 🧠 1단계: 사용자 질문을 벡터로 변환 (Embedding)
# =====================================================================
def get_query_embedding(text: str) -> list:
    """사용자의 질문을 문서 임베딩과 동일한 차원의 숫자 배열로 변환합니다."""
    # 💡 수석님의 날카로운 지적으로 구형 모델 대신 최신 gemini-embedding-2 모델로 동기화 완료!
    from google.genai import types
    
    response = ai_client.models.embed_content(
        model="gemini-embedding-2",  
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=3072)  # 🎯 3072 차원 강제 일치!
    )
    
    # 💡 [진단 전용 프린트] 이제 rag_node.py가 실행될 때 정상적으로 출력됩니다!
    print(f"📏 현재 파이썬이 만든 질문 벡터 차원 (숫자 B): {len(response.embeddings[0].values)}")
    
    return response.embeddings[0].values

# =====================================================================
# 🚀 2단계: 하이브리드 RAG 검색 (Vector + Keyword + ACL)
# =====================================================================
def hybrid_search_bq(user_query: str, user_email: str, top_k: int = 3) -> str:
    """빅쿼리 내장 머신러닝 기능을 활용해 권한이 통과된 문서 중 최고 적합도를 찾습니다."""
    query_vector = get_query_embedding(user_query)
    
    # 💡 키워드 가점을 위한 불용어 제거 (간단한 핵심 명사 추출)
    clean_query = ''.join(e for e in user_query if e.isalnum() or e.isspace())
    core_keyword = clean_query.split()[0] if clean_query else user_query
    
    # 👑 아키텍트의 예술: 빅쿼리 하이브리드 SQL (문법 규정 준수 버전)
    sql = f"""
    WITH 
    -- 1. 빅쿼리 제약 조건에 맞춰 VECTOR_SEARCH 첫 번째 인자에 순수 서브쿼리(SELECT/WHERE)를 직접 주입
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
            top_k => 10,
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

    # 💡 QueryJobConfig로 올바르게 인스턴스를 생성합니다.
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
    for row in results:
        # LLM이 읽기 좋고, 나중에 프론트엔드 출처(Citation)로 쓰기 좋은 구조로 포맷팅
        doc_info = (
            f"[문서명]: {row.doc_name}\n"
            f"[담당부서]: {row.dept_code}\n"
            f"[최종수정일]: {row.last_modified}\n"
            f"[문서URL]: {row.doc_url}\n"
            f"[상세 내용]:\n{row.content}\n"
        )
        retrieved_docs.append(doc_info)
        print(f"🎯 [RAG 검색 성공] {row.doc_name} (Hybrid Score: {row.hybrid_score:.4f} / Distance: {row.distance:.4f})")
        
    return "\n\n---\n\n".join(retrieved_docs)

# =====================================================================
# 🌐 LangGraph 연동용 에이전트 노드 함수
# =====================================================================
def rag_search_node(state: dict):
    """LangGraph에서 RAG 검색이 필요할 때 호출되는 진입점(Node)입니다."""
    user_query = state.get("lastQ", "")
    user_email = state.get("user_email", "employee_all@coway.com")
    
    print(f"\n🔍 [RAG 에이전트 가동] 질의: '{user_query}' / 권한: '{user_email}'")
    
    # 빅쿼리 하이브리드 검색 실행
    context_text = hybrid_search_bq(user_query, user_email)
    
    if not context_text:
        context_text = "시스템에 등록된 사내 규정이나 관련 문서를 찾을 수 없거나, 해당 문서를 열람할 권한이 없습니다."
        print("⚠️ 관련 문서를 찾지 못했습니다.")
        
    # LangGraph State 업데이트: context 키에 검색된 지식 뭉치를 덮어씌움
    return {"context": context_text}

# =====================================================================
# 🧪 (단독 테스트용) 로컬 슛팅 로직
# =====================================================================
if __name__ == "__main__":
    # 이 파일을 단독으로 실행했을 때 잘 작동하는지 샌드박스 테스트
    test_state = {
        "lastQ": "총무팀 출장 규정 알려줘",
        "user_email": "looloojhi@coway.com"
    }
    
    result_state = rag_search_node(test_state)
    print("\n[LLM에게 전달될 최종 Context 조각]")
    print(result_state["context"])