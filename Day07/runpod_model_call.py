"""RunPod vLLM (GLM-5.3-Flash, H200 x4) via langchain-openai.

pip install langchain-openai
"""

from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="GLM-5.3-Flash",
    base_url="https://jd0xx40obj73f9-8000.proxy.runpod.net/v1",
    api_key="EMPTY",
    # RunPod 프록시는 100초에서 연결을 끊으므로 긴 응답은 streaming=True로 받는다.
    timeout=95,
    extra_body={
        # 각 모델마다 Chat Template 이 다르기 때문에
        # Reasoning On/Off 요청을 전달하는 방식도 모두 다름.
        "chat_template_kwargs": {"reasoning_effort": "low", "clear_thinking": True},
    },
)

if __name__ == "__main__":
    print(llm.invoke("한 줄로 자기소개 해줘.").content)
