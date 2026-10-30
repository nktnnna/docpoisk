"""Выгрузка результатов анализа: сводная редакция договора (DOCX) и реестр рисков (XLSX)."""
from __future__ import annotations

import io
from datetime import datetime

from .legal import CONSOLIDATED_MARK, is_heading, Clause, word_diff

LEVEL_COLORS = {"высокий": "F8D7D0", "средний": "FBEBC8", "низкий": "E3EFE9"}


def consolidated_docx(result: dict, redline: bool) -> bytes:
    """Договор в действующей редакции. redline=True — правки выделены цветом, удалённое зачёркнуто."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
    from docx.shared import Pt, RGBColor

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name, style.font.size = "Times New Roman", Pt(12)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(result["title"] or "Договор")
    run.bold, run.font.size = True, Pt(14)
    if result["amendments"]:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("(в редакции " + ", ".join(a["label_gen"] for a in result["amendments"]) + ")").italic = True
    note = doc.add_paragraph()
    r = note.add_run(f"{CONSOLIDATED_MARK} программой DocSearch {datetime.now():%d.%m.%Y}. "
                     "Перед использованием сверьте с подписанными документами.")
    r.italic, r.font.size, r.font.color.rgb = True, Pt(9), RGBColor(0x70, 0x70, 0x70)

    for c in result["clauses"]:
        clause = Clause(c["num"], c["text"], c["status"], c["old"], c["sources"])
        if clause.num == "" and clause.text.startswith(result["title"] or "\0"):
            clause.text = clause.text[len(result["title"]):].lstrip()   # заголовок уже напечатан
            if not clause.text:
                continue
        p = doc.add_paragraph()
        heading = is_heading(clause)
        if heading:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        prefix = f"{clause.num}. " if clause.num else ""
        basis = "; ".join(clause.sources)

        if clause.status == "deleted":
            if redline:
                p.add_run(prefix).bold = heading
                t = p.add_run(clause.old or clause.text)
                t.font.strike, t.font.color.rgb = True, RGBColor(0xB0, 0x30, 0x20)
            else:
                p.add_run(prefix + "Утратил силу.").bold = heading
            p.add_run(f" ({basis})").italic = True
            continue

        p.add_run(prefix).bold = heading
        if redline and clause.status == "changed" and clause.old is not None:
            for kind, text in word_diff(clause.old, clause.text):
                t = p.add_run(text)
                t.bold = heading
                if kind == "+":
                    t.font.highlight_color = WD_COLOR_INDEX.YELLOW
                elif kind == "-":
                    t.font.strike, t.font.color.rgb = True, RGBColor(0xB0, 0x30, 0x20)
        else:
            t = p.add_run(clause.text)
            t.bold = heading
            if redline and clause.status == "added":
                t.font.highlight_color = WD_COLOR_INDEX.BRIGHT_GREEN
        if clause.status in ("changed", "added"):
            r = p.add_run(f" (в ред. {basis})")
            r.italic, r.font.size = True, Pt(9)

    ops = result["ops"]
    if ops:
        doc.add_page_break()
        doc.add_paragraph().add_run("Перечень внесённых изменений").bold = True
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for cell, text in zip(table.rows[0].cells, ("Пункт", "Изменение", "Основание", "Статус")):
            cell.text = text
            cell.paragraphs[0].runs[0].bold = True
        names = {"replace": "изложен в новой редакции", "add": "добавлен", "delete": "исключён",
                 "append": "дополнен", "replace_words": "заменены слова"}
        for op in ops:
            row = table.add_row().cells
            row[0].text = op["clause"] or "весь текст"
            what = names.get(op["op"], op["op"])
            if op["op"] == "replace_words":
                what += f": «{op['old']}» → «{op['new']}»"
            row[1].text = what
            row[2].text = op["source"]
            row[3].text = ("применено" + (" (распознано нейросетью — проверьте)" if op["by_llm"] else "")
                           if op["applied"] else "не применено") + (f"; {op['error']}" if op["error"] else "")
        if result["unparsed"]:
            doc.add_paragraph()
            doc.add_paragraph().add_run("Правки, которые не удалось применить автоматически:").bold = True
            for u in result["unparsed"]:
                doc.add_paragraph(f"{u['source']}: {u['text']}", style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def risks_xlsx(registers: list[dict]) -> bytes:
    """Реестр рисков: по строке на риск, с документом, пунктом и цитатой."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр рисков"
    head = ["№", "Документ", "Пункт", "Цитата из документа", "Риск", "Уровень", "Для кого", "Рекомендация", "Источник",
            "Дата анализа"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDE6E2")
    n = 0
    for reg in registers:
        for r in reg["risks"]:
            n += 1
            ws.append([n, reg["filename"], r["clause"], r["quote"], r["risk"], r["level"], r["for_whom"],
                       r["recommendation"], r["source"], reg.get("created_at", "")[:10]])
            color = LEVEL_COLORS.get(r["level"])
            if color:
                ws.cell(ws.max_row, 6).fill = PatternFill("solid", fgColor=color)
    for col, width in zip("ABCDEFGHIJ", (5, 30, 10, 50, 50, 11, 16, 45, 12, 12)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
