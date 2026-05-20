# test_app.py
from langchain_core.messages import HumanMessage
from graph import coway_agent_app

def run_coway_chat_test(user_query: str, user_email: str):
    print(f"\n" + "="*60)
    print(f"🎬 [통합 테스트 시작] 유저: {user_email} / 질의: '{user_query}'")
    print("="*60)
    
    # 💡 웹 화면(GAS)에서 백엔드로 넘겨줄 초기 장부(State) 구조를 그대로 모사합니다.
    initial_state = {
        "messages": [HumanMessage(content=user_query)],
        "user_info": {
            "email": user_email,
            "name": "고길동",
            "dept": "경영지원본부"
        }
    }
    
    # LangGraph 앱 구동! (모든 노드를 순서대로 돌며 최종 장부를 반환합니다.)
    final_output = coway_agent_app.invoke(initial_state)
    
    print("\n" + "="*60)
    print("🤖 [최종 AI 응답 출력]")
    print("="*60)
    print(final_output["messages"][-1].content)
    print("="*60 + "\n")

if __name__ == "__main__":
    # 🎯 대망의 V2 하이브리드 RAG 연동 테스트 슛!
    run_coway_chat_test(
        user_query="총무팀 출장 규정 중에서 예외 조항이나 사고 시 체재비 지급 기준 알려줘", 
        user_email="looloojhi@coway.com"
    )