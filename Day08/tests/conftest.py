"""
포인트: kim(alpha, 전달 가능)·intern(alpha, 전달 불가) 두 컨텍스트. 권한 검사 테스트가 이 둘을 대조한다.

주요 내용:
공통 fixture. 모델·네트워크 호출이 없다.
"""

import pytest

from guardlab.context import UserContext


@pytest.fixture
def kim() -> UserContext:
    return UserContext(user_id="kim.dev", tenant="nurisoft", projects=("alpha",), can_send=True)


@pytest.fixture
def intern() -> UserContext:
    return UserContext(user_id="intern.lee", tenant="nurisoft", projects=("alpha",), can_send=False)
