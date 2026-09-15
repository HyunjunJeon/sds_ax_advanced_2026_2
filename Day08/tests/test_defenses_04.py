"""
실행: uv run pytest tests/test_defenses_04.py -q
포인트: 첫 일치 규칙·미일치 허용. 넓은 허용이 앞에 오면 거부에 닿지 않는다. /policy/* 는 하위 폴더를 놓친다.

주요 내용:
04 파일 권한의 사실: 첫 일치 규칙 적용, 미일치 시 허용. 규칙 순서가 결과를 바꾼다.
"""

from deepagents import FilesystemPermission
from deepagents.middleware.filesystem import _check_fs_permission


def test_policy_deny_must_precede_broad_allow():
    wrong = [FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="allow"),
             FilesystemPermission(operations=["write"], paths=["/policy/**"], mode="deny")]
    fixed = [FilesystemPermission(operations=["write"], paths=["/policy/**"], mode="deny"),
             FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="allow")]
    assert _check_fs_permission(wrong, "write", "/policy/rules.md") == "allow"
    assert _check_fs_permission(fixed, "write", "/policy/rules.md") == "deny"
    assert _check_fs_permission(fixed, "read", "/policy/rules.md") == "allow"



def test_non_recursive_pattern_misses_nested_paths():
    shallow = [FilesystemPermission(operations=["write"], paths=["/policy/*"], mode="deny")]
    deep = [FilesystemPermission(operations=["write"], paths=["/policy/**"], mode="deny")]
    assert _check_fs_permission(shallow, "write", "/policy/rules.md") == "deny"
    assert _check_fs_permission(shallow, "write", "/policy/archive/rules.md") == "allow"  # 하위 폴더를 놓친다
    assert _check_fs_permission(deep, "write", "/policy/archive/rules.md") == "deny"
