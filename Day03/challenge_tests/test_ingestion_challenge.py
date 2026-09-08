"""C의 반례. 외부 API 대신 완료 시각을 제어하며 실제 SQLite와 재시작을 사용한다."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from student_tasks import FencedRegistry, Published

URL = "https://www.python-httpx.org/advanced/timeouts/"


def test_expired_worker_cannot_commit_after_takeover(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=5)
    b = FencedRegistry(r.path).claim(URL, "h1", now=6, ttl=10)
    assert b.epoch > a.epoch
    assert r.commit(b, "viking://new", now=7)
    assert not r.commit(a, "viking://late", now=8)
    assert r.head(URL).uri == "viking://new"


def test_pending_new_version_keeps_old_ready_head(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=5)
    assert r.commit(a, "viking://v1", now=1)
    b = r.claim(URL, "h2", now=2, ttl=5)
    assert r.head(URL).content_hash == "h1"
    assert r.snapshot() == {URL: Published(URL, "h1", "viking://v1", a.epoch)}
    assert r.commit(b, "viking://v2", now=3)
    assert r.snapshot()[URL].content_hash == "h2"


def test_newer_content_claim_fences_an_unexpired_old_worker(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=100)
    b = r.claim(URL, "h2", now=1, ttl=100)
    assert b.epoch > a.epoch
    assert not r.commit(a, "viking://old", now=2)
    assert r.head(URL) is None
    assert r.commit(b, "viking://new", now=3)


def test_completed_retry_is_idempotent_but_cannot_change_uri(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=5)
    assert r.commit(a, "viking://v1", now=1)
    assert r.commit(a, "viking://v1", now=99)
    assert not r.commit(a, "viking://different", now=99)
    assert r.claim(URL, "h1", now=100, ttl=5) is None


def test_modified_lease_fields_cannot_extend_ownership(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=5)
    assert not r.commit(replace(a, expires_at=100), "viking://forged", now=6)
    assert not r.commit(replace(a, content_hash="h2"), "viking://forged", now=1)
    assert not r.commit(a, "viking://expired", now=5)
    assert r.head(URL) is None


def test_identical_concurrent_claim_has_one_owner(tmp_path):
    path = tmp_path / "registry.sqlite"
    FencedRegistry(path)

    def claim(_):
        return FencedRegistry(path).claim(URL, "same", now=0, ttl=30)

    with ThreadPoolExecutor(max_workers=6) as pool:
        leases = list(pool.map(claim, range(12)))
    assert sum(lease is not None for lease in leases) == 1


def test_reversion_cancels_a_different_pending_version(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=10)
    assert r.commit(a, "viking://v1", now=1)
    b = r.claim(URL, "h2", now=2, ttl=10)
    restored = r.claim(URL, "h1", now=3, ttl=10)
    assert restored is not None and restored.epoch > b.epoch
    assert r.commit(restored, "viking://v1", now=4)
    assert not r.commit(b, "viking://v2", now=5)


def test_url_alias_and_empty_snapshot(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    assert r.snapshot() == {}
    a = r.claim(URL + "?utm_source=course#section", "h1", now=0, ttl=10)
    assert a.url == URL
    assert r.claim(URL, "h1", now=1, ttl=10) is None
    assert r.commit(a, "viking://v1", now=2)
    assert r.head(URL + "#other") == r.head(URL)


def test_ready_head_survives_a_real_process_restart(tmp_path):
    r = FencedRegistry(tmp_path / "registry.sqlite")
    a = r.claim(URL, "h1", now=0, ttl=5)
    assert r.commit(a, "viking://persisted", now=1)
    # Python 모듈 전역 캐시만 구현하면 이 검사에서 실패한다. 다른 프로세스가 DB를 읽는다.
    code = (
        "import json,sys; from pathlib import Path; from dataclasses import asdict; "
        "from student_tasks import FencedRegistry; "
        "print(json.dumps(asdict(FencedRegistry(Path(sys.argv[1])).head(sys.argv[2]))))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(r.path), URL],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["uri"] == "viking://persisted"


@pytest.mark.parametrize(
    "url,content_hash,ttl", [("file:///tmp/a", "h", 5), (URL, "", 5), (URL, "h", 0)]
)
def test_invalid_claim_is_rejected(tmp_path, url, content_hash, ttl):
    with pytest.raises(ValueError):
        FencedRegistry(tmp_path / "registry.sqlite").claim(url, content_hash, now=0, ttl=ttl)
