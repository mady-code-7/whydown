-- WhyDown database schema (spec section 9).
--
-- Applied by `python -m app.db init`, which runs this script in a single
-- transaction. Every object uses IF NOT EXISTS, so the script is idempotent
-- and can be run again at any time (for example on every deployment).
--
-- The script contains no BEGIN/COMMIT: the caller owns the transaction, and
-- psql runs the statements in autocommit mode.

-- Sites table
CREATE TABLE IF NOT EXISTS sites (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    expected_status INTEGER DEFAULT 200,
    keyword TEXT,
    slow_threshold_ms INTEGER DEFAULT 5000,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index for active sites
CREATE INDEX IF NOT EXISTS idx_sites_enabled ON sites (enabled);

-- Checks table
CREATE TABLE IF NOT EXISTS checks (
    id BIGSERIAL PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    checked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    status_code INTEGER,
    result TEXT NOT NULL,
    failed_stage TEXT,
    dns_ms INTEGER,
    tcp_ms INTEGER,
    tls_ms INTEGER,
    ttfb_ms INTEGER,
    total_ms INTEGER,
    resolved_ip TEXT,
    cert_expires_at TIMESTAMP WITH TIME ZONE,
    -- Flag of spec section 8.3.1: the certificate expires within the warning
    -- period. It is a flag, not a result code, so the check itself still
    -- counts as OK/SLOW.
    cert_expiring_soon BOOLEAN DEFAULT FALSE,
    error_detail TEXT
);

-- Index for dashboard queries
CREATE INDEX IF NOT EXISTS idx_checks_site_checked ON checks (site_id, checked_at DESC);

-- Incidents table
CREATE TABLE IF NOT EXISTS incidents (
    id SERIAL PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ended_at TIMESTAMP WITH TIME ZONE,
    failure_type TEXT NOT NULL,
    consecutive_failures INTEGER DEFAULT 1
);

-- Index for open incidents
CREATE INDEX IF NOT EXISTS idx_incidents_site_open ON incidents (site_id, ended_at)
    WHERE ended_at IS NULL;

-- Alert log table
CREATE TABLE IF NOT EXISTS alert_log (
    id SERIAL PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    channel TEXT NOT NULL,
    success BOOLEAN DEFAULT TRUE
);

-- Index for recent alerts
CREATE INDEX IF NOT EXISTS idx_alert_log_site_sent ON alert_log (site_id, sent_at DESC);
