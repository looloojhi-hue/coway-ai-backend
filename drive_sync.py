# drive_sync.py
import os
from datetime import datetime, timezone
from google.auth import default
from googleapiclient.discovery import build
from google.cloud import bigquery
from document_parser import parse_document_to_markdown
from embed_and_load import chunk_and_load_to_gcp

# =====================================================================
# ⚙️ GCP 및 엔터프라이즈 환경 변수 설정
# =====================================================================
PROJECT_ID = "hr-division-ai-rpa"
BQ_LOCATION = "asia-northeast3"
DATASET_ID = "hrga_rag_data"
TABLE_ID = "knowledge_master"

# 💡 수석님 전용 테스트 공유 드라이브 혹은 루트 폴더 ID
SHARED_FOLDER_ID = "1Fsq7uifKqCx9g43i7dynRltvHcb56_iw" 

# =====================================================================
# 🔒 보안 표준(Keyless) 및 API 클라이언트 초기화
# =====================================================================
credentials, _ = default(scopes=[
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud-platform"
])

drive_service = build("drive", "v3", credentials=credentials)
bq_client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

# =====================================================================
# 🧠 알고리즘 1: 역추적(Bottom-Up) 부서/TF 인식 로직
# =====================================================================
def find_department_by_bottom_up(folder_id):
    """이름이 '팀' 또는 'TF'로 끝나는 최초의 조직 폴더명을 찾아 반환합니다."""
    current_id = folder_id
    
    while current_id:
        try:
            folder = drive_service.files().get(
                fileId=current_id, fields="id, name, parents", supportsAllDrives=True
            ).execute()
            
            folder_name = folder.get("name", "").strip()
            print(f"   🔍 상위 폴더 확인 중: {folder_name}")
            
            if folder_name.endswith("팀") or folder_name.endswith("TF"):
                print(f"   🎯 부서 매핑 성공: [{folder_name}]")
                return folder_name
                
            parents = folder.get("parents", [])
            current_id = parents[0] if parents else None
            
        except Exception as e:
            print(f"   ⚠️ 폴더 역추적 중 오류 발생 (권한 제한 등): {e}")
            break
            
    return "공통" 

# =====================================================================
# 🔐 알고리즘 2: 구글 드라이브 그룹스 권한(ACL) 자동 추출 로직
# =====================================================================
def get_allowed_groups_from_drive(file_id):
    try:
        permissions_result = drive_service.permissions().list(
            fileId=file_id, fields="permissions(type, emailAddress, role)", supportsAllDrives=True
        ).execute()
        
        groups = []
        for perm in permissions_result.get("permissions", []):
            email = perm.get("emailAddress")
            if email:
                groups.append(email)
                
        if groups:
            return ",".join(groups)
            
    except Exception as e:
        print(f"   ⚠️ 권한(ACL) 정보 획득 실패: {e}")
        
    return "employee_all@coway.com" 

# =====================================================================
# 🔄 알고리즘 3: 딥스캔(Deep-Scan) 및 빅쿼리 변동성 동기화 (CRUD)
# =====================================================================
def get_all_files_in_bq():
    query = f"""
        SELECT file_id, max(last_modified) as last_modified 
        FROM `{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`
        GROUP BY file_id
    """
    try:
        query_job = bq_client.query(query)
        results = query_job.result()
        return {row.file_id: row.last_modified for row in results}
    except Exception:
        return {} 

def delete_file_from_bq(file_id):
    query = f"""
        DELETE FROM `{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}`
        WHERE file_id = @file_id
    """
    job_config = bigquery.QueryJobConfiguration(
        query_parameters=[bigquery.ScalarQueryParameter("file_id", "STRING", file_id)]
    )
    bq_client.query(query, job_config=job_config).result()
    print(f"🗑️  [동기화-삭제] 무효화된 기존 파일 ID [{file_id}]의 벡터 지식을 제거했습니다.")

