"""Portable project configuration. Never modifies the parent project's environment."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from day02.errors import ConfigurationError

def project_root():
    source_root = Path(__file__).resolve().parents[2]
    for candidate in [source_root, Path.cwd(), Path.cwd() / "Day02", *Path.cwd().parents]:
        if (candidate / "skills").is_dir() and (candidate / "data/business/catalog.json").is_file():
            return candidate.resolve()
    return source_root


ROOT = project_root()


def write_json(path: Path, value, *, private: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if private:
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Settings:
    root: Path
    api_key: str
    api_base: str
    model: str
    vision_model: str
    embedding_model: str
    embedding_dimension: int
    port: int
    remote_url: str = ""
    remote_key: str = ""

    @classmethod
    def load(cls, root: Path | None = None):
        root = Path(root or os.getenv("DAY02_ROOT", ROOT)).resolve()
        values = {**dotenv_values(root.parent / ".env"), **dotenv_values(root / ".env"), **os.environ}
        # 포트는 .env에서 바꾼다. OPENVIKING_PORT가 표준 이름이고, 기존 .env를 깨지
        # 않도록 DAY02_PORT도 계속 읽는다(둘 다 있으면 OPENVIKING_PORT가 이긴다).
        port_value = values.get("OPENVIKING_PORT") or values.get("DAY02_PORT") or 19350
        try:
            port = int(port_value)
        except ValueError:
            raise ConfigurationError("OPENVIKING_PORT는 정수여야 합니다.") from None
        if not 1024 <= port <= 65535:
            raise ConfigurationError("OPENVIKING_PORT는 1024~65535 범위여야 합니다.")
        remote_url = values.get("OPENVIKING_URL") or ""
        remote_key = values.get("OPENVIKING_API_KEY") or ""
        if bool(remote_url) != bool(remote_key):
            raise ConfigurationError("원격 연결에는 OPENVIKING_URL과 OPENVIKING_API_KEY가 모두 필요합니다.")
        return cls(
            root=root, api_key=values.get("OPENROUTER_API_KEY") or "",
            api_base=values.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
            model=values.get("DAY02_MODEL") or "google/gemini-3.8-flash",
            vision_model=values.get("DAY02_VISION_MODEL") or "google/gemini-3.8-flash",
            embedding_model=values.get("DAY02_EMBEDDING_MODEL") or "qwen/qwen3-embedding-8b",
            embedding_dimension=int(values.get("DAY02_EMBEDDING_DIMENSION") or 4096),
            port=port, remote_url=remote_url.rstrip("/"), remote_key=remote_key,
        )

    @property
    def state_dir(self):
        return self.root / ".openviking"

    @property
    def local_url(self):
        config_path = self.state_dir / "ov.conf"
        port = read_json(config_path)["server"]["port"] if config_path.exists() else self.port
        return f"http://127.0.0.1:{port}"

    def require_model_key(self):
        if not self.api_key:
            raise ConfigurationError("Day02/.env 또는 상위 .env에 OPENROUTER_API_KEY를 설정하세요.")

    def connection(self):
        if self.remote_url:
            return self.remote_url, self.remote_key
        credentials = self.state_dir / "client.json"
        if not credentials.exists():
            raise ConfigurationError("먼저 day02 server init과 day02 server start를 실행하세요.")
        saved = read_json(credentials)
        if saved["url"] != self.local_url:
            raise ConfigurationError("서버 주소와 문서 계정의 주소가 다릅니다. 로컬 설정을 확인하세요.")
        return saved["url"], saved["user_key"]

    def redact(self, text: str) -> str:
        secrets = [self.api_key, self.remote_key]
        for name, field in [("ov.conf", "root_api_key"), ("client.json", "user_key")]:
            path = self.state_dir / name
            if path.exists():
                data = read_json(path)
                secrets.append(data.get("server", data).get(field, ""))
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text
