"""Day-03 전용 Python OpenViking 설정·문서 계정 준비. 기존 .env와 Day-02 저장소는 보존한다.

가르치는 것:
- 실습 인프라의 격리 운영: 실습용 서버·계정·키를 Day-02의 기존 저장소와 분리해
  준비한다. 기존 자격증명을 덮어쓰지 않고 재실행은 멱등으로 설계하는 운영 습관이다.

아래 ACTION을 단계에 따라 바꿔가며 실행한다(1단계씩, 한 번에 하나):
1. ACTION = "configure" — .openviking/rag.conf를 만든다. 출력되는 명령으로 서버를
   별도 터미널에서 띄운다(서버 프로세스 자체는 외부 프로그램이다).
2. 서버가 뜬 뒤 ACTION = "init" — 문서 전용 계정을 만들고 .openviking/rag-client.env에
   URL/키를 저장한다. 이후 실습 파일의 LabConfig에서
   backend="viking", viking_env=".openviking/rag-client.env"로 지정해 사용한다.
"""

import json
import os
import secrets

import httpx
from dotenv import dotenv_values

from common.config import DAY02, DAY03, viking_port

# ── 실행할 단계 ──────────────────────────────────────────────────────────────
ACTION = "configure"  # "configure" 또는 "init"
# 포트는 .env의 OPENVIKING_PORT에서 읽는다(없으면 1935). 실습 파일의 검색 주소도
# 같은 값을 보므로 서버와 클라이언트가 따로 놀지 않는다.
PORT = viking_port()


def configure(port: int) -> None:
    """서버 설정 파일(.openviking/rag.conf)을 만든다. 임베딩/VLM은 OpenRouter 키를 쓴다."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY가 필요합니다.")
    directory = DAY03 / ".openviking"
    directory.mkdir(exist_ok=True)
    path = directory / "rag.conf"
    # 이미 있으면 기존 값을 유지한 채 필요한 항목만 갱신한다(키 재발급 시 안전).
    config = json.loads(
        path.read_text() if path.exists() else (DAY02 / "ov.conf.example").read_text()
    )
    config["embedding"]["dense"].update(
        api_key=key,
        model=os.getenv("OPENROUTER_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b"),
        dimension=int(os.getenv("OPENVIKING_EMBEDDING_DIMENSION", "4096")),
    )
    config["vlm"].update(
        api_key=key,
        model=os.getenv("OPENROUTER_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "google/gemini-3.8-flash",
    )
    config["storage"]["workspace"] = str(directory / "rag-data")
    url = f"http://127.0.0.1:{port}"
    config["server"].update(
        host="127.0.0.1",
        port=port,
        cors_origins=[url],
        # 루트 키는 첫 생성 시에만 만든다. 두 번째 configure부터는 기존 키를 유지한다.
        root_api_key=config["server"].get("root_api_key")
        if path.exists()
        else secrets.token_urlsafe(32),
    )
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    path.chmod(0o600)
    print("설정 생성 완료. 다음 명령으로 서버를 별도 터미널에서 실행하세요:")
    print(
        f"uv run --isolated --no-project --python 3.13 --with openviking==0.4.16 "
        f"openviking-server --config .openviking/rag.conf --host 127.0.0.1 --port {port}"
    )


def init(port: int) -> None:
    """문서 전용 계정을 준비하고 실습용 키 파일(.openviking/rag-client.env)을 만든다."""
    url = f"http://127.0.0.1:{port}"
    directory = DAY03 / ".openviking"
    config = json.loads((directory / "rag.conf").read_text())
    envfile = directory / "rag-client.env"
    if envfile.exists():
        # 키 파일이 이미 있으면 계정을 새로 만들지 않고 재사용한다.
        key = dotenv_values(envfile)["OPENVIKING_API_KEY"]
    else:
        response = httpx.post(
            url + "/api/v1/admin/accounts",
            headers={"X-API-Key": config["server"]["root_api_key"]},
            json={"account_id": "day03-rag", "admin_user_id": "student"},
            timeout=30,
            trust_env=False,
        )
        if response.status_code == 409:
            raise SystemExit(
                "계정은 있으나 로컬 키 파일이 없습니다. 기존 rag-client.env를 복원하세요."
            )
        response.raise_for_status()
        key = response.json()["result"]["user_key"]
    # 실제 문서 API에 접근되는지 확인한 뒤 키 파일을 쓴다. 실패하면 파일이 남지 않는다.
    response = httpx.get(
        url + "/api/v1/fs/ls",
        params={"uri": "viking://resources"},
        headers={"X-API-Key": key},
        timeout=30,
        trust_env=False,
    )
    response.raise_for_status()
    if response.json().get("status") == "error":
        raise SystemExit("문서 API 접근 실패")
    envfile.write_text("OPENVIKING_URL=" + url + "\nOPENVIKING_API_KEY=" + key + "\n")
    envfile.chmod(0o600)
    print('문서 계정 접근 확인. 실습에서 backend="viking", viking_env=".openviking/rag-client.env"를 사용하세요.')


if __name__ == "__main__":
    if ACTION == "configure":
        configure(PORT)
    elif ACTION == "init":
        init(PORT)
    else:
        raise SystemExit('ACTION은 "configure" 또는 "init"이어야 합니다.')
