from __future__ import annotations

import re

_WORD_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_STOP_WORDS = {
    "一个",
    "一张",
    "图片",
    "生成",
    "参考",
    "需要",
    "希望",
    "create",
    "and",
    "are",
    "for",
    "from",
    "image",
    "into",
    "is",
    "of",
    "on",
    "or",
    "make",
    "please",
    "the",
    "this",
    "to",
    "use",
    "using",
    "want",
    "with",
}


def query_terms(value: str, *, limit: int = 64) -> tuple[str, ...]:
    result: list[str] = []
    for token in _WORD_PATTERN.findall(str(value or "").lower()):
        if token in _STOP_WORDS:
            continue
        candidates = [token]
        if token[0] >= "\u4e00" and len(token) > 4:
            candidates.extend(
                token[index : index + width]
                for width in (2, 3, 4)
                for index in range(len(token) - width + 1)
            )
        for candidate in candidates:
            if len(candidate) < 2 or candidate in _STOP_WORDS or candidate in result:
                continue
            result.append(candidate)
            if len(result) >= limit:
                return tuple(result)
    return tuple(result)


def text_match_score(terms: tuple[str, ...], value: str, weight: float) -> float:
    text = str(value or "").lower()
    return sum(weight * min(2.0, max(1.0, len(term) / 2)) for term in terms if term in text)
