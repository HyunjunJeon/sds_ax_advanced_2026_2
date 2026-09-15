"""RunPod vLLM (GLM-5.3-Flash) Reasoning 과정 확인 via openai SDK.

langchain-openai의 ChatOpenAI는 vLLM이 보내는 비표준 필드(reasoning)를 버리므로
openai SDK로 직접 호출해 사고 과정(reasoning)과 답변(content)을 나눠 본다.

pip install openai
"""

from openai import OpenAI

client = OpenAI(
    base_url="https://jd0xx40obj73f9-8000.proxy.runpod.net/v1",
    api_key="EMPTY",
    # RunPod 프록시는 100초에서 연결을 끊으므로 긴 사고는 stream=True로 받는다.
    timeout=95,
)

MODEL = "GLM-5.3-Flash"
QUESTION = "3개의 연속된 홀수의 곱이 1287일 때 세 수를 구하고, 검산 과정을 짧게 보여줘."
EXTRA_BODY = {"chat_template_kwargs": {"reasoning_effort": "max", "clear_thinking": True}}


def reasoning_of(obj):
    # vLLM 버전에 따라 필드 이름이 reasoning 또는 reasoning_content다.
    return getattr(obj, "reasoning", None) or getattr(obj, "reasoning_content", None)


def non_streaming():
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": QUESTION}],
        extra_body=EXTRA_BODY,
    )
    message = resp.choices[0].message
    print("===== [비스트리밍] Reasoning =====")
    print(reasoning_of(message))
    print("===== [비스트리밍] Answer =====")
    print(message.content)
    print("usage:", resp.usage)


def streaming():
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": QUESTION}],
        extra_body=EXTRA_BODY,
        stream=True,
        stream_options={"include_usage": True},
    )
    print("===== [스트리밍] Reasoning =====")
    in_answer = False
    for chunk in stream:
        if chunk.usage:
            print("\nusage:", chunk.usage)
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if text := reasoning_of(delta):
            print(text, end="", flush=True)
        if delta.content:
            if not in_answer:
                print("\n===== [스트리밍] Answer =====")
                in_answer = True
            print(delta.content, end="", flush=True)


if __name__ == "__main__":
    non_streaming()
    print()
    streaming()
