"""Runnable dense/sparse/RRF/weighted/MultiQuery retrieval comparisons."""
from __future__ import annotations

import numpy as np
from kiwipiepy import Kiwi
from rank_bm25 import BM25Okapi


def rrf(rankings: list[list[str]], weights=None, constant=60):
    weights = weights if weights is not None else [1.0] * len(rankings)
    if len(weights) != len(rankings) or constant <= 0 or any(w < 0 for w in weights):
        raise ValueError("검색기별 가중치와 RRF 상수를 확인하세요.")
    scores = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] = scores.get(key, 0) + weight / (constant + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))


class KoreanBM25:
    def __init__(self, documents: dict[str, str], user_words=()):
        if not documents:
            raise ValueError("문서가 필요합니다.")
        self.documents = documents
        self.user_words = list(user_words)
        self.kiwi = Kiwi()
        for word in user_words:
            self.kiwi.add_user_word(word, "NNP")
        self.keys = list(documents)
        self.index = BM25Okapi([self.tokens(documents[key]) or ["__empty__"] for key in self.keys])

    def tokens(self, text):
        return [t.form.lower() for t in self.kiwi.tokenize(text) if t.tag.startswith(("N", "V", "SL", "SN"))]

    def search(self, query: str, limit=6):
        scores = self.index.get_scores(self.tokens(query))
        return [self.keys[i] for i in np.argsort(-scores)[:limit] if scores[i] > 0]

    def save(self, path):
        from day02.settings import write_json
        write_json(path, {"version": 1, "documents": self.documents, "user_words": self.user_words})

    @classmethod
    def load(cls, path):
        from day02.settings import read_json
        state = read_json(path)
        if state.get("version") != 1:
            raise ValueError("지원하지 않는 BM25 저장 형식입니다.")
        return cls(state["documents"], user_words=state["user_words"])


def cosine_ranking(keys, document_vectors, query_vector):
    vectors = np.asarray(document_vectors, dtype=float)
    query = np.asarray(query_vector, dtype=float)
    norms = np.linalg.norm(vectors, axis=1) * np.linalg.norm(query)
    if (norms == 0).any():
        raise ValueError("0 벡터를 비교할 수 없습니다.")
    scores = vectors @ query / norms
    return [(keys[i], float(scores[i])) for i in np.argsort(-scores)]


def weighted_scores(rankings, weights):
    if len(rankings) != len(weights) or any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("표현별 가중치를 확인하세요.")
    combined = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for key, score in ranking:
            combined[key] = combined.get(key, 0) + weight * score / sum(weights)
    return sorted(combined.items(), key=lambda pair: -pair[1])


def multi_query(client, queries, target_uri, limit=6):
    if not 1 <= len(queries) <= 3:
        raise ValueError("질의는 1~3개까지 허용합니다.")
    return rrf([[hit["uri"] for hit in client.find(q, target_uri, limit)] for q in queries])[:limit]
