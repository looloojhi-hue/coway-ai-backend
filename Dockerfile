# 파이썬 3.11 슬림 버전 환경을 가져옵니다
FROM python:3.11-slim

# 파이썬 내부의 출력 버퍼를 무력화하여 로그가 실시간으로 찍히도록 보장합니다
ENV PYTHONUNBUFFERED True

# 작업할 폴더를 지정합니다
WORKDIR /app

# 명세서를 복사하고 라이브러리를 설치합니다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 💡 [초격차 캐시 억까 원천 파괴 패치]
# 런타임 환경에서 텍스트 스플리터 및 jinja2 엔진 누락 크래시를 방지하기 위해 강제 주입 레이어를 결합합니다.
RUN pip install --no-cache-dir langgraph-checkpoint-firestore google-genai google-cloud-bigquery requests langchain-text-splitters jinja2

# 내 PC의 모든 코드를 클라우드로 복사합니다
COPY . .

# Cloud Run 오피셜 표준 규격에 맞추어 uvicorn이 포트 환경변수를 네이티브하게 물고 인입되도록 조율합니다.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]