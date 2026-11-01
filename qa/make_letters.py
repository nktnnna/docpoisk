"""~2600 писем с известной «правдой» о переписке — нагрузочный тест загрузки и автосвязей.

python qa/make_letters.py qa/letters     (нужен python-docx — он уже стоит вместе с приложением)

Создаёт: Почта_выгрузка.mbox (450 переписок по 2–5 писем, у 15 % ответов нет заголовка In-Reply-To —
только «Fwd:» в теме), Письма_DOCX.zip (230 переписок «бумажных» писем со ссылками «На ваш исх. № … от …»),
Почта_с_вложениями.zip (100 переписок .eml, у первого письма — вложение) и truth.json — какая переписка у какого файла.
"""
import io, json, mailbox, random, sys, zipfile
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
import docx

random.seed(7)
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
ORGS = [("ООО «Альфа»", "alfa.ru", "Иванов С. П."), ("ООО «СтройБета»", "stroybeta.ru", "Кузнецов А. В."),
        ("АО «МеталлТорг»", "metalltorg.ru", "Петрова Е. А."), ("ООО «Гамма Проект»", "gamma-pr.ru", "Смирнов О. И."),
        ("ПАО «ЭнергоСеть»", "energoset.ru", "Волков Д. Н."), ("ООО «ТрансЛогистик»", "translog.ru", "Орлова М. С."),
        ("АО «СибСтрой»", "sibstroy.ru", "Лебедев К. А."), ("ООО «Фарм-Снаб»", "farmsnab.ru", "Никитина А. Р.")]
TOPICS = ["сроках поставки арматуры", "переносе сроков работ", "оплате этапа", "качестве бетона", "приёмке работ",
          "графике отгрузки", "изменении цены", "претензии по качеству", "допуске на объект", "актах сверки",
          "замене оборудования", "гарантийном ремонте", "продлении договора", "страховании груза", "охране объекта"]
truth = {}   # имя файла → id переписки
base = datetime(2024, 1, 10, 9, 0)

# 1) почта: 450 переписок по 2–5 писем ≈ 1500
box = mailbox.mbox(str(out / "Почта_выгрузка.mbox")); box.lock()
n_mail = 0
for t in range(450):
    a, b = random.sample(ORGS, 2)
    topic = random.choice(TOPICS); contract = f"{random.randint(1, 60)}/2024"
    subj = f"О {topic} по договору № {contract}"
    when = base + timedelta(days=random.randint(0, 300), hours=random.randint(0, 8))
    prev_id, k = None, random.randint(2, 5)
    for i in range(k):
        frm, to = (a, b) if i % 2 == 0 else (b, a)
        m = EmailMessage()
        m["From"] = f"{frm[2]} <{frm[2].split()[0].lower()}@{frm[1]}>"; m["To"] = f"{to[2]} <info@{to[1]}>"
        # 15 % ответов — без «In-Reply-To» (пересылка из другого клиента): связь только по теме
        lost_header = prev_id and random.random() < 0.15
        m["Subject"] = ("RE: " * min(i, 2) if not lost_header else "Fwd: ") + subj if i else subj
        m["Date"] = format_datetime(when + timedelta(days=i, hours=i))
        mid = f"<t{t}.m{i}@{frm[1]}>"; m["Message-ID"] = mid
        if prev_id and not lost_header:
            m["In-Reply-To"] = prev_id; m["References"] = prev_id
        body = f"Добрый день!\nПо вопросу о {topic} сообщаем: позиция {random.randint(1, 999)} согласована, срок — {random.randint(3, 30)} дней.\n{frm[2]}"
        if prev_id:
            body += "\n\n> Предыдущее письмо цитируется здесь и не должно мешать поиску."
        m.set_content(body)
        box.add(m); n_mail += 1
        truth[f"Почта_выгрузка_{n_mail:05d}.eml"] = f"mail{t}"
        prev_id = mid
box.flush(); box.unlock(); box.close()

# 2) DOCX-письма: 230 переписок по 2–4 письма ≈ 700, связь через «на ваш исх. № … от …»
z = zipfile.ZipFile(out / "Письма_DOCX.zip", "w", zipfile.ZIP_DEFLATED)
num = {o[1]: 100 for o in ORGS}; n_docx = 0
for t in range(230):
    a, b = random.sample(ORGS, 2)
    topic = random.choice(TOPICS)
    when = base + timedelta(days=random.randint(0, 300))
    prev = None
    for i in range(random.randint(2, 4)):
        frm, to = (a, b) if i % 2 == 0 else (b, a)
        num[frm[1]] += 1
        no, date = f"{num[frm[1]]}", (when + timedelta(days=3 * i)).strftime("%d.%m.%Y")
        d = docx.Document()
        for line in [frm[0], f"Исх. № {no} от {date}", f"Генеральному директору {to[0]}", f"О {topic}",
                     (f"На ваш исх. № {prev[0]} от {prev[1]} сообщаем следующее." if prev else "Сообщаем следующее."),
                     f"Просим рассмотреть вопрос о {topic} в течение {random.randint(5, 15)} дней.",
                     f"С уважением, {frm[2]}"]:
            d.add_paragraph(line)
        buf = io.BytesIO(); d.save(buf)
        n_docx += 1; name = f"Письмо_{frm[1].split('.')[0]}_исх_{no}.docx"
        z.writestr(name, buf.getvalue()); truth[name] = f"docx{t}"
        prev = (no, date)
z.close()

# 3) .eml с вложениями: 100 переписок по 3 письма = 300, у первого письма — вложение
z = zipfile.ZipFile(out / "Почта_с_вложениями.zip", "w", zipfile.ZIP_DEFLATED)
for t in range(100):
    a, b = random.sample(ORGS, 2); topic = random.choice(TOPICS)
    when = base + timedelta(days=random.randint(0, 300)); prev_id = None
    for i in range(3):
        frm, to = (a, b) if i % 2 == 0 else (b, a)
        m = EmailMessage(); m["From"] = f"{frm[2]} <office@{frm[1]}>"; m["To"] = f"<office@{to[1]}>"
        m["Subject"] = ("Re: " if i else "") + f"Документы о {topic} (пакет {t})"
        m["Date"] = format_datetime(when + timedelta(days=i)); mid = f"<att{t}.{i}@{frm[1]}>"; m["Message-ID"] = mid
        if prev_id: m["In-Reply-To"] = prev_id
        m.set_content(f"Направляем документы о {topic}.")
        if i == 0:
            m.add_attachment(f"Акт сверки № {t} о {topic}\nСумма {random.randint(10, 999)} 000 рублей".encode(),
                             maintype="text", subtype="plain", filename=f"Акт_сверки_{t}.txt")
        name = f"письмо_{t}_{i}.eml"
        z.writestr(name, bytes(m)); truth[name] = f"att{t}"; prev_id = mid
z.close()
json.dump(truth, open(out / "truth.json", "w"), ensure_ascii=False)
print("писем:", len(truth), "почта:", n_mail, "docx:", n_docx)
