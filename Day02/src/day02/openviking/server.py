"""Docker-free lifecycle. Only a process recorded and owned by this project is stopped."""
from __future__ import annotations

import os
import errno
import secrets
import shutil
import socket
import subprocess
import time

import httpx
import psutil
from filelock import FileLock

from day02.errors import ConfigurationError, Day02Error, ServiceError
from day02.openviking.client import VikingClient
from day02.settings import Settings, read_json, write_json

SERVER_VERSION = "0.4.16"


def check_port_available(port: int):
    """Probe a live listener, not bind(): recently closed TCP sockets may remain in TIME_WAIT."""
    with socket.socket() as probe:
        probe.settimeout(0.5)
        result = probe.connect_ex(("127.0.0.1", port))
    if result == 0:
        raise ConfigurationError(f"{port} 포트가 사용 중입니다. 다른 서버는 종료하지 않습니다.")
    if result not in {errno.ECONNREFUSED, 10061}:  # Windows WSAECONNREFUSED
        raise ConfigurationError(f"{port} 포트의 가용성을 확인하지 못했습니다 (socket error {result}).")


def initialize(settings: Settings):
    settings.require_model_key()
    directory = settings.state_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ov.conf"
    if path.exists():
        existing = read_json(path)
        if (existing["embedding"]["dense"]["model"] != settings.embedding_model
                or existing["embedding"]["dense"]["dimension"] != settings.embedding_dimension):
            raise ConfigurationError("임베딩 모델/차원이 기존 저장소와 다릅니다. 새 저장소에서 초기화하세요.")
        return {"status": "exists", "url": settings.local_url, "version": SERVER_VERSION}
    config = {
        "embedding": {"dense": {
            "provider": "openai", "model": settings.embedding_model,
            "api_base": settings.api_base, "api_key": settings.api_key,
            "dimension": settings.embedding_dimension, "encoding_format": "float",
        }},
        "vlm": {
            "provider": "openai", "model": settings.vision_model,
            "api_base": settings.api_base, "api_key": settings.api_key,
            "temperature": 0, "thinking": False,
        },
        "storage": {
            "workspace": str(directory / "data"),
            "agfs": {"backend": "local"}, "vectordb": {"backend": "local"},
        },
        "server": {
            "host": "127.0.0.1", "port": settings.port,
            "root_api_key": secrets.token_urlsafe(32),
            "cors_origins": [f"http://127.0.0.1:{settings.port}"],
        },
    }
    write_json(path, config, private=True)
    return {"status": "initialized", "url": settings.local_url, "version": SERVER_VERSION}


def owned_process(settings: Settings):
    path = settings.state_dir / "process.json"
    if not path.exists():
        return None
    saved = read_json(path)
    try:
        process = psutil.Process(saved["pid"])
        if abs(process.create_time() - saved["created"]) > 0.1:
            return None
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        # uv may exec the server, but the config path must remain in argv.
        if str(settings.state_dir / "ov.conf") not in process.cmdline():
            raise ConfigurationError("PID의 실행 명령이 이 프로젝트의 서버와 다릅니다. 종료하지 않습니다.")
        return process
    except psutil.NoSuchProcess:
        return None


def health(settings: Settings):
    try:
        response = httpx.get(f"{settings.local_url}/health", timeout=2, trust_env=False)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except (httpx.RequestError, ValueError):
        return False


def status(settings: Settings):
    process = owned_process(settings)
    return {"running": process is not None, "pid": process.pid if process else None,
            "healthy": health(settings), "url": settings.local_url, "version": SERVER_VERSION}


def initialize_account(settings: Settings):
    config = read_json(settings.state_dir / "ov.conf")
    path = settings.state_dir / "client.json"
    if path.exists():
        saved = read_json(path)
        if saved["workspace"] != config["storage"]["workspace"] or saved["url"] != settings.local_url:
            raise ConfigurationError("기존 문서 계정과 서버 저장소/주소가 다릅니다.")
        key = saved["user_key"]
    else:
        with VikingClient(settings.local_url, config["server"]["root_api_key"]) as client:
            result = client.request("POST", "/api/v1/admin/accounts", json={
                "account_id": "day02-local", "admin_user_id": "student",
            })
        key = result["user_key"]
        write_json(path, {"workspace": config["storage"]["workspace"],
                          "url": settings.local_url, "user_key": key}, private=True)
    with VikingClient(settings.local_url, key) as client:
        client.ls("viking://resources")


def start(settings: Settings, timeout: float = 120):
    if not (settings.state_dir / "ov.conf").exists():
        initialize(settings)
    with FileLock(str(settings.state_dir / "control.lock"), timeout=5):
        process = owned_process(settings)
        if process:
            if not health(settings):
                raise Day02Error("관리 중인 서버가 응답하지 않습니다. logs를 확인한 뒤 stop/start 하세요.")
            initialize_account(settings)
            return status(settings)
        port = read_json(settings.state_dir / "ov.conf")["server"]["port"]
        check_port_available(port)
        uv = shutil.which("uv")
        if not uv:
            raise ConfigurationError("uv 설치가 필요합니다: https://docs.astral.sh/uv/")
        command = [uv, "run", "--project", str(settings.root / "runtime/openviking"),
                   "--locked", "--python", "3.13", "openviking-server",
                   "--config", str(settings.state_dir / "ov.conf"),
                   "--host", "127.0.0.1", "--port", str(port)]
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        }
        log_path = settings.state_dir / "server.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(command, cwd=settings.root, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, **kwargs)
        log_path.chmod(0o600)
        tracked = psutil.Process(process.pid)
        write_json(settings.state_dir / "process.json", {
            "pid": process.pid, "created": tracked.create_time(), "command": command,
        }, private=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise Day02Error("서버 시작 실패. day02 server logs로 설치/설정 오류를 확인하세요.")
            if health(settings):
                initialize_account(settings)
                return status(settings)
            time.sleep(0.5)
        raise ServiceError("서버 시작 대기 시간 초과. logs/status를 확인하세요.")


def stop(settings: Settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.state_dir / "control.lock"), timeout=5):
        process = owned_process(settings)
        if process is None:
            return {"status": "already-stopped"}
        descendants = process.children(recursive=True)
        # Stop the launcher first to prevent it from respawning a child.
        for child in [process, *descendants]:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs([process, *descendants], timeout=10)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=5)
        (settings.state_dir / "process.json").unlink(missing_ok=True)
        return {"status": "stopped", "data_preserved": True}


def logs(settings: Settings, lines: int = 40):
    path = settings.state_dir / "server.log"
    if not path.exists():
        return "서버 로그가 없습니다."
    return settings.redact("\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]))
