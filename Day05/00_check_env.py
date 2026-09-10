"""00. 환경과 실제 상태의 의미 확인. 기본 점검은 외부 호출 없음."""

import os
from pathlib import Path

from business_lab.dataset import load_fixtures
from business_lab.environment import OrderEnvironment
from course_config import MODE, USE_LANGFUSE

CHECK_CONNECTIONS = False  # True이면 Judge 약 1회와 Langfuse 인증을 확인한다.


def main():
    if Path.cwd().resolve() != Path(__file__).resolve().parent:
        raise SystemExit("Day05 폴더에서 실행하세요.")
    for key in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "DEEPEVAL_JUDGE_MODEL",
                "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_TRACING_ENVIRONMENT"):
        print(f"{key}: {'설정됨' if os.environ.get(key) else '미설정'}")
    for fixture in load_fixtures().values():
        env = OrderEnvironment(fixture)
        try:
            assert env.snapshot()["orders"] is not None
        finally:
            env.close()
    print(f"초기 상태 로드 정상. 수업 실행 MODE={MODE}, Langfuse={USE_LANGFUSE}")
    print("Agent의 완료 주장, 서비스의 실제 상태, 정책 준수는 별도로 평가합니다.")
    if CHECK_CONNECTIONS:
        from business_lab.models import load_judge, load_openrouter_env, resolve_judge
        from pydantic import BaseModel

        class Ping(BaseModel):
            ok: bool

        agent_name = load_openrouter_env()["model"]
        if resolve_judge().model == agent_name:
            raise ValueError("Agent와 Judge를 다른 모델로 설정하세요.")
        print("이 점검은 Judge 호출 약 1회입니다. 연결 확인은 평가 정확도 검증이 아닙니다.")
        load_judge(agent_model=agent_name).generate('{"ok": true}를 반환하세요.', schema=Ping)
        if USE_LANGFUSE:
            from business_lab.connections import connect_langfuse
            connect_langfuse()
        print("연결 확인 완료")


if __name__ == "__main__":
    main()
