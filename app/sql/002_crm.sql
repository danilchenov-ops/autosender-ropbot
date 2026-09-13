-- CRM: лиды, сделки, контакты, компании, история стадий, справочники

CREATE TABLE IF NOT EXISTS crm_dict (
    kind       TEXT NOT NULL,      -- STATUS / SOURCE / DEAL_STAGE_<cat> / DEAL_TYPE ...
    status_id  TEXT NOT NULL,
    name       TEXT,
    sort       INTEGER,
    semantics  TEXT,
    PRIMARY KEY (kind, status_id)
);

CREATE TABLE IF NOT EXISTS deal_categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT,
    sort  INTEGER
);

CREATE TABLE IF NOT EXISTS leads (
    id              BIGINT PRIMARY KEY,
    title           TEXT,
    status_id       TEXT,
    status_semantic TEXT,
    source_id       TEXT,
    assigned_by     INTEGER,
    date_create     TIMESTAMPTZ,
    date_modify     TIMESTAMPTZ,
    date_closed     TIMESTAMPTZ,
    opportunity     NUMERIC,
    currency        TEXT,
    contact_id      BIGINT,
    company_id      BIGINT,
    phone           TEXT,
    email           TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    utm_content     TEXT,
    utm_term        TEXT,
    raw             JSONB,
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leads_created  ON leads (date_create DESC);
CREATE INDEX IF NOT EXISTS idx_leads_assigned ON leads (assigned_by);
CREATE INDEX IF NOT EXISTS idx_leads_utm      ON leads (utm_source, utm_campaign);
CREATE INDEX IF NOT EXISTS idx_leads_phone    ON leads (phone);

CREATE TABLE IF NOT EXISTS deals (
    id              BIGINT PRIMARY KEY,
    title           TEXT,
    category_id     INTEGER,
    stage_id        TEXT,
    stage_semantic  TEXT,
    assigned_by     INTEGER,
    date_create     TIMESTAMPTZ,
    date_modify     TIMESTAMPTZ,
    begindate       TIMESTAMPTZ,
    closedate       TIMESTAMPTZ,
    closed          BOOLEAN,
    opportunity     NUMERIC,
    currency        TEXT,
    lead_id         BIGINT,
    contact_id      BIGINT,
    company_id      BIGINT,
    source_id       TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    utm_content     TEXT,
    utm_term        TEXT,
    raw             JSONB,
    synced_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deals_created  ON deals (date_create DESC);
CREATE INDEX IF NOT EXISTS idx_deals_assigned ON deals (assigned_by);
CREATE INDEX IF NOT EXISTS idx_deals_stage    ON deals (category_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_deals_utm      ON deals (utm_source, utm_campaign);

CREATE TABLE IF NOT EXISTS contacts (
    id           BIGINT PRIMARY KEY,
    full_name    TEXT,
    phones       TEXT[],
    emails       TEXT[],
    company_id   BIGINT,
    assigned_by  INTEGER,
    date_create  TIMESTAMPTZ,
    date_modify  TIMESTAMPTZ,
    raw          JSONB
);
CREATE INDEX IF NOT EXISTS idx_contacts_phones ON contacts USING GIN (phones);

CREATE TABLE IF NOT EXISTS companies (
    id           BIGINT PRIMARY KEY,
    title        TEXT,
    phones       TEXT[],
    assigned_by  INTEGER,
    date_create  TIMESTAMPTZ,
    date_modify  TIMESTAMPTZ,
    raw          JSONB
);

-- Движение по стадиям: главный источник правды о том, где умирают сделки
CREATE TABLE IF NOT EXISTS stage_history (
    id            BIGINT PRIMARY KEY,
    entity_kind   TEXT,          -- lead / deal
    owner_id      BIGINT,
    category_id   INTEGER,
    stage_id      TEXT,
    stage_semantic TEXT,
    created_time  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_stagehist_owner ON stage_history (entity_kind, owner_id, created_time);

-- Дела: звонки, письма, встречи, сообщения открытых линий
CREATE TABLE IF NOT EXISTS activities (
    id             BIGINT PRIMARY KEY,
    owner_type_id  INTEGER,
    owner_id       BIGINT,
    type_id        INTEGER,
    provider_id    TEXT,
    provider_type  TEXT,
    direction      INTEGER,
    subject        TEXT,
    created        TIMESTAMPTZ,
    end_time       TIMESTAMPTZ,
    completed      BOOLEAN,
    responsible_id INTEGER,
    associated_entity_id BIGINT,
    raw            JSONB
);
CREATE INDEX IF NOT EXISTS idx_act_owner    ON activities (owner_type_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_act_provider ON activities (provider_id);
CREATE INDEX IF NOT EXISTS idx_act_created  ON activities (created DESC);

-- ── Представления ────────────────────────────────────────────────────────────

-- Сколько времени сделка провела на каждой стадии
CREATE OR REPLACE VIEW v_stage_durations AS
SELECT
    h.entity_kind,
    h.owner_id,
    h.category_id,
    h.stage_id,
    d.name AS stage_name,
    h.created_time AS entered_at,
    lead(h.created_time) OVER (PARTITION BY h.entity_kind, h.owner_id ORDER BY h.created_time) AS left_at,
    EXTRACT(EPOCH FROM (
        lead(h.created_time) OVER (PARTITION BY h.entity_kind, h.owner_id ORDER BY h.created_time)
        - h.created_time
    )) / 86400.0 AS days_on_stage
FROM stage_history h
LEFT JOIN crm_dict d ON d.status_id = h.stage_id;

-- Воронка по источникам: от лида до денег
CREATE OR REPLACE VIEW v_source_funnel AS
SELECT
    COALESCE(NULLIF(l.utm_source, ''), l.source_id, 'не указан')  AS source,
    NULLIF(l.utm_campaign, '')                                    AS campaign,
    date_trunc('month', l.date_create)                            AS month,
    count(*)                                                      AS leads,
    count(*) FILTER (WHERE l.status_semantic = 'S')               AS converted,
    count(*) FILTER (WHERE l.status_semantic = 'F')               AS lost,
    count(DISTINCT d.id)                                          AS deals,
    count(DISTINCT d.id) FILTER (WHERE d.stage_semantic = 'S')    AS deals_won,
    COALESCE(sum(d.opportunity) FILTER (WHERE d.stage_semantic = 'S'), 0) AS revenue
FROM leads l
LEFT JOIN deals d ON d.lead_id = l.id
GROUP BY 1, 2, 3;

-- Звонки, привязанные к сделкам: качество разговора против денег
CREATE OR REPLACE VIEW v_call_deal AS
SELECT
    c.id            AS call_id,
    c.call_start,
    c.direction,
    c.duration,
    c.portal_user_id,
    mg.full_name    AS manager,
    d.id            AS deal_id,
    d.stage_id,
    d.stage_semantic,
    d.opportunity,
    m.manager_talk_ratio,
    m.monologue_flag,
    s.score,
    s.outcome,
    s.next_step_dated
FROM calls c
LEFT JOIN managers    mg ON mg.portal_user_id = c.portal_user_id
LEFT JOIN deals       d  ON c.crm_entity_type = 'DEAL' AND d.id = c.crm_entity_id
LEFT JOIN call_metrics m ON m.call_id = c.id
LEFT JOIN call_scores  s ON s.call_id = c.id;
