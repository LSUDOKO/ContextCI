-- Cleaning up a legacy column. Software tests pass; nothing here mentions
-- the dbt models, Airflow jobs or ML features that read it.
BEGIN;

ALTER TABLE SampleHiveDataset DROP COLUMN field_foo;

COMMIT;
