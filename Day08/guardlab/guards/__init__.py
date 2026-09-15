"""
"모델 기반 방어" 의 자리다. 여기 있는 것은 전부 GuardSignal(신호) 을 내고 끝난다. 실행 권한을 주는 코드는 없다.

주요 내용:
Guard 계약과 클라이언트. 공급자 출력 형식보다 내부 계약(GuardSignal·PolicyDecision)을 먼저 고정한다.
"""


from .contracts import Action, GuardSignal, PolicyDecision, unknown_signal
from .injection import InjectionGuard
from .pii import PIIGuard, Span

__all__ = ["Action", "GuardSignal", "PolicyDecision", "unknown_signal", "InjectionGuard", "PIIGuard", "Span"]
