-- Составная оценка заявки: поведение до звонка + разговоры + отклик клиента.
-- Веса подобраны на когорте июнь-июль 2026 (7 828 заявок, 89 продаж),
-- проверены перекрёстно по хешу телефона: AUC 0,856 вне выборки.

CREATE TABLE IF NOT EXISTS lead_composite (
    lead_id        bigint PRIMARY KEY,
    score          int NOT NULL,           -- 0-100, то, что уходит в CRM
    logodds        numeric NOT NULL,       -- сумма лог-шансов до перевода в баллы
    p_est          numeric,                -- оценка вероятности продажи
    crown          boolean NOT NULL DEFAULT false,
    parts          jsonb,                  -- вклад блоков, для объяснения менеджеру
    first_crown_at timestamptz,            -- когда корона загорелась впервые
    peak_score     int,                    -- максимум за жизнь заявки
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lead_composite_score ON lead_composite(score DESC);
CREATE INDEX IF NOT EXISTS idx_lead_composite_crown ON lead_composite(crown) WHERE crown;

-- Что уже отправлено в Битрикс: чтобы не дёргать API без изменений
CREATE TABLE IF NOT EXISTS lead_composite_sent (
    lead_id    bigint PRIMARY KEY,
    score      int NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now()
);

-- Балл карточки разговора (эталонный скрипт). Заполняется разбором звонка,
-- пока промт не расширен — NULL, и блок «карточка» в оценку не входит.
ALTER TABLE call_scores ADD COLUMN IF NOT EXISTS card_score numeric;
ALTER TABLE call_scores ADD COLUMN IF NOT EXISTS card jsonb;

-- Какой значок реально стоит в названии лида (⚡ ★ ♛ или пусто).
-- Раньше хватало флага fire, теперь значков три.
ALTER TABLE lead_marks ADD COLUMN IF NOT EXISTS sym text;

-- Корону ставим и заявкам, заведённым до 1 августа, если они до сих пор в работе.
-- У них нет класса до звонка, поэтому «Категория клиента» остаётся пустой.
ALTER TABLE lead_marks ALTER COLUMN value DROP NOT NULL;
