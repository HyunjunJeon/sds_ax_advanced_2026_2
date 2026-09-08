"""파일 이동 후 루트 진입점과 실제 코퍼스 경로가 분리되지 않았는지 확인한다."""

import json
import subprocess
import sys

from common.config import DAY02, DAY03, LAB
from common.lab import FILES, load_build
from common.provenance import run_provenance


def test_numbered_graphs_load_from_day03_root():
    assert LAB == DAY03
    assert DAY02 == DAY03 / "vendor/day02"
    assert not (DAY03 / "rag_labs").exists()
    for architecture, filename in FILES.items():
        assert (DAY03 / filename).is_file()
        assert callable(load_build(architecture))


def test_prepare_uses_bundled_data_even_from_another_cwd(tmp_path):
    # 실습은 CLI 플래그 없이 파일 안의 LabConfig로 실행한다. 다른 작업 디렉터리에서
    # 실행해도 동봉 코퍼스·.env를 Day03 기준 경로로 읽는지 확인한다(플래그 없음).
    proc = subprocess.run(
        [sys.executable, str(DAY03 / "00_prepare.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["internal_documents"] == 10 and result["backend"] == "local"


def test_provenance_records_code_and_assets_without_env_contents():
    provenance = run_provenance()
    assert "student_tasks.py" in provenance["files"]
    assert "06_supervisor_rag.py" in provenance["files"]
    assert provenance["assets"]["data/gold.jsonl"]
    assert all(".env" not in path and "archive/" not in path for path in provenance["files"])
