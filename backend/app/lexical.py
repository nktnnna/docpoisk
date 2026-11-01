"""Поиск по словам (BM25) — дополнение к векторному поиску.

Векторы хорошо ловят смысл («в каком суде» → «подсудность»), но плохо — точные
совпадения: номер пункта «5.2», сумму «1 500 000», ФИО, название организации.
Поэтому ищем двумя способами и объединяем результаты (см. chat_service).

Индекс строится в памяти из той же коллекции Qdrant и перестраивается,
когда в базе меняется число фрагментов.
"""
from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, defaultdict

from . import store

try:
    import snowballstemmer

    _ru = snowballstemmer.stemmer("russian")
    _en = snowballstemmer.stemmer("english")

    def _stem(w: str) -> str:
        return _en.stemWord(w) if w.isascii() else _ru.stemWord(w)
except ImportError:  # без стеммера — грубо обрезаем окончания
    def _stem(w: str) -> str:
        return w[:6]

_TOKEN = re.compile(r"\d+(?:\.\d+)*|[a-zа-я]+")
STOPWORDS = set("""
а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до его ее если
есть еще же за здесь и из или им их к как ко когда кто ли либо мне может мы на над надо наш не него нее нет ни
них но ну о об однако он она они оно от очень по под при с со так также такой там те тем то того тоже той только
том ты у уже чем что чтобы чье чья эта эти это я какой какая какие каким каких который которая которые
ли сколько ваш the a an of to in and or is are for on what which
""".split())

K1, B = 1.5, 0.75


def norm_numbers(text: str) -> str:
    """«18 600 000» → «18600000», «0,3» → «0.3» — чтобы число из вопроса совпадало с числом в тексте."""
    text = re.sub(r"(?<=\d)[ \u00a0\u202f\n](?=\d{3}(?!\d))", "", text)   # \n — перенос строки внутри числа в PDF
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def tokenize(text: str) -> list[str]:
    words = _TOKEN.findall(norm_numbers(text.lower().replace("ё", "е")))
    return [w if w[0].isdigit() else _stem(w) for w in words if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]


class Index:
    def __init__(self, points: list[dict]):
        self.points = points
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths: list[int] = []
        for i, p in enumerate(points):
            tokens = tokenize(f"{p.get('section') or ''}\n{p['text']}")
            self.lengths.append(len(tokens))
            for term, tf in Counter(tokens).items():
                self.postings[term].append((i, tf))
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0

    def search(self, query: str, limit: int, doc_ids: list[str] | None = None) -> list[tuple[dict, float]]:
        n = len(self.points)
        allowed = set(doc_ids) if doc_ids else None
        scores: dict[int, float] = defaultdict(float)
        for term in set(tokenize(query)):
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
            for i, tf in posting:
                if allowed is not None and self.points[i]["doc_id"] not in allowed:
                    continue
                norm = tf + K1 * (1 - B + B * self.lengths[i] / self.avg_len)
                scores[i] += idf * tf * (K1 + 1) / norm
        best = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [(self.points[i], s) for i, s in best]


_index: Index | None = None
_index_count = -1
_lock = asyncio.Lock()


def invalidate() -> None:
    global _index_count
    _index_count = -1


async def get_index() -> Index:
    global _index, _index_count
    async with _lock:
        count = await store.count()
        if _index is None or count != _index_count:
            _index = Index(await store.all_chunks())
            _index_count = count
        return _index


async def search(query: str, limit: int, doc_ids: list[str] | None = None) -> list[tuple[dict, float]]:
    return (await get_index()).search(query, limit, doc_ids)
