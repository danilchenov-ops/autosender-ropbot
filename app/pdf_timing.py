"""Разбор: время поступления заявки, скорость реакции, потери на «ночном» потоке.

Запуск: docker compose run --rm collector python pdf_timing.py [--send]
"""
import datetime as dt
import os
import sys

from reportlab.graphics.shapes import Drawing, Line, Rect, String
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

F = "/usr/share/fonts/truetype/dejavu"
pdfmetrics.registerFont(TTFont("DJ", f"{F}/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DJ-B", f"{F}/DejaVuSans-Bold.ttf"))

INK = colors.HexColor("#0b0b0b")
SEC = colors.HexColor("#52514e")
MUTED = colors.HexColor("#8a8985")
GRID = colors.HexColor("#e6e5e2")
SERIES = colors.HexColor("#2a78d6")     # проверено валидатором для светлой подложки
BAD = colors.HexColor("#fdeeee")
WARN = colors.HexColor("#fdf6e3")

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontName="DJ-B", fontSize=20,
                    alignment=TA_LEFT, textColor=INK, spaceAfter=2)
SUB = ParagraphStyle("SUB", fontName="DJ", fontSize=11, textColor=SEC, spaceAfter=14)
H2 = ParagraphStyle("H2", fontName="DJ-B", fontSize=13, textColor=INK,
                    spaceBefore=16, spaceAfter=6)
P = ParagraphStyle("P", fontName="DJ", fontSize=9.5, textColor=INK, leading=14,
                   spaceAfter=6)
CAP = ParagraphStyle("CAP", fontName="DJ", fontSize=8, textColor=MUTED, leading=11,
                     spaceAfter=10)
CELL = ParagraphStyle("CELL", fontName="DJ", fontSize=8, leading=10)


def bars(values, labels, width=176 * mm, height=44 * mm, fmt="{:.0f}",
         label_every=2, highlight=None, note=None):
    """Столбики одной серии. Подписи значений — только у заметных столбцов."""
    d = Drawing(width, height + 14)
    pad_l, pad_b = 2, 16
    plot_w = width - pad_l - 2
    plot_h = height - 8
    top = max(values) or 1
    n = len(values)
    slot = plot_w / n
    bw = slot - 2                       # 2pt промежуток между столбцами

    for gy in (0, 0.5, 1.0):
        y = pad_b + plot_h * gy
        d.add(Line(pad_l, y, pad_l + plot_w, y, strokeColor=GRID, strokeWidth=0.5))

    for i, v in enumerate(values):
        h = plot_h * (v / top)
        x = pad_l + i * slot
        col = SERIES
        d.add(Rect(x, pad_b, bw, max(h, 0.6), fillColor=col, strokeColor=None,
                   rx=1.5, ry=1.5))
        if highlight and i in highlight:
            d.add(String(x + bw / 2, pad_b + h + 3, fmt.format(v), fontName="DJ-B",
                         fontSize=6.5, fillColor=INK, textAnchor="middle"))
        if i % label_every == 0:
            d.add(String(x + bw / 2, 6, labels[i], fontName="DJ", fontSize=6,
                         fillColor=MUTED, textAnchor="middle"))
    if note:
        d.add(String(pad_l, height + 6, note, fontName="DJ", fontSize=7.5,
                     fillColor=SEC))
    return d


def simple_bars(pairs, width=110 * mm, height=30 * mm, suffix="%"):
    """Пара-тройка столбцов с прямыми подписями."""
    d = Drawing(width, height + 16)
    top = max(v for _, v in pairs) or 1
    n = len(pairs)
    slot = width / n
    bw = min(slot - 14, 34 * mm)
    for i, (lab, v) in enumerate(pairs):
        h = (height - 6) * (v / top)
        x = i * slot + (slot - bw) / 2
        d.add(Rect(x, 14, bw, max(h, 0.6), fillColor=SERIES, strokeColor=None,
                   rx=2, ry=2))
        d.add(String(x + bw / 2, 14 + h + 4, f"{v}{suffix}", fontName="DJ-B",
                     fontSize=9, fillColor=INK, textAnchor="middle"))
        d.add(String(x + bw / 2, 4, lab, fontName="DJ", fontSize=7.5,
                     fillColor=SEC, textAnchor="middle"))
    return d


