"""Отчёт по недозвонам: PDF + отправка руководству в Телеграм.

ВАЖНО: считаем по КЛИЕНТУ (номеру телефона), а не по карточке лида.
Один человек часто порождает несколько карточек у разных менеджеров —
отсутствие звонков по одной карточке не значит, что клиенту не звонили.

Запуск: docker compose run --rm collector python pdf_missed.py [дней] [--send]
"""
import datetime as dt
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

from common import db, log

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
pdfmetrics.registerFont(TTFont("DJ", f"{FONT_DIR}/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DJ-B", f"{FONT_DIR}/DejaVuSans-Bold.ttf"))

INK = colors.HexColor("#1a1a1a")
GREY = colors.HexColor("#666666")
LINE = colors.HexColor("#dddddd")
BAD = colors.HexColor("#fdeeee")
WARN = colors.HexColor("#fdf6e3")
CALM = colors.HexColor("#f2f4f7")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="DJ-B", fontSize=20,
                    alignment=TA_LEFT, textColor=INK, spaceAfter=2)
SUB = ParagraphStyle("SUB", fontName="DJ", fontSize=11, textColor=GREY, spaceAfter=14)
H2 = ParagraphStyle("H2", fontName="DJ-B", fontSize=13, textColor=INK,
                    spaceBefore=16, spaceAfter=6)
P = ParagraphStyle("P", fontName="DJ", fontSize=9.5, textColor=INK, leading=14,
                   spaceAfter=6)
CELL = ParagraphStyle("CELL", fontName="DJ", fontSize=7.5, leading=9.5)

SQL = """
SELECT lead_id, date_create, manager, phone, source, title,
       calls_on_lead, calls_on_client, client_leads, client_managers,
       client_untouched, worked_elsewhere
FROM v_lead_with_client
WHERE status_name ILIKE %s
  AND date_create > now() - (%s || ' days')::interval
ORDER BY calls_on_client, date_create DESC
"""


def pretty_phone(p):
    d = "".join(ch for ch in (p or "") if ch.isdigit())
    return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:]}" if len(d) == 11 else (p or "—")


