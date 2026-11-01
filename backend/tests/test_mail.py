"""Письма: разбор .eml/.mbox/.zip, реквизиты переписки, автосвязи и цепочки."""
import io
import mailbox
import zipfile
from email.message import EmailMessage

from app import legal, mail


def eml(frm, to, subj, date, mid, body, reply=None, att=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = frm, to, subj, date, mid
    if reply:
        m["In-Reply-To"] = reply
    m.set_content(body)
    if att:
        m.add_attachment(att[1], maintype="text", subtype="plain", filename=att[0])
    return bytes(m)


E1 = eml("Иванов <ivanov@alfa.ru>", "Кузнецов <k@stroybeta.ru>", "О сроках работ по договору № 15/2024",
         "Mon, 04 Mar 2024 10:00:00 +0300", "<m1@alfa.ru>", "Просим сообщить сроки.", att=("График.txt", "Этап 3".encode()))
E2 = eml("Кузнецов <k@stroybeta.ru>", "Иванов <ivanov@alfa.ru>", "RE: О сроках работ по договору № 15/2024",
         "Tue, 05 Mar 2024 11:00:00 +0300", "<m2@stroybeta.ru>", "Каркас — до 31.07.2024.\n\n> Просим сообщить сроки.",
         reply="<m1@alfa.ru>")
E3 = eml("Иванов <ivanov@alfa.ru>", "Кузнецов <k@stroybeta.ru>", "Fwd: О сроках работ по договору № 15/2024",
         "Wed, 06 Mar 2024 12:00:00 +0300", "<m3@alfa.ru>", "Принято.")
L1 = ("ООО «Альфа»\nИсх. № 45 от 10.04.2024\nДиректору ООО «СтройБета» А. В. Кузнецову\nО переносе сроков\n"
      "Просим перенести сроки.\nС уважением, С. П. Иванов")
L2 = ("ООО «СтройБета»\nИсх. № 112 от 12.04.2024\nГенеральному директору ООО «Альфа» С. П. Иванову\nО переносе сроков\n"
      "На ваш исх. № 45 от 10.04.2024 сообщаем, что перенос согласован.\nДиректор А. В. Кузнецов")
OTHER = ("ООО «Гамма»\nИсх. № 45 от 01.02.2024\nДиректору ООО «Дельта»\nО поставке\nТекст.")   # тот же номер, другие стороны


def test_eml_headers_body_and_attachments():
    text, atts = mail.parse_eml(E2)
    assert text.startswith(legal.EMAIL_MARK) and "В ответ на: <m1@alfa.ru>" in text
    assert "Каркас — до 31.07.2024." in text and "Просим сообщить" not in text   # цитата убрана
    assert mail.parse_eml(E1)[1] == [("График.txt", "Этап 3".encode())]


def test_mbox_and_zip(tmp_path):
    box = mailbox.mbox(str(tmp_path / "a.mbox"))
    for raw in (E1, E2):
        box.add(raw)
    box.close()
    assert len(mail.split_mbox(str(tmp_path / "a.mbox"))) == 2
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Письма/a.eml", E1)
        z.writestr("__MACOSX/._a.eml", b"x")
        z.writestr(".DS_Store", b"x")
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    assert [n for n, _ in mail.unzip(str(tmp_path / "a.zip"))] == ["a.eml"]


def test_letter_requisites():
    m = legal.analyze_meta(mail.parse_eml(E2)[0])
    assert m["type"] == "letter" and m["date"] == "2024-03-05" and m["in_reply_to"] == ["<m1@alfa.ru>"]
    assert set(m["participants"]) == {"alfa.ru", "stroybeta.ru"}
    assert "ООО «Альфа»" in legal.analyze_meta(L1)["participants"]
    assert legal.doc_label(m).startswith("Письмо от 05.03.2024 «RE: О сроках")
    m2 = legal.analyze_meta(L2)
    assert (m2["number"], m2["date"], m2["subject"]) == ("112", "2024-04-12", "О переносе сроков")
    assert m2["letter_refs"] == [{"num": "45", "date": "2024-04-10", "reply": True, "text": "исх. № 45 от 10.04.2024"}]
    assert legal.analyze_meta(L1)["letter_refs"] == []    # свой «Исх. № 45» — не ссылка


def test_links_and_threads():
    texts = {"e1": mail.parse_eml(E1)[0], "e2": mail.parse_eml(E2)[0], "e3": mail.parse_eml(E3)[0],
             "l1": L1, "l2": L2, "other": OTHER,
             "c": "ДОГОВОР ПОДРЯДА № 15/2024\nг. Москва «01» февраля 2024 г.\nООО «Альфа» и ООО «СтройБета»."}
    metas = {k: legal.analyze_meta(t) for k, t in texts.items()}
    links = legal.build_links(metas, {"attach": [["att", "e1"]]})
    rel = {(l["from"], l["to"]): l["relation"] for l in links}
    assert rel[("e2", "e1")] == "ответ на"                 # почтовые заголовки
    assert rel[("e3", "e2")] == "та же переписка"          # тема без Re/Fwd, те же стороны
    assert rel[("l2", "l1")] == "ответ на"                 # «на ваш исх. № 45 от 10.04.2024»
    assert ("l2", "other") not in rel                      # тот же номер, но другая дата
    assert rel[("e1", "c")] == "письмо по"
    threads = {tuple(t["docs"]): t for t in legal.threads(metas, links)}
    assert ("e1", "e2", "e3") in threads and ("l1", "l2") in threads
    assert threads[("e1", "e2", "e3")]["contract"] == "c"


def test_ref_without_date_needs_same_parties():
    """«На ваше письмо № 45» без даты: из двух писем № 45 выбираем то, где те же стороны."""
    reply = ("ООО «СтройБета»\nИсх. № 113 от 15.04.2024\nДиректору ООО «Альфа»\nО переносе сроков\n"
             "В ответ на ваше письмо № 45 сообщаем.")
    metas = {k: legal.analyze_meta(t) for k, t in {"l1": L1, "other": OTHER, "r": reply}.items()}
    rel = {(l["from"], l["to"]) for l in legal.build_links(metas, {})}
    assert ("r", "l1") in rel and ("r", "other") not in rel


def test_same_subject_months_apart_is_new_thread():
    a = eml("A <a@alfa.ru>", "B <b@beta.ru>", "О сроках поставки", "Mon, 04 Mar 2024 10:00:00 +0300", "<x1@alfa.ru>", "Текст")
    b = eml("A <a@alfa.ru>", "B <b@beta.ru>", "Re: О сроках поставки", "Mon, 11 Mar 2024 10:00:00 +0300", "<x2@alfa.ru>", "Текст")
    c = eml("A <a@alfa.ru>", "B <b@beta.ru>", "О сроках поставки", "Mon, 16 Sep 2024 10:00:00 +0300", "<x3@alfa.ru>", "Текст")
    metas = {k: legal.analyze_meta(mail.parse_eml(v)[0]) for k, v in {"a": a, "b": b, "c": c}.items()}
    rel = {(l["from"], l["to"]) for l in legal.build_links(metas, {})}
    assert ("b", "a") in rel and not any(x == "c" for x, _ in rel)
