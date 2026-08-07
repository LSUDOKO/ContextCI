-- Example ContextCI output.
--
-- Input PR (migrations/007_drop_customer_id.sql):
--     ALTER TABLE analytics.orders DROP COLUMN customer_id;
--
-- DataHub said:
--     urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)
--       -> dbt model  dim_customers      (column-level lineage CONFIRMED, terms: PII)
--       -> dbt model  fct_order_revenue  (column-level lineage CONFIRMED, terms: Revenue-Critical)
--       -> dashboard  Exec Revenue Overview (owner: @data-platform)
--
-- Verdict: 🔴 critical / block. Two dbt models and one Looker dashboard read the
-- column being dropped, and both models carry sensitive glossary terms.
--
-- ContextCI committed the migration below to the PR branch so the drop can ship
-- as a two-step deprecation instead of a breaking change.

-- ---------------------------------------------------------------------------
-- Step 1 (this PR): keep old readers working.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.orders_compat AS
SELECT
    *,
    NULL::bigint AS customer_id  -- deprecated: removal scheduled, see PR #128
FROM analytics.orders;

COMMENT ON VIEW analytics.orders_compat IS
    'ContextCI compatibility shim for PR #128. Downstream owners: migrate dim_customers, '
    'fct_order_revenue and Exec Revenue Overview off analytics.orders.customer_id, then '
    'this view and the column can be removed together.';

-- ---------------------------------------------------------------------------
-- Step 1b: downstream dbt models select through the shim until they migrate.
-- models/marts/dim_customers.sql
-- ---------------------------------------------------------------------------
WITH source AS (
    SELECT * FROM {{ source('analytics', 'orders_compat') }}
)
SELECT
    -- customer_id is now NULL-filled by the shim; fall back to the replacement
    -- key so the model keeps producing rows during the deprecation window.
    COALESCE(source.customer_id, source.customer_key) AS customer_id,
    source.order_id,
    source.order_total,
    source.created_at
FROM source;

-- ---------------------------------------------------------------------------
-- Step 2 (follow-up release, once DataHub shows no downstream readers):
-- ---------------------------------------------------------------------------
-- ALTER TABLE analytics.orders DROP COLUMN customer_id;
-- DROP VIEW analytics.orders_compat;
