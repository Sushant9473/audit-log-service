-- Second layer for constraint 4: a tenant never sees another tenant's events.
--
-- Every read query in the API already filters on tenant_id. Row-level security makes the
-- database enforce it as well: read requests run as tenant_reader with app.tenant_id set for
-- their transaction, so a query that forgot its tenant filter still only sees that tenant.
-- Writes (Ingest API, load script) run as the table owner, which these policies don't apply
-- to, because one ingest batch may hold events for several tenants.

-- Roles are shared by every database in the server (the tests' audit_test database too),
-- so create it only if it doesn't exist yet.
DO $$
BEGIN
    CREATE ROLE tenant_reader NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $$;

GRANT SELECT ON events, daily_action_counts TO tenant_reader;
GRANT SELECT ON SEQUENCE events_ingest_seq_seq TO tenant_reader;  -- List API snapshot

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_action_counts ENABLE ROW LEVEL SECURITY;

-- current_setting(..., true) is NULL when app.tenant_id isn't set: a reader without a tenant sees nothing
CREATE POLICY tenant_reads ON events FOR SELECT TO tenant_reader
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_reads ON daily_action_counts FOR SELECT TO tenant_reader
    USING (tenant_id = current_setting('app.tenant_id', true));

-- The Summary window ends on the newest day across ALL tenants (the brief's rule), which a
-- tenant_reader can't see. This function runs with its owner's rights and returns only that date.
CREATE FUNCTION newest_event_day() RETURNS date
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
    AS $$ SELECT max(day) FROM daily_action_counts $$;
REVOKE ALL ON FUNCTION newest_event_day() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION newest_event_day() TO tenant_reader;
