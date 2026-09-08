from datetime import date

import pytest

from day02.evidence import Budget, Metadata, digest
from day02.tools.search import RetrievalContext


class FakeViking:
    url = "http://test"

    def __init__(self):
        self.texts = {}
        self.hits = []
        self.reads = []

    def find(self, query, target_uri, limit):
        return [{"uri": uri} for uri in self.hits[:limit]]

    def read(self, uri, offset=0, limit=100):
        self.reads.append((uri, offset, limit))
        return "".join(self.texts[uri].splitlines(keepends=True)[offset:offset+limit])

    def grep(self, *args):
        return []


@pytest.fixture
def context():
    client = FakeViking()
    docs = {}
    examples = [
        ("v2", "알파", "approved", "2026-07-01", None, "신청은 다음 달 15일까지입니다.\n"),
        ("addendum", "알파", "approved", "2026-08-01", None, "추가 약정: 신청은 다음 달 20일까지입니다.\n"),
        ("old", "알파", "approved", "2025-01-01", "2026-07-01", "신청은 다음 달 말일까지입니다.\n"),
        ("beta", "베타", "approved", "2026-01-01", None, "베타는 크레딧 30%입니다.\n"),
        ("draft", "알파", "draft", "2026-01-01", None, "미승인: 크레딧 100%입니다.\n"),
    ]
    for name, entity, status, start, end, text in examples:
        uri = f"viking://resources/test/{name}.md"
        client.texts[uri] = text
        client.hits.append(uri)
        meta = Metadata(doc_id=name, title=name, source=name + ".md", entity=entity,
                        status=status, valid_from=start, valid_until=end).model_dump(mode="json")
        meta["family"] = "alpha-sla" if entity == "알파" else "beta-sla"
        docs[uri] = {"metadata": meta, "remote_hash": digest(text), "line_count": len(text.splitlines())}
    return RetrievalContext(client, {"documents": docs, "target_uri": "viking://resources/test"},
                            entity="알파", as_of=date(2026, 9, 8), budget=Budget())