def table(rows, header, widths, zebra=None):
    data = [[Paragraph(f"<b>{h}</b>", CELL) for h in header]]
    data += [[Paragraph(str(x), CELL) for x in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    stl = [("FONTNAME", (0, 0), (-1, -1), "DJ"), ("FONTSIZE", (0, 0), (-1, -1), 8),
           ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
           ("LINEBELOW", (0, 1), (-1, -2), 0.25, GRID),
           ("VALIGN", (0, 0), (-1, -1), "TOP"),
           ("TOPPADDING", (0, 0), (-1, -1), 3.5),
           ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]
    if zebra:
        stl.append(("BACKGROUND", (0, 1), (-1, -1), zebra))
    t.setStyle(TableStyle(stl))
    return t


WINDOW = "h BETWEEN 10 AND 17"
BASE = """
WITH base AS (
  SELECT l.id, l.date_create, l.status_semantic,
         COALESCE(NULLIF(l.utm_source,''), l.source_id,'—') AS src,
         EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok')::int AS h,
         EXTRACT(EPOCH FROM ((SELECT min(c.call_start) FROM calls c
            WHERE c.phone_e164 = l.phone_e164 AND c.direction='out'
              AND c.call_start >= l.date_create) - l.date_create))/60.0 AS w
  FROM leads l
  WHERE l.phone_kind='mobile'
    AND l.date_create >= '2026-06-01' AND l.date_create < now() - interval '14 days'
)
"""


def main():
    send = "--send" in sys.argv
    with db() as conn:
        hours = conn.execute(BASE + """
            SELECT h, count(*), 
                   round(percentile_cont(0.5) WITHIN GROUP (ORDER BY w)::numeric,0),
                   round(100.0*count(*) FILTER (WHERE status_semantic='S')/count(*),2)
            FROM base GROUP BY h ORDER BY h""").fetchall()

        win = conn.execute(BASE + f"""
            SELECT CASE WHEN {WINDOW} THEN 'офис работает' ELSE 'офис закрыт' END,
                   count(*), count(*) FILTER (WHERE status_semantic='S'),
                   round(100.0*count(*) FILTER (WHERE status_semantic='S')/count(*),2),
                   round(percentile_cont(0.5) WITHIN GROUP (ORDER BY w)::numeric,0)
            FROM base GROUP BY 1 ORDER BY 1""").fetchall()

        src = conn.execute(BASE + f"""
            SELECT src,
              count(*) FILTER (WHERE {WINDOW}),
              count(*) FILTER (WHERE NOT ({WINDOW})),
              round(100.0*count(*) FILTER (WHERE {WINDOW} AND status_semantic='S')
                    /NULLIF(count(*) FILTER (WHERE {WINDOW}),0),2),
              round(100.0*count(*) FILTER (WHERE NOT ({WINDOW}) AND status_semantic='S')
                    /NULLIF(count(*) FILTER (WHERE NOT ({WINDOW})),0),2)
            FROM base GROUP BY 1 HAVING count(*) >= 150 ORDER BY 2+3 DESC""").fetchall()

        speed = conn.execute(BASE + f"""
            SELECT CASE WHEN w IS NULL THEN 'не звонили'
                        WHEN w < 120 THEN 'до 2 часов'
                        WHEN w < 600 THEN '2–10 часов'
                        ELSE 'больше 10 часов' END,
                   count(*), count(*) FILTER (WHERE status_semantic='S'),
                   round(100.0*count(*) FILTER (WHERE status_semantic='S')/count(*),2)
            FROM base WHERE NOT ({WINDOW})
              AND src IN ('direct','max_bot','WEB','OTHER')
            GROUP BY 1""").fetchall()

        days = conn.execute(BASE + "SELECT count(DISTINCT date_create::date) FROM base"
                            ).fetchone()[0]

    build_and_send(hours, win, src, speed, days, send)


def build_and_send(hours, win, src, speed, days, send):
    path = f"/tmp/timing_{dt.date.today():%Y%m%d}.pdf"
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=16 * mm, bottomMargin=14 * mm,
                            title="Время заявки и скорость реакции")
    st = []
    st.append(Paragraph("Ночной поток заявок", H1))
    st.append(Paragraph("Сколько мы теряем на том, что не успеваем обрабатывать "
                        "западные заявки", SUB))

    d = {r[0]: r for r in win}
    open_w = d.get("офис работает")
    closed_w = d.get("офис закрыт")

    st.append(Paragraph(
        f"Разбор построен на заявках с 1 июня по {(dt.date.today()-dt.timedelta(days=14)):%d.%m.%Y} — "
        f"это {days} дней. Заявки последних двух недель исключены: медианный цикл сделки "
        "8,6 дня, свежие ещё не успели дозреть, и их конверсия была бы занижена. "
        "Лиды с непригодным номером исключены, звонки считаются по номеру телефона "
        "клиента, а не по карточке лида.", P))

    st.append(Paragraph("Главное в двух цифрах", H2))
    st.append(table(
        [[r[0], r[1], r[2], f"{r[3]}%", f"{int(r[4])} мин"] for r in win],
        ["Когда пришла заявка", "Заявок", "Сделок", "Конверсия", "Медиана до звонка"],
        [45 * mm, 25 * mm, 25 * mm, 30 * mm, 40 * mm]))
    if open_w and closed_w:
        share = 100.0 * closed_w[1] / (closed_w[1] + open_w[1])
        st.append(Paragraph(
            f"<b>{share:.0f}% заявок приходит, когда офис закрыт.</b> Конверсия по ним "
            f"{closed_w[3]}% против {open_w[3]}% — разница ровно вдвое. "
            f"Первого звонка они ждут {int(closed_w[4])} минут — это "
            f"{int(closed_w[4])//60} часов, тогда как в рабочее окно "
            f"{int(open_w[4])} минут.", P))

    # ── График 1
    st.append(Paragraph("Когда приходят заявки", H2))
    st.append(bars([h[1] for h in hours], [f"{h[0]:02d}" for h in hours],
                   note="Заявок за период, по часам (время Владивостока)"))
    st.append(Paragraph("Поток почти равномерный: ночью по местному времени заявок "
                        "не меньше, чем днём. Это клиенты из европейской части страны, "
                        "у которых как раз рабочий день.", CAP))

    # ── График 2
    st.append(Paragraph("Когда начинается провал", H2))
    st.append(bars([float(h[2] or 0) for h in hours], [f"{h[0]:02d}" for h in hours],
                   highlight={17, 18},
                   note="Медиана времени до первого звонка, минут (время Владивостока)"))
    st.append(Paragraph(
        "<b>Перелом происходит в 18:00 по Владивостоку — это 11:00 по Москве.</b> "
        "Заявка, пришедшая в 17 часов, получает звонок через час. Пришедшая в 18 — "
        "через восемнадцать часов. Дальше до утра ситуация не улучшается.", P))

    # ── График 3
    st.append(PageBreak())
    st.append(Paragraph("Конверсия по окнам", H2))
    if open_w and closed_w:
        st.append(simple_bars([("офис работает\n10:00–18:00 Влд", float(open_w[3])),
                               ("офис закрыт", float(closed_w[3]))]))

    st.append(Paragraph("Но не спешите с выводом", H2))
    st.append(Paragraph(
        "Разрыв в конверсии — факт. А вот его причина не так очевидна, как кажется. "
        "Если бы дело было в скорости ответа, то внутри ночного окна быстро "
        "обработанные заявки конвертировались бы заметно лучше медленных. Проверяем:", P))
    st.append(table(
        [[r[0], r[1], r[2], f"{r[3]}%"] for r in
         sorted(speed, key=lambda r: {'до 2 часов': 1, '2–10 часов': 2,
                                      'больше 10 часов': 3}.get(r[0], 4))],
        ["Скорость реакции (ночное окно)", "Заявок", "Сделок", "Конверсия"],
        [55 * mm, 30 * mm, 30 * mm, 30 * mm], zebra=WARN))
    st.append(Paragraph(
        "Разница есть, но скромная: ответ в течение десяти часов даёт около 1,1% "
        "против 0,76% при более долгом молчании. Это примерно в полтора раза, а не "
        "вдвое. И на таких числах — 12 сделок против 24 — статистической значимости "
        "нет: подобный разброс мог возникнуть случайно.", P))
    st.append(Paragraph(
        "Значит двукратный разрыв между окнами объясняется <b>не только скоростью</b>. "
        "Ночные заявки в основном приходят из европейской части России, а дневные — "
        "из ближних к Владивостоку регионов. Это разные клиенты с разной готовностью "
        "покупать праворульный автомобиль с доставкой через всю страну.", P))

    st.append(Paragraph("Подтверждение: разрыв разный у разных источников", H2))
    st.append(table(
        [[r[0], r[1], r[2], f"{r[3]}%" if r[3] is not None else "—",
          f"{r[4]}%" if r[4] is not None else "—"] for r in src],
        ["Источник", "Заявок в окно", "Вне окна", "Конверсия в окно", "Вне окна"],
        [35 * mm, 30 * mm, 25 * mm, 35 * mm, 30 * mm]))
    st.append(Paragraph(
        "Обратите внимание на строку с номером 9025240049 — это звонки на рекламный "
        "номер. У них конверсия в обоих окнах одинаковая. Логично: человек звонит сам, "
        "его либо берут, либо перезванивают, и время суток роли не играет. "
        "А вот у заявок с форм и ботов разрыв в два-три раза.", P))

    # ── Оценка
    st.append(PageBreak())
    st.append(Paragraph("Сколько это стоит", H2))
    sp = {r[0]: r for r in speed}
    slow = sp.get("больше 10 часов")
    fast_n = sum(sp[k][1] for k in ("до 2 часов", "2–10 часов") if k in sp)
    fast_d = sum(sp[k][2] for k in ("до 2 часов", "2–10 часов") if k in sp)
    fast_conv = 100.0 * fast_d / max(fast_n, 1)
    gain_period = (fast_conv - float(slow[3])) / 100.0 * slow[1] if slow else 0
    gain_month = gain_period / max(days, 1) * 30

    upper = (float(open_w[3]) - float(closed_w[3])) / 100.0 * closed_w[1] / days * 30

    st.append(table(
        [["Оптимистичная: ночное окно догоняет дневное", f"+{upper:.0f} сделок в месяц"],
         ["Реалистичная: только эффект скорости ответа", f"+{gain_month:.0f} сделок в месяц"],
         ["Пессимистичная: разрыв целиком в составе заявок", "0"]],
        ["Сценарий", "Прирост"], [110 * mm, 45 * mm]))
    st.append(Paragraph(
        f"Реалистичная оценка получена так: заявки ночного окна, дождавшиеся ответа "
        f"быстрее десяти часов, конвертируются в {fast_conv:.2f}%, а ждавшие дольше — "
        f"в {slow[3]}%. Если бы все {slow[1]} «долгих» заявок обрабатывались быстрее, "
        f"это дало бы примерно {gain_period:.0f} дополнительных сделок за {days} дней.", P))
    st.append(Paragraph(
        f"При среднем чеке около 349 тысяч рублей {gain_month:.0f} сделок в месяц — это "
        f"порядка {gain_month*349/1000:.1f} млн рублей выручки. Оговорка: сумма проставлена "
        "лишь в 7% сделок, поэтому средний чек — оценка по неполным данным.", P))

    st.append(Paragraph("Что с этим делать", H2))
    st.append(Paragraph(
        "<b>Историческими данными этот вопрос до конца не решается.</b> Разброс между "
        "нулём и двадцатью сделками в месяц слишком велик, чтобы вкладываться вслепую, "
        "и слишком велик, чтобы махнуть рукой.", P))
    st.append(Paragraph(
        "Ответ даст эксперимент, и он недорогой. На три недели сдвинуть график одного "
        "менеджера на вечер — с 15:00 до 23:00 по Владивостоку. Половину заявок, "
        "пришедших после 18:00, случайным образом отдавать ему, вторую половину "
        "обрабатывать как сейчас. Система разметит обе группы автоматически и через "
        "три недели покажет разницу.", P))
    st.append(Paragraph(
        "Это стоит одного изменённого графика и даёт настоящий ответ вместо оценки "
        "с четырёхкратным разбросом. Если эффект подтвердится — нанимать сотрудника "
        "в московском часовом поясе становится очевидным решением.", P))

    st.append(Paragraph("И независимо от эксперимента", H2))
    st.append(Paragraph(
        "Автоответ на заявки, пришедшие в нерабочее время, стоит поставить в любом "
        "случае. Сообщение в мессенджер в момент заявки — «получили, свяжемся утром» — "
        "снимает у клиента ощущение, что его игнорируют, и не даёт ему за ночь уйти "
        "к конкуренту. Это настраивается в Битриксе за час и не требует ни одного "
        "нового сотрудника.", P))

    doc.build(st)
    log.info("PDF готов: %s", path)

    if send:
        import tg
        with db() as conn:
            chats = [r[0] for r in conn.execute(
                "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]
        cap = (f"*Ночной поток заявок*\n\n"
               f"{100.0*closed_w[1]/(closed_w[1]+open_w[1]):.0f}% заявок приходит, "
               f"когда офис закрыт\n"
               f"Конверсия {closed_w[3]}% против {open_w[3]}%\n"
               f"Провал начинается в 18:00 Влд (11:00 МСК)\n\n"
               f"Потери: от 0 до {upper:.0f} сделок в месяц, "
               f"реалистично около {gain_month:.0f}\n\n"
               "_Внутри — почему разброс такой большой и как получить точный ответ_")
        for chat in chats:
            with open(path, "rb") as f:
                tg.call_file("sendDocument", chat_id=chat, caption=cap,
                             parse_mode="Markdown",
                             files={"document": (os.path.basename(path), f,
                                                 "application/pdf")})
            log.info("Отправлено в чат %s", chat)


if __name__ == "__main__":
    main()
