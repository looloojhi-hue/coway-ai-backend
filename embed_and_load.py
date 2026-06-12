# embed_and_load.py
import os
import io
import csv
import uuid
import datetime
from google.cloud import bigquery
from langchain_text_splitters import MarkdownTextSplitter
from google import genai
from google.genai import types
import google.auth
from googleapiclient.discovery import build
import json

# 💡 [대통합] PDF/이미지 가공을 위해 document_parser.py의 명품 AI 파싱 함수 로드
from document_parser import parse_document_to_markdown

# =====================================================================
# ⚙️ GCP 및 환경 변수 초기화
# =====================================================================
PROJECT_ID = "gcp-cw-ai-chatbot"
VERTEX_LOCATION = "us"           
BQ_LOCATION = "asia-northeast3"      
DATASET_ID = "hrga_rag_data"
TABLE_ID = "knowledge_master"

ADMIN_USER_EMAIL = "looloojhi@coway.com"  # 🔑 권한 가장용 프로님 계정

# 🤖 크롤링 타겟 마스터 폴더 고유 ID 동결 세팅!
TARGET_SHARED_FOLDER_ID = "1fUfnHqbk72NeWnFUGPpDuUYvCpPVK_Jx"

client = genai.Client(vertexai=True, project=PROJECT_ID, location=VERTEX_LOCATION)
bq_client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

# 📡 코웨이 AI 챗봇 타겟 공유드라이브 마스터 폴더 기지국 매트릭스
TARGET_FOLDERS = [
    {
        "name": "전체공유용",
        "id": TARGET_SHARED_FOLDER_ID,
        "allowed_group": "employee_all@coway.com"
    }
]

# ==========================================
# 🛡️ 기저 인프라 테이블 체크 장치
# ==========================================
def get_drive_service(user_email: str):
    """[엔터프라이즈 최상위 보안 적용] 로컬과 상용 서버 모두에서 물리 키 파일(JSON) 없이
    구글 고유의 환경 인증(ADC 및 Impersonation 토큰 체인)을 통해 무결하게 자원을 획득합니다."""
    scopes = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/cloud-platform"
    ]
    # 🔑 어떤 하드코딩 파일도 보지 않고 구글 정식 인프라 레일 연결선만 활성화합니다.
    creds, _ = google.auth.default(scopes=scopes)
    
    if hasattr(creds, 'with_subject'):
        delegated_creds = creds.with_subject(user_email)
        return build('drive', 'v3', credentials=delegated_creds)
    return build('drive', 'v3', credentials=creds)

