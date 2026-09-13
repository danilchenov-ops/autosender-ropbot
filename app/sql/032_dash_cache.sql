-- Кэш тяжёлых фрагментов разметки панелей (web/dash.py, 09.09.2026).
-- Первый жилец — «Воронка по менеджерам» на дашборде менеджера: шесть окон
-- считаются ~26 с, а дашборд собирается каждую минуту; фрагмент живёт 14 мин.
CREATE TABLE IF NOT EXISTS dash_cache (
    key      text PRIMARY KEY,
    html     text NOT NULL,
    built_at timestamptz NOT NULL
);
