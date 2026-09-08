from day02.session import Sessions


def test_session_and_scope_isolation(tmp_path):
    sessions = Sessions(tmp_path / "sessions.db")
    scope = {"entity": "알파", "as_of": "2026-09-08", "namespace": "business"}
    sessions.append("one", scope, "신청 기한?", "검증된 답변")
    assert len(sessions.history("one", scope)) == 2
    assert sessions.history("two", scope) == []
    assert sessions.history("one", {**scope, "entity": "베타"}) == []
