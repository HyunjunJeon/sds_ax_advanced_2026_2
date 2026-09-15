"""
실행: uv run pytest tests/test_defenses_05.py -q
포인트: 승인은 도구·인자 해시·요청자·만료에 묶인다. 수신자 변경·버전 변경·다른 요청자·재사용은 각각 다른 코드로 거절된다.

주요 내용:
05 승인 바인딩: 승인은 도구·인자 해시·요청자·만료에 묶인다. 인자가 바뀌거나 재사용되면 막힌다.
"""

from datetime import timedelta

from guardlab.components import ApprovalStore

ARGS = {"project": "alpha", "recipient": "seojun.park@nurisoft.example", "subject": "s", "body": "b", "report_version": 1}


def test_edited_recipient_does_not_match_approval():
    store = ApprovalStore(ttl=timedelta(minutes=5))
    store.approve("send_report", ARGS, requester="kim.dev", approver="lead")
    drifted = {**ARGS, "recipient": "ext.partner@outside.example"}
    assert store.check("send_report", drifted, requester="kim.dev").reason_code == "APPROVAL_ARGS_MISMATCH"
    assert store.check("send_report", ARGS, requester="kim.dev").action == "ALLOW"
    assert store.check("send_report", ARGS, requester="kim.dev").reason_code == "APPROVAL_ALREADY_USED"



def test_other_requester_and_version_drift_are_rejected():
    store = ApprovalStore(ttl=timedelta(minutes=5))
    store.approve("send_report", ARGS, requester="kim.dev", approver="lead")
    assert store.check("send_report", ARGS, requester="intern.lee", consume=False).reason_code == "NO_APPROVAL"
    assert store.check("send_report", {**ARGS, "report_version": 2}, requester="kim.dev", consume=False).reason_code == "APPROVAL_ARGS_MISMATCH"
