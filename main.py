from fastapi import FastAPI
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage

# 💡 방금 만든 LangGraph 뼈대를 가져옵니다!
from graph import coway_agent_app 

app = FastAPI(title="Coway AI Agent API", version="2.0")

class QueryRequest(BaseModel):
    current: str
    lastQ: str = ""
    lastA: str = ""
    userEmail: str = ""
    userAgent: str = ""

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Coway AI Backend V2 is running!"}

@app.post("/chat")
def chat_endpoint(request: QueryRequest):
    print(f"\n📥 [API 수신] 사용자 질문: {request.current}")
    
    # 💡 [핵심] 과거 대화 기록(Context) 조립하기
    messages_list = []
    
    # 이전 질문과 답변이 있다면 장부에 먼저 적어줍니다.
    if request.lastQ:
        messages_list.append(HumanMessage(content=request.lastQ))
    if request.lastA:
        messages_list.append(AIMessage(content=request.lastA))
        
    # 마지막으로 지금 막 들어온 진짜 질문을 적어줍니다.
    messages_list.append(HumanMessage(content=request.current))

    # 1. LangGraph 장부(State)의 초기값 세팅
    initial_state = {
        "messages": messages_list,  # 👈 조립된 대화 기록 전체를 넘김!
        "current_intent": "",
        "refined_query": "",
        "retrieved_docs": "",
        "user_info": {"email": request.userEmail} # 👈 이메일을 장부에 기록!
    }
    
    # 2. 🚀 LangGraph 실행! (여기서 graph.py의 노드들이 순서대로 작동합니다)
    print("================ LangGraph 시작 ================")
    final_state = coway_agent_app.invoke(initial_state)
    print("================ LangGraph 종료 ================\n")
    
    # 💡 [핵심] 장부(State)의 가장 마지막 메시지가 바로 Reasoner가 작성한 최종 답변입니다!
    raw_content = final_state["messages"][-1].content
    
    # 🛡️ [메타데이터 제거 방어 로직] 
    if isinstance(raw_content, list):
        final_answer_text = "".join([item["text"] for item in raw_content if isinstance(item, dict) and "text" in item])
    else:
        final_answer_text = str(raw_content)

    # 🔗 [RAG 출처 매핑 로직 (미래 대비)]
    # 나중에 rag_node에서 State 장부에 "sources"라는 이름으로 출처를 담아주면, 여기서 꺼내서 프론트엔드로 보냅니다.
    # (BQ 노드는 출처가 없으므로 자동으로 빈 배열([])이 들어갑니다.)
    source_results = final_state.get("sources", [])

    # 3. 프론트엔드(GAS) 규격에 맞춰 응답 반환
    return {
        "summary": {
            "summaryText": [
                {
                    "type": "text",
                    "text": final_answer_text # 🗣️ AI의 순수 텍스트 답변
                }
            ]
        },
        "results": source_results # 📚 RAG 검색 시 출처(문서 링크, 제목 등)가 여기에 쏙 들어갑니다!
    }