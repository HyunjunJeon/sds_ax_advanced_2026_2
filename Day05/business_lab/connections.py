"""수업에서 명시적으로 선택한 Langfuse 연결. import만으로 외부 호출하지 않는다."""


def connect_langfuse():
    """키 값은 출력하지 않고 인증만 확인한다. 프로젝트 키는 Day05/.env에서 읽는다."""
    import os
    from langfuse import get_client

    if not os.environ.get("LANGFUSE_TRACING_ENVIRONMENT"):
        raise ValueError("Day05/.env에 LANGFUSE_TRACING_ENVIRONMENT를 설정하세요.")
    client = get_client()
    if not client.auth_check():
        raise RuntimeError("Langfuse 인증 실패. Day05/.env의 설정을 직접 확인하세요.")
    return client
