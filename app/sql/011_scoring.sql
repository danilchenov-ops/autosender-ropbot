-- Скоринг заявок: поля форм и Метрики, оценки, веса.
-- Колонки считаются из raw — заполняются, когда коллектор приносит UF-поля (см. UF_LEAD в crm_sync.py).

ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS ym_client_id text GENERATED ALWAYS AS (CASE WHEN raw->>'UF_CRM_YANDEX_CL_ID' ~ '^[0-9]{6,}$' THEN raw->>'UF_CRM_YANDEX_CL_ID' WHEN raw->>'UF_CRM_1729578832' ~ '^[0-9]{6,}$' THEN raw->>'UF_CRM_1729578832' END) STORED,
  ADD COLUMN IF NOT EXISTS form_text text GENERATED ALWAYS AS (NULLIF(raw->>'UF_CRM_1622530114','')) STORED,
  ADD COLUMN IF NOT EXISTS form_model text GENERATED ALWAYS AS (NULLIF(raw->>'UF_CRM_1730191118','')) STORED,
  ADD COLUMN IF NOT EXISTS ip_city text GENERATED ALWAYS AS (NULLIF(raw->>'UF_CRM_1742283804','')) STORED,
  ADD COLUMN IF NOT EXISTS page_url text GENERATED ALWAYS AS (COALESCE(NULLIF(raw->>'UF_CRM_1730191143',''), substring(raw->>'SOURCE_DESCRIPTION' from 'https?://[^[:space:]"]+'))) STORED;

CREATE INDEX IF NOT EXISTS idx_leads_ym_client_id ON leads(ym_client_id) WHERE ym_client_id IS NOT NULL;

-- Поведение клиента на сайте ДО момента заявки (снимок из Метрики по ClientID)
CREATE TABLE IF NOT EXISTS lead_visits (
    lead_id bigint PRIMARY KEY,
    client_id text NOT NULL,
    counter_id bigint,
    visits_before int,
    days_since_first int,
    pageviews int,
    seconds int,
    distinct_landings int,
    saw_lot boolean,
    last_source text,
    device text,
    region text,
    last_campaign text,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    raw jsonb
);

-- Первая (замороженная) оценка заявки. Не обновляется — по ней меряем точность прогноза.
CREATE TABLE IF NOT EXISTS lead_scores (
    lead_id bigint NOT NULL,
    scored_at timestamptz NOT NULL DEFAULT now(),
    model_version text NOT NULL,
    score int NOT NULL,
    grade text NOT NULL,
    p_est numeric,
    reasons jsonb,
    features jsonb,
    PRIMARY KEY (lead_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_lead_scores_scored_at ON lead_scores(scored_at);

-- Веса признаков (лог-отношения шансов), пересчитываются по истории
CREATE TABLE IF NOT EXISTS score_weights (
    model_version text NOT NULL,
    factor text NOT NULL,
    value text NOT NULL,
    weight numeric NOT NULL,
    n int,
    sales int,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_version, factor, value)
);