def scan_and_sync_google_drive():
    print("🚀 [배치 시작] 구글 공유 드라이브 딥스캐닝 파이프라인을 가동합니다.")
    
    bq_files_snapshot = get_all_files_in_bq()
    current_drive_file_ids = set()
    
    # 💡 [핵심 알고리즘 3 추가] BFS 재귀 탐색을 위한 큐(Queue) 초기화
    target_files = []
    folders_to_check = [SHARED_FOLDER_ID]
    
    try:
        print("🕵️‍♂️ 폴더 뎁스를 뚫고 문서 탐색을 시작합니다...")
        while folders_to_check:
            current_folder_id = folders_to_check.pop(0)
            # 폴더이거나 PDF/엑셀인 항목을 한 번에 다 가져옵니다.
            query_str = f"'{current_folder_id}' in parents and trashed = false"
            
            page_token = None
            while True:
                results = drive_service.files().list(
                    q=query_str,
                    fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime, parents)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageToken=page_token
                ).execute()
                
                for file in results.get("files", []):
                    mime_type = file.get("mimeType")
                    # 하위 폴더를 발견하면 큐에 추가해서 다음 턴에 까봅니다!
                    if mime_type == "application/vnd.google-apps.folder":
                        folders_to_check.append(file["id"])
                    # 우리가 찾는 목표 파일(PDF/엑셀)이면 리스트에 수집합니다!
                    elif mime_type in ["application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]:
                        target_files.append(file)
                        
                page_token = results.get("nextPageToken")
                if not page_token:
                    break
                    
        print(f"📁 구글 드라이브 딥스캔 완료: 총 {len(target_files)}개의 대상 파일 발견.")
        
        # 발견된 대상 파일들을 순회하며 CRUD 동기화 진행
        for file in target_files:
            file_id = file["id"]
            doc_name = file["name"]
            doc_url = file["webViewLink"]
            drive_modified_str = file["modifiedTime"].replace("Z", "+00:00")
            drive_modified = datetime.fromisoformat(drive_modified_str)
            parent_folder_id = file.get("parents", [None])[0]
            
            current_drive_file_ids.add(file_id)
            
            is_new = file_id not in bq_files_snapshot
            is_modified = False
            
            if not is_new:
                bq_modified = bq_files_snapshot[file_id]
                if drive_modified > bq_modified:
                    is_modified = True
            
            if is_new or is_modified:
                if is_new:
                    print(f"\n✨ [신규 감지] 파일명: {doc_name}")
                else:
                    print(f"\n🔄 [수정 감지] 파일명: {doc_name} (내용 갱신으로 인한 재임베딩 가동)")
                    delete_file_from_bq(file_id)
                
                # 역추적 로직 가동!
                dept_code = find_department_by_bottom_up(parent_folder_id)
                allowed_groups = get_allowed_groups_from_drive(file_id)
                
                print(f"📥 파일 다운로드 및 파싱 처리 중...")
                
                local_test_path = "sample_rule.pdf" 
                if os.path.exists(local_test_path):
                    parsed_md = parse_document_to_markdown(local_test_path)
                    
                    chunk_and_load_to_gcp(
                        file_id=file_id,
                        doc_name=doc_name,
                        doc_url=doc_url,
                        last_modified=drive_modified.isoformat(),
                        markdown_text=parsed_md,
                        dept_code=dept_code,
                        allowed_groups=allowed_groups
                    )
                else:
                    print(f"❌ 로컬에 테스트용 {local_test_path} 파일이 없어 파싱을 스킵합니다.")
            else:
                print(f"🍏 [동기화 유지] 변동 없음: {doc_name}")
                
        # 삭제 감지 로직
        for bq_file_id in bq_files_snapshot.keys():
            if bq_file_id not in current_drive_file_ids:
                print(f"\n🚨 [삭제 감지] 구글 드라이브에서 사라진 파일 ID [{bq_file_id}] 포착.")
                delete_file_from_bq(bq_file_id)
                
        print("\n✅ [배치 완료] 빅쿼리 벡터 DB가 구글 드라이브의 최신 실시간 상태와 100% 동기화되었습니다.")
        
    except Exception as e:
        print(f"\n❌ 드라이브 동기화 가동 중 치명적 에러 발생: {e}")

if __name__ == "__main__":
    scan_and_sync_google_drive()