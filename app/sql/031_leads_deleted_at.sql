-- Лид, удалённый в Битриксе, списочным методом не отдаётся и у нас оставался
-- открытым навсегда (09.09.2026, панель РОПа: 19 vs 13 «Не обработан» у Томаша).
-- crm_sync.sweep_deleted_leads помечает такие: deleted_at + status_semantic 'D'.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS leads_open_alive ON leads (assigned_by)
    WHERE status_semantic = 'P' AND deleted_at IS NULL;
