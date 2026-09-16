-- Customer breach-rate trend, month over month, and the customers most at risk.
--
-- v_customer_risk_trend: one row per customer x month with the month's breach rate, the
-- previous month's (LAG), the change in percentage points, a 3-month rolling breach rate
-- (window frame over the customer's own months) and the customer's rank within the month.
--
-- v_top_customers_at_risk: the ten highest rolling-3-month breach rates in the latest
-- complete month (the month before the snapshot month, so that every ticket has an outcome),
-- restricted to customers with at least 20 outcome-bearing tickets in the window.

CREATE OR REPLACE VIEW v_customer_risk_trend AS
WITH monthly AS (
    SELECT
        t.customer_id,
        date_trunc('month', t.creation_ts)::date               AS month,
        count(*)                                                AS tickets,
        count(*) FILTER (WHERE t.sla_breached IS NOT NULL)      AS tickets_with_outcome,
        count(*) FILTER (WHERE t.sla_breached)                  AS breached_tickets
    FROM fact_ticket t
    WHERE t.status <> 'cancelled'
    GROUP BY t.customer_id, date_trunc('month', t.creation_ts)
),
rates AS (
    SELECT
        m.*,
        c.customer_name,
        c.tier,
        c.industry,
        round(100.0 * m.breached_tickets / NULLIF(m.tickets_with_outcome, 0), 2)
                                                                AS breach_rate_pct,
        sum(m.breached_tickets)      OVER w3                    AS breached_3m,
        sum(m.tickets_with_outcome)  OVER w3                    AS outcome_3m,
        lag(m.breached_tickets)      OVER w1                    AS prev_breached,
        lag(m.tickets_with_outcome)  OVER w1                    AS prev_outcome
    FROM monthly m
    JOIN dim_customer c USING (customer_id)
    WINDOW
        w1 AS (PARTITION BY m.customer_id ORDER BY m.month),
        w3 AS (PARTITION BY m.customer_id ORDER BY m.month
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
),
shaped AS (
    SELECT
        customer_id,
        customer_name,
        tier,
        industry,
        month,
        tickets,
        tickets_with_outcome,
        breached_tickets,
        breach_rate_pct,
        round(100.0 * prev_breached / NULLIF(prev_outcome, 0), 2)    AS prev_month_breach_rate_pct,
        round(100.0 * breached_3m / NULLIF(outcome_3m, 0), 2)        AS rolling_3m_breach_rate_pct,
        outcome_3m                                                   AS rolling_3m_tickets_with_outcome
    FROM rates
)
SELECT
    *,
    breach_rate_pct - prev_month_breach_rate_pct                     AS mom_change_pp,
    rank() OVER (PARTITION BY month
                 ORDER BY rolling_3m_breach_rate_pct DESC NULLS LAST) AS risk_rank_in_month
FROM shaped;

CREATE OR REPLACE VIEW v_top_customers_at_risk AS
WITH latest_complete AS (
    SELECT (date_trunc('month', max(snapshot_ts)) - interval '1 month')::date AS month
    FROM load_run
)
SELECT
    r.customer_id,
    r.customer_name,
    r.tier,
    r.industry,
    r.month,
    r.rolling_3m_breach_rate_pct,
    r.rolling_3m_tickets_with_outcome,
    r.breach_rate_pct                                                AS latest_month_breach_rate_pct,
    r.prev_month_breach_rate_pct,
    r.mom_change_pp,
    CASE
        WHEN r.mom_change_pp >  2 THEN 'worsening'
        WHEN r.mom_change_pp < -2 THEN 'improving'
        ELSE 'flat'
    END                                                              AS trend,
    rank() OVER (ORDER BY r.rolling_3m_breach_rate_pct DESC)          AS risk_rank
FROM v_customer_risk_trend r
JOIN latest_complete lc ON r.month = lc.month
WHERE r.rolling_3m_tickets_with_outcome >= 20
ORDER BY risk_rank
LIMIT 10;