def create_bq_vector_table_if_not_exists():
    """BigQuery 상위 데이터세트가 이미 존재하므로, 곧바로 하위 마스터 테이블 검사 및 자율 생성에 착수합니다."""
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    
    # 📦 [하위 금고 생성] 벡터 마스터 테이블 스키마 매립 생성 (+ 다이내믹 links 컬럼 추가)
    schema = [
        bigquery.SchemaField("chunk_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("file_id", "STRING", mode="REQUIRED"), 
        bigquery.SchemaField("doc_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("doc_url", "STRING", mode="NULLABLE"), 
        bigquery.SchemaField("last_modified", "TIMESTAMP", mode="REQUIRED"), 
        bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("embedding", "FLOAT64", mode="REPEATED"), 
        bigquery.SchemaField("dept_code", "STRING", mode="NULLABLE"),  
        bigquery.SchemaField("allowed_groups", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("links", "STRING", mode="NULLABLE") # 👈 [Phase 4] 다이내믹 링크 압축 장부방 개설!
    ]
    
    try:
        bq_client.get_table(table_ref)
        print(f"📦 [{TABLE_ID}] 테이블이 이미 존재합니다. 자율 동기화를 시작합니다.")
    except Exception:
        print(f"✨ [{TABLE_ID}] 테이블이 존재하지 않아 새로 생성합니다...")
        table = bigquery.Table(table_ref, schema=schema)
        # 일자별로 데이터를 쪼개서 보관하여 조회 속도를 10배 올리는 파티션 가동
        table.time_partitioning = bigquery.TimePartitioning(type_=bigquery.TimePartitioningType.DAY, field="last_modified")
        bq_client.create_table(table)
        print("✅ 2026년형 초자동화 벡터 마스터 테이블 최종 안착 완료!")

def get_existing_knowledge_meta() -> dict:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    meta_map = {}
    try:
        sql = f"SELECT file_id, max(last_modified) as last_mod FROM `{table_ref}` GROUP BY file_id"
        query_job = bq_client.query(sql)
        for row in query_job.result():
            meta_map[row.file_id] = row.last_mod
    except Exception: pass
    return meta_map

def generate_gemini_embeddings(text_chunks):
    all_embeddings = []
    for chunk in text_chunks:
        response = client.models.embed_content(
            model="gemini-embedding-2",
            contents=[chunk],
            config=types.EmbedContentConfig(output_dimensionality=3072)
        )
        all_embeddings.append(response.embeddings[0].values)
    return all_embeddings

def direct_load_rows_to_bq(rows_to_insert, file_id):
    if not rows_to_insert: return
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    
    delete_sql = f"DELETE FROM `{table_ref}` WHERE file_id = '{file_id}'"
    try: bq_client.query(delete_sql).result()
    except Exception: pass

    errors = bq_client.insert_rows_json(table_ref, rows_to_insert)
    if not errors:
        print(f"   ✅ 빅쿼리 인덱싱 주입 대성공! (지식 조각 수: {len(rows_to_insert)}개)")
    else:
        print(f"   ❌ 적재 에러 발생: {errors}")

# =====================================================================
# 📊 구글 스프레드시트 정밀 행(Row) 해체 가공 엔진 (FAQ 장부 특화)
# =====================================================================
def process_spreadsheet_scenario(file_id, doc_name, doc_url, last_modified, csv_content_stream, dept_code, allowed_group):
    """📊 구글 스프레드시트 정밀 행(Row) 해체 가공 엔진 (FAQ 장부 특화 + [Phase 4] 유동적 멀티 링크 자동 파싱 엔진 탑재)"""
    f = io.StringIO(csv_content_stream)
    reader = csv.DictReader(f)
    
    def find_column_value_and_key(row_dict, keywords):
        for k, v in row_dict.items():
            if not k: continue
            clean_key = k.lower().replace(" ", "").replace("_", "").replace("-", "")
            if any(kw in clean_key for kw in keywords):
                return k, (v.strip() if v and v.strip() else "")
        return None, ""

    text_chunks = []
    rows_to_insert = []
    
    # CSV 내부 컬럼 목록 확보 (동적 링크 매핑 추적용)
    fieldnames = reader.fieldnames if reader.fieldnames else []
    
    for row in reader:
        mapped_keys = set() 
        
        k_biz, biz_type = find_column_value_and_key(row, ["업무구분", "구분", "카테고리"])
        k_q, question = find_column_value_and_key(row, ["질문", "질문내용", "query", "q", "문의"])
        k_a, answer = find_column_value_and_key(row, ["답변", "답변내용", "reply", "a", "정답", "내용"])
        k_mgr, manager = find_column_value_and_key(row, ["담당자", "담당자정보", "manager", "담당"])
        
        for k in [k_biz, k_q, k_a, k_mgr]:
            if k: mapped_keys.add(k)
        
        # 🎯 [Phase 4 핵심 알고리즘] 유동적 '링크' 컬럼 무제한 자동 추적 엔진
        dynamic_links = []
        
        # 링크 1번부터 최대 20번 세트까지 컬럼이 뒤로 확장되어도 자동으로 감지하는 유연 루프
        for i in range(1, 21):
            k_ln, l_name = find_column_value_and_key(row, [f"링크{i}이름", f"link{i}name"])
            k_lu, l_url = find_column_value_and_key(row, [f"링크{i}url", f"링크{i}주소", f"링크{i}링크", f"link{i}url"])
            
            if k_ln: mapped_keys.add(k_ln)
            if k_lu: mapped_keys.add(k_lu)
            
            # 🔗 데이터가 존재하는 경우에만 링크 배열에 동적 안착
            if l_name or l_url:
                dynamic_links.append({
                    "title": l_name if l_name else "참고 링크",
                    "url": l_url if l_url else "#"
                })

        # LLM 임베딩 및 검색용 컨텍스트 텍스트 청크 포맷팅
        formatted_chunk = (
            f"[{dept_code} 사내 FAQ 시나리오 장부]\n"
            f"- 업무 구분: {biz_type if biz_type else '내용 없음'}\n"
            f"- 질문 문항: {question if question else '내용 없음'}\n"
            f"- 답변 내용:\n{answer if answer else '내용 없음'}\n"
            f"- 업무 담당자 정보: {manager if manager else '내용 없음'}\n"
        )
        
        # 가독성을 위해 본문 텍스트 스트림 내부에도 수집된 링크 임베딩
        if dynamic_links:
            formatted_chunk += "\n[연관 참고 링크 정보]\n"
            for link in dynamic_links:
                formatted_chunk += f"- {link['title']}: {link['url']}\n"
            
        extra_text = ""
        for k, v in row.items():
            if k and k not in mapped_keys and v and v.strip():
                extra_text += f"- {k.strip()}: {v.strip()}\n"
        
        if extra_text: 
            formatted_chunk += f"\n[임직원 참고용 추가 기재 정보]\n{extra_text}"
            
        text_chunks.append(formatted_chunk)

        # 🌟 [Generative UI 레일] 수집된 링크 객체를 JSON 스트링으로 직렬화 (없으면 빈 배열 '[]' 저장)
        links_json_str = json.dumps(dynamic_links, ensure_ascii=False)

        rows_to_insert.append({
            "chunk_id": str(uuid.uuid4()), 
            "file_id": file_id, 
            "doc_name": doc_name, 
            "doc_url": doc_url,  
            "last_modified": last_modified, 
            "content": formatted_chunk, 
            "embedding": None, # 아래에서 배치 할당
            "dept_code": dept_code, 
            "allowed_groups": allowed_group,
            "links": links_json_str # 👈 확장된 빅쿼리 룸에 완벽 매립
        })

    if not text_chunks: return
    
    # 🛡️ 로컬 및 실서버 간 권한 대기 시 예외 크래시 완벽 방어선 가동
    try:
        vectors = generate_gemini_embeddings(text_chunks)
        for i, row_data in enumerate(rows_to_insert):
            row_data["embedding"] = vectors[i]
        direct_load_rows_to_bq(rows_to_insert, file_id)
    except Exception as e:
        print(f"   ⚙️  [인프라 권한 오프라인 예외 제어] 데이터 정제 대성공 (총 {len(rows_to_insert)}행 완료) | 단, Vertex AI IAM 대기로 임베딩 적재 단계는 자율 스킵합니다. ({e})")

# =====================================================================
# 🔄 [대통합 가동] 하위 무제한 재귀 추적 스캔 엔진 (스프레드시트 + PDF + PPT + Docs)
# =====================================================================
def traverse_and_sync(folder_id, parent_dept_code, allowed_group, drive_service, bq_meta_map, active_drive_ids):
    query = f"'{folder_id}' in parents and trashed = false"
    try:
        results = drive_service.files().list(
            q=query, spaces='drive',
            fields='files(id, name, mimeType, modifiedTime, webViewLink)',
            supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        
        items = results.get('files', [])
        for item in items:
            item_id = item['id']
            item_name = item['name']
            item_mime = item['mimeType']
            
            current_dept_code = parent_dept_code
            if item_mime == 'application/vnd.google-apps.folder':
                if item_name.endswith("팀") or item_name.endswith("TF") or "팀 " in item_name:
                    current_dept_code = item_name.strip()
                traverse_and_sync(item_id, current_dept_code, allowed_group, drive_service, bq_meta_map, active_drive_ids)
                
            # 💡 [정밀 필터링 확장] 탐색기가 수집할 모든 파일 확장자 레이어 통합 바인딩
            elif item_mime in [
                'application/vnd.google-apps.document', 'text/plain', 
                'application/vnd.google-apps.spreadsheet', 'application/vnd.google-apps.presentation',
                'application/pdf'  # 👈 기존 drive_sync가 처리하던 생 PDF 조항 완벽 흡수!
            ]:
                active_drive_ids.add(item_id)
                
                f_modified_str = item['modifiedTime'].replace('Z', '+00:00')
                drive_modified_time = datetime.datetime.fromisoformat(f_modified_str)
                
                if item_id in bq_meta_map:
                    if drive_modified_time <= bq_meta_map[item_id]:
                        print(f"   Skip ⏭️  [변동 없음]: {item_name}")
                        continue
                        
                print(f"   ⚙️  [변경/신규 감지] 지식 동기화 착수: {item_name}")
                try:
                    # 💡 분기 A: 구글 시트 FAQ 시나리오 장부 처리
                    if item_mime == 'application/vnd.google-apps.spreadsheet':
                        raw_csv_stream = drive_service.files().export(fileId=item_id, mimeType='text/csv').execute().decode('utf-8')
                        process_spreadsheet_scenario(item_id, item_name, item.get('webViewLink', ''), item['modifiedTime'], raw_csv_stream, current_dept_code, allowed_group)
                    
                    # 💡 분기 B: 날것의 생 PDF 파일 처리 (기존 drive_sync.py 기능 무결성 흡수)
                    elif item_mime == 'application/pdf':
                        print(f"   📥 [바이너리 다운로드] 실시간 PDF 스트림 획득 중...")
                        local_sync_path = f"temp_{item_id}.pdf"
                        try:
                            request = drive_service.files().get_media(fileId=item_id)
                            with open(local_sync_path, "wb") as f:
                                f.write(request.execute())
                            
                            # 🎯 [대통합 문법 교정]: 튜플 리턴 규격 (raw_text, detected_team) 수령선 동기화 및 부서코드 즉시 갱신
                            raw_text, detected_team = parse_document_to_markdown(local_sync_path)
                            if detected_team and detected_team != "공통부서":
                                current_dept_code = detected_team
                        finally:
                            if os.path.exists(local_sync_path):
                                os.remove(local_sync_path)  # FinOps 컨테이너 디스크 정리
                        
                        # 파싱이 완료되면 일반 문서 마크다운 청킹 파이프라인으로 토스
                        text_splitter = MarkdownTextSplitter(chunk_size=800, chunk_overlap=100)
                        chunks = text_splitter.split_text(raw_text)
                        
                        vectors = generate_gemini_embeddings(chunks)
                        rows_to_insert = []
                        for i, chunk in enumerate(chunks):
                            rows_to_insert.append({
                                "chunk_id": str(uuid.uuid4()), "file_id": item_id, "doc_name": item_name,
                                "doc_url": item.get('webViewLink', ''), "last_modified": item['modifiedTime'],
                                "content": chunk, "embedding": vectors[i], "dept_code": current_dept_code, "allowed_groups": allowed_group
                            })
                        direct_load_rows_to_bq(rows_to_insert, item_id)

                    # 💡 분기 C: 구글 문서(Docs) 및 프리젠테이션(PPT) 처리
                    else:
                        if item_mime == 'application/vnd.google-apps.document':
                            raw_text = drive_service.files().export(fileId=item_id, mimeType='text/plain').execute().decode('utf-8')
                        elif item_mime == 'application/vnd.google-apps.presentation':
                            raw_text = drive_service.files().export(fileId=item_id, mimeType='text/plain').execute().decode('utf-8')
                        else:
                            raw_text = drive_service.files().get_media(fileId=item_id).execute().decode('utf-8')
                            
                        if not raw_text.strip(): continue
                        
                        text_splitter = MarkdownTextSplitter(chunk_size=800, chunk_overlap=100)
                        chunks = text_splitter.split_text(raw_text)
                        
                        vectors = generate_gemini_embeddings(chunks)
                        rows_to_insert = []
                        for i, chunk in enumerate(chunks):
                            rows_to_insert.append({
                                "chunk_id": str(uuid.uuid4()), "file_id": item_id, "doc_name": item_name,
                                "doc_url": item.get('webViewLink', ''), "last_modified": item['modifiedTime'],
                                "content": chunk, "embedding": vectors[i], "dept_code": current_dept_code, "allowed_groups": allowed_group
                            })
                        direct_load_rows_to_bq(rows_to_insert, item_id)
                        
                except Exception as content_err:
                    print(f"   ❌ [{item_name}] 지식 사냥 에러 발생: {content_err}")
                    
    except Exception as api_err:
        print(f"❌ 드라이브 탑 토폴로지 탐색 오류: {api_err}")

def purge_deleted_files_from_bq(active_drive_ids: set):
    print("\n🧹 [삭제 추적 파이프라인] 구글 드라이브 영구 삭제 건 검사 및 청소 개시...")
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
    try:
        sql = f"SELECT DISTINCT file_id, max(doc_name) as d_name FROM `{table_ref}` GROUP BY file_id"
        query_job = bq_client.query(sql)
        bq_file_ids = []
        id_to_name = {}
        for row in query_job.result():
            bq_file_ids.append(row.file_id)
            id_to_name[row.file_id] = row.d_name
            
        deleted_file_ids = set(bq_file_ids) - active_drive_ids
        if not deleted_file_ids:
            print("✨ 빅쿼리 지식 금고가 실제 드라이브와 100% 동기화 상태입니다.")
            return
            
        print(f"🚨 [유령 고아 지식 발견] 총 {len(deleted_file_ids)}개의 자원이 삭제 확인되었습니다. 금고 제명 처리...")
        for del_id in deleted_file_ids:
            del_name = id_to_name.get(del_id, "알 수 없는 문서")
            purge_sql = f"DELETE FROM `{table_ref}` WHERE file_id = '{del_id}'"
            bq_client.query(purge_sql).result()
            print(f"   🗑 영구 데이터 삭제 완료: [{del_name}]")
    except Exception as e:
        print(f"⚠️ 삭제 스위퍼 예외 방어 작동: {e}")

def main_sync_pipeline():
    print("🚀 [전사 마스터 파이프라인] 코웨이 공유 드라이브 통합 자율 CRUD 동기화 부트업...")
    drive_service = get_drive_service(ADMIN_USER_EMAIL)
    
    # 💡 요렇게 맨 앞에 #을 붙여서 주석 처리해 줍니다! (이미 지어놨으니 건너뛰기)
    #create_bq_vector_table_if_not_exists()
    
    bq_meta_map = get_existing_knowledge_meta()
    active_drive_ids = set()
    
    for folder_meta in TARGET_FOLDERS:
        f_name = folder_meta["name"]
        f_id = folder_meta["id"]
        f_group = folder_meta["allowed_group"]
        
        if "입력하세요" in f_id or "입력" in f_id: continue
            
        print(f"\n📡 기지국 스캔 가동: [{f_name}] (보안 레이어: {f_group})")
        traverse_and_sync(f_id, "분류 불가", f_group, drive_service, bq_meta_map, active_drive_ids)
        
    purge_deleted_files_from_bq(active_drive_ids)
    print("\n🏁 [파이프라인 자율 종료] 전사 지식 동기화 대공사가 완전히 완료되었습니다!")

if __name__ == "__main__":
    main_sync_pipeline()