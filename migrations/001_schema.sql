-- Applied once by the postgres image when its data volume is empty
-- (this folder is mounted at /docker-entrypoint-initdb.d).
-- After changing this file: docker compose down -v && docker compose up -d

CREATE TABLE events (
    -- The 12 fields from the brief
    event_id      uuid        PRIMARY KEY,  -- dedup rule (constraint 1): a retried event can't be stored twice
    tenant_id     text        NOT NULL,
    actor_id      text        NOT NULL,
    actor_type    text        NOT NULL CHECK (actor_type IN ('user', 'service', 'api_key')),
    actor_email   text,
    action        text        NOT NULL CHECK (action IN (
                      'user.login', 'user.logout', 'user.password_reset', 'user.mfa_enabled',
                      'user.invite', 'user.role_change', 'user.delete', 'api_key.create',
                      'api_key.revoke', 'invoice.create', 'invoice.update', 'invoice.delete',
                      'invoice.export', 'payment.refund', 'project.create', 'project.update',
                      'project.delete', 'document.view', 'document.download', 'document.share',
                      'settings.update', 'webhook.create')),
    resource_type text        NOT NULL CHECK (resource_type IN (
                      'user', 'api_key', 'invoice', 'payment', 'project', 'document', 'settings', 'webhook')),
    resource_id   text        NOT NULL,
    result        text        NOT NULL CHECK (result IN ('success', 'failure')),
    ip            inet        NOT NULL,
    metadata      jsonb       NOT NULL,
    occurred_at   timestamptz NOT NULL,

    -- Our own columns
    received_at   timestamptz NOT NULL DEFAULT now(),        -- when we stored it
    ingest_seq    bigint      GENERATED ALWAYS AS IDENTITY,  -- arrival order; List API snapshots use it (constraint 3)

    -- Brief: actor_email is set only when actor_type is user
    CONSTRAINT events_email_only_for_users CHECK (actor_email IS NULL OR actor_type = 'user')
);

-- List API with no filter or only a time range: one tenant, newest first, ties broken by event_id
CREATE INDEX events_tenant_time_idx
    ON events (tenant_id, occurred_at DESC, event_id DESC);

-- List API with the action filter (with or without a time range)
CREATE INDEX events_tenant_action_time_idx
    ON events (tenant_id, action, occurred_at DESC, event_id DESC);


-- Summary API: events per action per UTC day, kept up to date by every write
CREATE TABLE daily_action_counts (
    tenant_id text   NOT NULL,
    day       date   NOT NULL,  -- UTC date of occurred_at
    action    text   NOT NULL,
    count     bigint NOT NULL,
    PRIMARY KEY (tenant_id, day, action)  -- Summary reads one tenant's days; writes upsert by this key
);

-- Summary API: find the newest day across all tenants (the 30-day window's anchor)
CREATE INDEX daily_action_counts_day_idx
    ON daily_action_counts (day);
