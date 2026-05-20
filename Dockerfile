# 파이썬 3.14 슬림 버전 환경을 가져옵니다
FROM python:3.14-slim

# 작업할 폴더를 지정합니다
WORKDIR /app

# 명세서를 복사하고 라이브러리를 설치합니다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 내 PC의 모든 코드를 클라우드로 복사합니다
COPY . .

# 8080 포트로 서버를 실행하도록 명령합니다 (Cloud Run 기본 포트)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]