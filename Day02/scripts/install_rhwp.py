"""Install a checksum-verified native rhwp CLI in Day02/bin (no Docker)."""
import hashlib
import io
import platform
import tarfile
import zipfile

import httpx

from day02.settings import Settings

VERSION = "0.8.6"


def main():
    settings = Settings.load()
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    machine = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "AMD64": "x86_64"}.get(platform.machine())
    if not system or not machine or (system == "windows" and machine != "x86_64"):
        raise RuntimeError("이 OS/아키텍처의 rhwp 릴리스 경로가 정의되지 않았습니다.")
    extension = "zip" if system == "windows" else "tar.gz"
    asset = f"rhwp-v{VERSION}-{system}-{machine}.{extension}"
    base = f"https://github.com/edwardkim/rhwp/releases/download/v{VERSION}"
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        checksums = client.get(f"{base}/SHA256SUMS.txt")
        checksums.raise_for_status()
        lines = [line.split() for line in checksums.text.splitlines() if line.strip()]
        expected = next((row[0] for row in lines if row[-1].lstrip("*").endswith(asset)), None)
        if not expected:
            raise RuntimeError("릴리스 체크섬에 대상 아카이브가 없습니다.")
        response = client.get(f"{base}/{asset}")
        response.raise_for_status()
    data = response.content
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("rhwp 다운로드 체크섬 불일치")
    binary_name = "rhwp.exe" if system == "windows" else "rhwp"
    if extension == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next(name for name in archive.namelist() if name.rsplit("/", 1)[-1] == binary_name)
            executable = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = next(m for m in archive.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == binary_name)
            executable = archive.extractfile(member).read()
    destination = settings.root / "bin" / binary_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(executable)
    destination.chmod(0o755)
    print(f"rhwp {VERSION} 설치 완료: {destination.relative_to(settings.root)} (SHA256 확인)")


if __name__ == "__main__":
    main()
