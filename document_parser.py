# document_parser.py
import os
import vertexai
from vertexai.generative_models import GenerativeModel, Part

# 💡 GCP 프로젝트 정보 세팅
PROJECT_ID = "hr-division-ai-rpa"
# 💡 [핵심] 수석님 세팅과 동일하게 global 리전으로 맞춤!
LOCATION = "global" 

# Vertex AI 초기화
vertexai.init(project=PROJECT_ID, location=LOCATION)

def parse_document_to_markdown(file_path: str) -> str:
    """
    PDF 또는 이미지 파일을 읽어들여 표(Table)가 완벽히 보존된 Markdown으로 변환합니다.
    """
    print(f"📄 [{file_path}] 문서 파싱을 시작합니다...")
    
    # 1. 💡 [수정 완료] 수석님의 최신 환경에 맞춘 Gemini 3 Flash 모델 적용!
    model = GenerativeModel("gemini-3-flash-preview") 
    
    # 2. 로컬 파일을 Gemini가 읽을 수 있는 형태로 변환
    with open(file_path, "rb") as f:
        document_data = f.read()
    
    # 파일 확장자에 따라 MIME 타입 지정
    mime_type = "application/pdf" if file_path.lower().endswith(".pdf") else "image/png"
    document_part = Part.from_data(data=document_data, mime_type=mime_type)
    
    # 3. 구조화 엔지니어링 프롬프트
    prompt = """
    당신은 세계 최고의 문서 구조화(Parsing) 전문가입니다.
    첨부된 문서를 읽고, 내용을 처음부터 끝까지 마크다운(Markdown) 포맷으로 완벽하게 변환하세요.
    
    [절대 준수 지침]
    1. 문서에 포함된 표(Table)를 발견하면, 절대 텍스트로 풀어쓰지 말고 반드시 Markdown 표 문법(`| 항목 | 내용 |`)을 사용하여 행과 열의 구조를 100% 똑같이 재현하세요.
    2. 표의 병합된 셀이 있다면, 문맥에 맞게 각 셀에 내용을 반복해서라도 표의 Grid 형태를 유지하세요.
    3. 글머리 기호(Bullet points)나 번호 매기기 등도 마크다운 문법으로 살려주세요.
    4. 내용을 요약하거나 생략하지 말고, 문서에 있는 모든 텍스트를 있는 그대로 추출하세요.
    """
    
    # 4. Gemini에게 파싱 지시!
    print("🧠 Gemini 3 Flash가 문서의 레이아웃과 표 구조를 초고속으로 분석 중입니다...")
    response = model.generate_content([document_part, prompt])
    
    print("✅ 문서 파싱 완료!")
    return response.text

# ==========================================
# 🧪 테스트용 실행 코드
# ==========================================
if __name__ == "__main__":
    # 테스트할 PDF 파일 경로를 여기에 넣으세요 (예: 사규 문서, 여비지침 등)
    test_file = "sample_rule.pdf" 
    
    if os.path.exists(test_file):
        markdown_result = parse_document_to_markdown(test_file)
        print("\n" + "="*50)
        print("🎯 [파싱 결과 (Markdown)]")
        print("="*50)
        print(markdown_result)
        print("="*50)
    else:
        print(f"⚠️ 테스트할 '{test_file}' 파일을 폴더에 넣어주세요!")