def table(rows, header, widths, zebra=None):
    data = [[Paragraph(f"<b>{h}</b>", CELL) for h in header]]
    data += [[Paragraph(str(x), CELL) for x in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DJ"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    if zebra:
        style.append(("BACKGROUND", (0, 1), (-1, -1), zebra))
    t.setStyle(TableStyle(style))
    return t


def by_manager(rows):
    d = {}
    for r in rows:
        d[r[2]] = d.get(r[2], 0) + 1
    return sorted(d.items(), key=lambda kv: -kv[1])


def build(path, days, rows, fanout):
    lost = [r for r in rows if r[10]]                       # клиенту не звонили вообще
    dupes = [r for r in rows if r[6] == 0 and not r[10]]    # карточка-дубль
    tried = [r for r in rows if r[6] > 0]

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=16 * mm, bottomMargin=14 * mm, title="Недозвоны")
    st = []
    today = dt.date.today()
    since = today - dt.timedelta(days=days)

    st.append(Paragraph("Недозвоны", H1))
    st.append(Paragraph(
        f"Лиды со статусом «Недозвон», созданные с {since:%d.%m.%Y} по {today:%d.%m.%Y}", SUB))

    st.append(Paragraph(
        "<b>Как считалось.</b> Звонки учитываются по номеру телефона клиента, а не по "
        "карточке лида. Один человек часто оставляет несколько заявок, они попадают "
        "разным менеджерам, и отсутствие звонков по одной карточке не значит, что "
        "клиенту не звонили. Лиды с непригодным номером телефона исключены.", P))

    st.append(table(
        [["Всего лидов со статусом «Недозвон»", len(rows)],
         ["Клиенту не звонили ни разу", len(lost)],
         ["Карточка-дубль: клиента вели по другой заявке", len(dupes)],
         ["Попытки были", len(tried)]],
        ["Показатель", "Лидов"], [120 * mm, 30 * mm]))

    # ── 1. Настоящие потери
    st.append(PageBreak())
    st.append(Paragraph("1. Клиенту не звонили ни разу", H2))
    st.append(Paragraph(
        f"{len(lost)} человек с рабочим номером телефона: заявка есть, статус «Недозвон», "
        "исходящих звонков по номеру нет вообще — ни от кого. Это настоящие потери "
        "и готовый список на обзвон.", P))
    if lost:
        st.append(table(
            [[r[0], f"{r[1]:%d.%m %H:%M}", r[2], pretty_phone(r[3]), r[4], str(r[5])[:34]]
             for r in lost],
            ["Лид", "Создан", "Менеджер", "Телефон", "Источник", "Заявка"],
            [15 * mm, 22 * mm, 28 * mm, 30 * mm, 25 * mm, 62 * mm], zebra=BAD))
        st.append(Spacer(1, 8))
        st.append(table([[m, n] for m, n in by_manager(lost)],
                        ["Менеджер", "Потеряно"], [70 * mm, 30 * mm]))
    else:
        st.append(Paragraph("Таких клиентов нет — все получили хотя бы один звонок.", P))

    # ── 2. Дубли
    st.append(PageBreak())
    st.append(Paragraph("2. Карточки-дубли", H2))
    st.append(Paragraph(
        f"{len(dupes)} карточек без звонков, но клиента при этом вели — по другой заявке "
        "с тем же номером. К менеджеру претензий нет: он видел, что клиент уже в работе. "
        "Это вопрос к настройкам CRM, а не к людям.", P))
    if dupes:
        st.append(table(
            [[r[0], f"{r[1]:%d.%m}", r[2], pretty_phone(r[3]), r[8], r[9], r[7]]
             for r in dupes],
            ["Лид", "Создан", "Менеджер", "Телефон", "Заявок<br/>у клиента",
             "Менеджеров", "Звонков<br/>клиенту"],
            [15 * mm, 16 * mm, 30 * mm, 30 * mm, 22 * mm, 22 * mm, 22 * mm], zebra=CALM))

    # ── 3. Размножение заявок
    st.append(PageBreak())
    st.append(Paragraph("3. Почему появляются дубли", H2))
    st.append(Paragraph(
        "Одна и та же заявка нередко порождает несколько карточек за сутки, и они "
        "расходятся разным менеджерам. Данные за 90 дней:", P))
    st.append(table(
        [[k, v[0], v[1], v[2]] for k, v in fanout],
        ["Заявок с номера за сутки", "Случаев", "Всего карточек", "Менеджеров в среднем"],
        [50 * mm, 30 * mm, 35 * mm, 40 * mm]))
    st.append(Spacer(1, 8))
    st.append(Paragraph(
        "Кроме путаницы в отчётах это создаёт и прямой вред: несколько менеджеров "
        "звонят одному человеку, а клиент решает, что в компании беспорядок. "
        "Лечится склейкой заявок по номеру телефона на входе в CRM.", P))

    # ── 4. Попытки были
    st.append(PageBreak())
    st.append(Paragraph("4. Попытки были, но клиент закрыт как «Недозвон»", H2))
    dist = {}
    for r in tried:
        k = min(r[7], 5)
        dist[k] = dist.get(k, 0) + 1
    st.append(table(
        [[("5 и более" if k == 5 else str(k)), v, f"{100.0*v/max(len(tried),1):.1f}%"]
         for k, v in sorted(dist.items())],
        ["Звонков клиенту", "Лидов", "Доля"], [45 * mm, 35 * mm, 35 * mm]))
    st.append(Spacer(1, 8))
    st.append(Paragraph(
        "Норма, к которой стоит прийти, — не менее пяти попыток за три дня в разное время "
        "суток. Всё, что закрыто после одной-двух попыток, закрыто преждевременно.", P))
    st.append(Spacer(1, 6))
    if tried:
        st.append(table(
            [[r[0], f"{r[1]:%d.%m}", r[2], pretty_phone(r[3]), r[7], str(r[5])[:32]]
             for r in tried],
            ["Лид", "Создан", "Менеджер", "Телефон", "Звонков", "Заявка"],
            [15 * mm, 15 * mm, 30 * mm, 30 * mm, 16 * mm, 76 * mm], zebra=WARN))

    doc.build(st)
    return len(lost), len(dupes), len(tried)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 14
    send = "--send" in sys.argv

    with db() as conn:
        rows = conn.execute(SQL, ("%недозвон%", days)).fetchall()
        fanout = conn.execute(
            """SELECT CASE WHEN leads=2 THEN '2' WHEN leads<=4 THEN '3-4' ELSE '5+' END,
                      count(*), sum(leads), round(avg(managers),1)
               FROM v_lead_fanout WHERE day > current_date - 90
               GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        fanout = [(r[0], (r[1], r[2], r[3])) for r in fanout]

        path = f"/tmp/nedozvon_{dt.date.today():%Y%m%d}.pdf"
        lost, dupes, tried = build(path, days, rows, fanout)
        log.info("PDF готов: %s (потери %s, дубли %s, с попытками %s)",
                 path, lost, dupes, tried)

        if send:
            import tg
            chats = [r[0] for r in conn.execute(
                "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]
            caption = (
                f"*Недозвоны за {days} дней — исправленная версия*\n\n"
                f"Всего лидов со статусом: {len(rows)}\n"
                f"Клиенту не звонили ни разу: *{lost}*\n"
                f"Карточки-дубли (клиента вели): {dupes}\n"
                f"Попытки были: {tried}\n\n"
                "_В прошлой версии звонки считались по карточке лида, из-за чего дубли "
                "выглядели как потерянные клиенты. Теперь счёт по номеру телефона._")
            for chat in chats:
                with open(path, "rb") as f:
                    tg.call_file("sendDocument", chat_id=chat, caption=caption,
                                 parse_mode="Markdown",
                                 files={"document": (os.path.basename(path), f,
                                                     "application/pdf")})
                log.info("Отправлено в чат %s", chat)


if __name__ == "__main__":
    main()
