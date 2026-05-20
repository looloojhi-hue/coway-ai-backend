# embed_and_load.py
import os
import uuid
from google.cloud import bigquery
from langchain_text_splitters import MarkdownTextSplitter
from google import genai
from google.genai import types

# 1. GCP 및 환경 변수 초기화
PROJECT_ID = "hr-division-ai-rpa"
VERTEX_LOCATION = "us"           
BQ_LOCATION = "asia-northeast3"      
DATASET_ID = "hrga_rag_data"
TABLE_ID = "knowledge_master"

client = genai.Client(vertexai=True, project=PROJECT_ID, location=VERTEX_LOCATION)
bq_client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

def create_bq_vector_table_if_not_exists():
    """BigQuery에 벡터, ACL, 그리고 드라이브 동기화용 메타데이터를 저장할 마스터 테이블을 생성합니다."""
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    
    schema = [
        bigquery.SchemaField("chunk_id", "STRING", mode="REQUIRED"),
        # 🔒 [초자동화 추가 1] 구글 드라이브 고유 파일 ID (수정/삭제 추적용 주민번호)
        bigquery.SchemaField("file_id", "STRING", mode="REQUIRED"), 
        bigquery.SchemaField("doc_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("doc_url", "STRING", mode="NULLABLE"), 
        # 🕒 [초자동화 추가 2] 파일 최종 수정 일시 (업데이트 감지용 타임스탬프)
        bigquery.SchemaField("last_modified", "TIMESTAMP", mode="REQUIRED"), 
        bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"), 
        bigquery.SchemaField("dept_code", "STRING", mode="NULLABLE"),  
        bigquery.SchemaField("allowed_groups", "STRING", mode="NULLABLE") 
    ]
    
    try:
        bq_client.get_table(table_ref)
        print(f"📦 [{TABLE_ID}] 테이블이 이미 존재합니다. 적재를 시작합니다.")
    except Exception:
        print(f"✨ [{TABLE_ID}] 테이블이 존재하지 않아 새로 생성합니다...")
        table = bigquery.Table(table_ref, schema=schema)
        bq_client.create_table(table)
        print("✅ 2026년형 초자동화 벡터 마스터 테이블 생성 완료!")

def generate_gemini_embeddings(text_chunks):
    """최신 gemini-embedding-2 모델을 사용하여 벡터를 추출합니다."""
    print("🔮 최신 Gemini Embedding 2 엔진으로 벡터 주소 추출 중...")
    all_embeddings = []
    
    for chunk in text_chunks:
        # 💡 [정밀 교정] 빅쿼리 마스터 테이블 규격에 맞게 3072 차원으로 강제 픽스합니다.
        response = client.models.embed_content(
            model="gemini-embedding-2",
            contents=[chunk],
            config=types.EmbedContentConfig(output_dimensionality=3072)  # 🎯 차원 전쟁 종식!
        )
        all_embeddings.append(response.embeddings[0].values)
        
    return all_embeddings

def chunk_and_load_to_gcp(file_id, doc_name, doc_url, last_modified, markdown_text, dept_code="ALL", allowed_groups="ALL"):
    """마크다운 문서를 쪼개고 동기화 메타데이터와 함께 빅쿼리에 적재합니다."""
    # 💡 [핵심 수정] 이미 테이블이 완벽하게 생성되어 있으므로, 
    # 권한 에러를 일으키는 테이블 체크/생성 함수를 주석 처리하여 건너뜁니다!
    #create_bq_vector_table_if_not_exists()
    
    print("✂️ 마크다운 문맥 기반 청킹(Chunking) 진행 중...")
    text_splitter = MarkdownTextSplitter(chunk_size=800, chunk_overlap=100)
    chunks = text_splitter.split_text(markdown_text)
    print(f"📝 총 {len(chunks)}개의 지식 조각(Chunk)이 생성되었습니다.")
    
    if not chunks:
        return

    vectors = generate_gemini_embeddings(chunks)
    
    rows_to_insert = []
    for i, chunk in enumerate(chunks):
        rows_to_insert.append({
            "chunk_id": str(uuid.uuid4()),
            "file_id": file_id,               # 🔒 드라이브 고유 ID 매핑
            "doc_name": doc_name,
            "doc_url": doc_url,  
            "last_modified": last_modified,   # 🕒 수정 시간 매핑
            "content": chunk,
            "embedding": vectors[i],
            "dept_code": dept_code,   
            "allowed_groups": allowed_groups 
        })
        
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    errors = bq_client.insert_rows_json(table_ref, rows_to_insert)
    
    if not errors:
        print(f"✅ [{doc_name}] 빅쿼리 적재 성공!")
    else:
        print(f"❌ 적재 에러 발생: {errors}")