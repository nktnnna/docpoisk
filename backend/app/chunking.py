"""Разбиение текста на чанки с перекрытием.

Режем «умно»: сначала пытаемся по абзацам, потом по строкам, предложениям,
словам — и только в крайнем случае посимвольно. Так чанк не обрывается
посреди предложения, и смысл пункта договора не теряется.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " ", ""]


@dataclass
class Chunk:
    text: str
    page: int | None
    index: int
    section: str = ""   # заголовок раздела, к которому относится фрагмент («5. Ответственность сторон»)


# Заголовки разделов в договорах и регламентах
_HEADING = re.compile(
    r"^(?:(?:раздел|статья|глава|часть|приложение)\s+[\dIVXLC№]+.{0,80}"   # «Статья 5. Оплата», «Приложение № 1»
    r"|\d{1,2}\.?\s+[А-ЯЁA-Z][^.;:!?]{2,60}"                               # «5. Ответственность сторон»
    r"|[А-ЯЁ][А-ЯЁ0-9 ,№\-–«»\"]{4,80})$",                                     # «ПРЕДМЕТ ДОГОВОРА»
    re.IGNORECASE,
)


_ORG = re.compile(r"^(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ФГУП|МУП|ГУП)\b")  # «ООО «РОМАШКА»» — не заголовок


def headings(text: str) -> list[str]:
    found = []
    for line in text.split("\n"):
        line = line.strip()
        # у заголовка мало слов и нет точки в конце — так не путаем его с пунктом «1. Подрядчик обязуется…»
        if 3 < len(line) <= 90 and len(line.split()) <= 8 and _HEADING.match(line) \
                and not _ORG.match(line):
            # регистронезависимость нужна для «Статья/СТАТЬЯ», но строка из одних заглавных — только капсом
            if line[0].isdigit() or line.split()[0].lower() in {"раздел", "статья", "глава", "часть", "приложение"} \
                    or line.isupper():
                found.append(line)
    return found


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"-\n(?=[а-яa-z])", "", text)        # переносы слов в PDF: «дого-\nвор»
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split(text: str, size: int, separators: list[str]) -> list[str]:
    """Рекурсивно режет текст на куски не длиннее size."""
    if len(text) <= size:
        return [text]
    sep, rest = separators[0], separators[1:]
    if sep == "":
        return [text[i : i + size] for i in range(0, len(text), size)]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, size, rest)
    pieces: list[str] = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        if len(piece) > size:
            pieces.extend(_split(piece, size, rest))
        elif piece:
            pieces.append(piece)
    return pieces


def split_text(text: str, size: int, overlap: int) -> list[str]:
    pieces = _split(text, size, SEPARATORS)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > size:
            chunks.append(current.strip())
            # перекрытие: хвост предыдущего чанка, начиная с границы слова
            tail = current[-overlap:] if overlap else ""
            if " " in tail:
                tail = tail[tail.index(" ") + 1 :]
            current = tail
        current += piece
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if len(c) > 20 or len(chunks) == 1]


def chunk_pages(pages: list[tuple[int | None, str]], size: int, overlap: int) -> list[Chunk]:
    result: list[Chunk] = []
    section = ""  # раздел переходит со страницы на страницу, пока не встретится новый заголовок
    for page_no, text in pages:
        for piece in split_text(normalize(text), size, overlap):
            heads = headings(piece)
            starts_with_head = heads and piece.lstrip().startswith(heads[0])
            result.append(Chunk(text=piece, page=page_no, index=len(result),
                                section=heads[0] if starts_with_head else section))
            if heads:
                section = heads[-1]
    return result


def embed_text(filename: str, section: str, text: str) -> str:
    """Что подаём в модель эмбеддингов: фрагмент вместе с документом и разделом.

    Пункт «5.2. Размер составляет 0,1 % в день» сам по себе не говорит, что речь о неустойке
    по договору подряда, — а с заголовком раздела и именем файла поиск его находит.
    """
    head = " / ".join(x for x in (Path(filename).stem, section) if x)
    return f"{head}\n{text}" if head else text
