"""Почта и архивы: письма .eml / .msg (Outlook), почтовые ящики .mbox, ZIP-архивы.

Письмо превращается в текст с заголовками в начале (От, Кому, Дата, Тема, Message-ID, «В ответ на»):
по ним legal.analyze_meta узнаёт реквизиты, а legal.build_links связывает письма в цепочки.
Вложения поддерживаемых форматов индексируются отдельными документами и привязываются к письму.
"""
from __future__ import annotations

import email
import email.policy
import mailbox
import re
import tempfile
import zipfile
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .legal import EMAIL_MARK
# защита от «архивных бомб» и случайно выбранного диска целиком
ZIP_MAX_FILES = 20000
ZIP_MAX_BYTES = 4 * 1024 ** 3


def _decode(payload: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8", "cp1251", "koi8-r"):
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return soup.get_text("\n")


def _strip_quoted(text: str) -> str:
    """Цитата прошлых писем («> …», «-----Original Message-----») — она уже есть в базе отдельным письмом.
    Оставляем только новый текст: так поиск не находит одно и то же десятки раз."""
    lines = []
    for line in text.splitlines():
        if re.match(r"\s*(-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение)|"
                    r"(от|from):\s.+(отправлено|sent):)", line, re.IGNORECASE):
            break
        if re.match(r"\s*(>|&gt;)", line):
            continue
        if re.match(r"\s*\d{1,2}\.\d{1,2}\.\d{2,4}.{0,40}(пишет|написал|wrote):\s*$", line, re.IGNORECASE):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _fmt_addrs(values: list[str]) -> str:
    out = []
    for name, addr in getaddresses(values):
        out.append(f"{name} <{addr}>" if name and addr else (name or addr))
    return ", ".join(x for x in out if x)


def _header_block(sender: str, to: str, cc: str, date: str, subject: str, message_id: str,
                  in_reply_to: str, references: str, attachments: list[str]) -> str:
    rows = [EMAIL_MARK, f"От: {sender}", f"Кому: {to}"]
    if cc:
        rows.append(f"Копия: {cc}")
    rows += [f"Дата: {date}", f"Тема: {subject or '(без темы)'}"]
    if message_id:
        rows.append(f"Message-ID: {message_id}")
    if in_reply_to:
        rows.append(f"В ответ на: {in_reply_to}")
    if references:
        rows.append(f"Цепочка: {references}")
    if attachments:
        rows.append(f"Вложения: {', '.join(attachments)}")
    return "\n".join(rows)


def parse_eml(data: bytes) -> tuple[str, list[tuple[str, bytes]]]:
    """Письмо .eml → (текст с заголовками, вложения [(имя, байты)])."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    plain, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if name or part.get_content_disposition() == "attachment":
            if name and payload:
                attachments.append((Path(name).name, payload))
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode(payload, part.get_content_charset()))
        elif ctype == "text/html":
            html.append(_html_to_text(_decode(payload, part.get_content_charset())))
    body = "\n".join(plain) if plain else "\n".join(html)
    try:
        date = parsedate_to_datetime(msg["Date"]).strftime("%d.%m.%Y %H:%M") if msg["Date"] else ""
    except (TypeError, ValueError):
        date = str(msg["Date"] or "")
    head = _header_block(
        _fmt_addrs(msg.get_all("From", [])), _fmt_addrs(msg.get_all("To", [])), _fmt_addrs(msg.get_all("Cc", [])),
        date, str(msg["Subject"] or "").strip(), str(msg["Message-ID"] or "").strip(),
        str(msg["In-Reply-To"] or "").strip(), " ".join(str(msg["References"] or "").split()),
        [n for n, _ in attachments])
    return f"{head}\n\n{_strip_quoted(body)}", attachments


def parse_msg(data: bytes) -> tuple[str, list[tuple[str, bytes]]]:
    """Письмо Outlook .msg → то же, что parse_eml."""
    import extract_msg

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        m = extract_msg.openMsg(path)
        try:
            attachments = []
            for a in m.attachments:
                name = getattr(a, "longFilename", None) or getattr(a, "shortFilename", None)
                payload = getattr(a, "data", None)
                if name and isinstance(payload, bytes):
                    attachments.append((Path(name).name, payload))
            body = m.body or (_html_to_text(m.htmlBody.decode("utf-8", errors="replace")) if m.htmlBody else "")
            date = m.date.strftime("%d.%m.%Y %H:%M") if getattr(m, "date", None) else ""
            head = _header_block(m.sender or "", m.to or "", m.cc or "", date, (m.subject or "").strip(),
                                 (m.messageId or "").strip(), (getattr(m, "inReplyTo", None) or "").strip(),
                                 "", [n for n, _ in attachments])
        finally:
            m.close()
    finally:
        Path(path).unlink(missing_ok=True)
    return f"{head}\n\n{_strip_quoted(body or '')}", attachments


def split_mbox(path: str) -> list[bytes]:
    """Почтовый ящик .mbox (выгрузка Thunderbird, Gmail Takeout…) → отдельные письма .eml."""
    box = mailbox.mbox(path, create=False)
    try:
        return [m.as_bytes() for m in box]
    finally:
        box.close()


def unzip(path: str) -> list[tuple[str, bytes]]:
    """Файлы из ZIP (с вложенными папками). Служебные файлы macOS и скрытые — пропускаем."""
    out, total = [], 0
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_FILES:
            raise ValueError(f"В архиве больше {ZIP_MAX_FILES} файлов — разбейте его на части")
        for info in infos:
            name = info.filename
            if "__MACOSX" in name or any(p.startswith(".") for p in Path(name).parts):
                continue
            total += info.file_size
            if total > ZIP_MAX_BYTES:
                raise ValueError("Архив слишком большой после распаковки (больше 4 ГБ) — разбейте его на части")
            raw = z.read(info)
            # имена из Windows-архивов приходят в cp866
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("cp866")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            out.append((Path(name).name, raw))
    return out


def eml_pages(data: bytes) -> list[tuple[int | None, str]]:
    return [(None, parse_eml(data)[0])]


def msg_pages(data: bytes) -> list[tuple[int | None, str]]:
    return [(None, parse_msg(data)[0])]

