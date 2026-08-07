<!-- contextci-blast-report -->
## ContextCI — Schema Change Blast Radius

🔴 **Blocked** — this change breaks downstream assets.

Overall risk: 🔴 Critical · 1 schema change(s) analyzed

| Change | Table | Column | Risk | Downstream | Verdict |
| --- | --- | --- | --- | --- | --- |
| drop column | `analytics.orders` | `customer_id` | 🔴 Critical | 3 | block |

### 🔴 Critical — drop column on `analytics.orders.customer_id`

Dropping `customer_id` breaks 2 dbt models and 1 Looker dashboard. Both models carry sensitive glossary terms, and the dashboard will render empty rather than error.

<sub>Found in `migrations/007_drop_customer_id.sql`:4</sub>

<details><summary>Blast radius — 3 downstream asset(s)</summary>

| Asset | Type | Column-level | Owners | Terms |
| --- | --- | --- | --- | --- |
| dim_customers | dataset | ✅ confirmed | Data Platform | PII |
| fct_order_revenue | dataset | ✅ confirmed | Analytics Eng | Revenue-Critical |
| Exec Revenue Overview | dashboard | ⚠️ table-level only | Data Platform | — |

</details>

<details><summary>Suggested migrations (1)</summary>

**`migrations/compat_analytics_orders_customer_id.sql`** — Compatibility view keeping `customer_id` readable for dim_customers, fct_order_revenue while they migrate. Drop the column in a follow-up release.

```sql
CREATE OR REPLACE VIEW analytics.orders_compat AS
SELECT
    *,
    NULL AS customer_id  -- deprecated, removal scheduled
FROM analytics.orders;
```

</details>

<details><summary>Reasoning</summary>

Column-level lineage confirms two dbt models read `customer_id` directly. `fct_order_revenue` is tagged Revenue-Critical and feeds the executive dashboard, so a silent NULL would surface as wrong revenue numbers rather than a failure. Staging the drop behind a compatibility view lets the downstream owners migrate on their own schedule.

</details>

### Downstream owners
Owners of the affected assets, from DataHub:

- **Data Platform**
- **Analytics Eng**

_Set the `CONTEXTCI_MENTION_OWNERS` variable to `true` to @-mention them once you have confirmed the DataHub owner names match GitHub handles._

---
<sub>Posted by [ContextCI](https://github.com/LSUDOKO/ContextCI) — context-aware CI, zero breaking changes. Affected datasets have been tagged in DataHub.</sub>
