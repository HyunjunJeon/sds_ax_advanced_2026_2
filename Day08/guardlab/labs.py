"""
사용: lab = load_lab("02_action_guard.py"); lab.build_agent(...)   (테스트에서 번호 파일의 build_agent 를 재사용한다)

주요 내용:
번호 파일을 모듈로 불러온다 (이름이 숫자로 시작해 import 문을 쓸 수 없다). 테스트와 06 이 쓴다.
"""


from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .config import DAY08


def load_lab(filename: str):
    path = DAY08 / filename
    name = "lab_" + path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
