
# Dcode 를 설치해서 사용해보기

주의! 

AI 모델은 OpenRouter 기반의 API 를 활용하되
OpenAI, Anthropic, Google 3사 모델의 호출을 허용하지 않습니다.

테스트 해보실 모델: GLM-5.3-Flash, DeepSeek-V4.1-Flash, Qwen3.8 등 

---

1. 설치와 실행

WSL 환경 내에서 실행하셔야만 합니다.

Docs: https://docs.langchain.com/oss/deepagents/code/quickstart

```bash
curl -LsSf https://langch.in/dcode | bash
```

---

2. dcode 설치 후 작업해보시면 좋은 Task List

1) Prompt Caching 을 고려한 DeepAgents 구성
   
2) 모델별 특성을 이해하고 Harness Profile 을 나눠서 호출하는 DeepAgents 구성

3) DeepAgents 가 활용 가능한 메모리 구성 확인 및 테스트

4) DeepAgents Middleware 로 구성하는 Self-Improving

---

**목-금 PJT에서 쓰실 dcode 의 소스코드는 Day08 에서 제공합니다.**
- 소스코드에 주석 작성.
- 소스코드 맵 제공.