-- Post-restore checks: schema version, row counts, and that row-level security survived.
\echo 'schema version:'
SELECT version_num FROM alembic_version;

\echo 'row counts:'
SELECT 'orgs' AS t, count(*) FROM orgs
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'projects', count(*) FROM projects
UNION ALL SELECT 'traces', count(*) FROM traces
UNION ALL SELECT 'spans', count(*) FROM spans
UNION ALL SELECT 'datasets', count(*) FROM datasets
UNION ALL SELECT 'experiments', count(*) FROM experiments;

\echo 'tables with org_id but WITHOUT forced row-level security (must be empty):'
SELECT c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND EXISTS (SELECT 1 FROM information_schema.columns col WHERE col.table_name = c.relname AND col.column_name = 'org_id')
  AND NOT (c.relrowsecurity AND c.relforcerowsecurity);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND EXISTS (SELECT 1 FROM information_schema.columns col WHERE col.table_name = c.relname AND col.column_name = 'org_id')
      AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
  ) THEN
    RAISE EXCEPTION 'row-level security missing after restore';
  END IF;
END $$;
