-- Backlog ageing: open tickets by age band, as of any point in time.
--
-- A ticket is "open at T" when it was created at or before T and had not yet reached its
-- end (first resolution, or cancellation) by T. Reopened tickets are treated as open
-- until their final resolution (the resolved-then-reopened gap is ignored).
--
-- f_backlog_ageing(T)          -> one snapshot for an arbitrary T
-- v_backlog_ageing_monthly     -> the same snapshot on the 1st of every month in the data
-- v_backlog_ageing_current     -> snapshot at the load's "as of" moment

CREATE OR REPLACE FUNCTION f_backlog_ageing(p_as_of timestamptz)
RETURNS TABLE (
    as_of             timestamptz,
    assignment_group  text,
    age_bucket        text,
    bucket_order      smallint,
    open_tickets      bigint,
    past_due_tickets  bigint,
    critical_or_major bigint,
    median_age_hours  numeric
)
LANGUAGE sql STABLE AS $$
    WITH open_at AS (
        SELECT
            t.assignment_group,
            t.severity,
            EXTRACT(EPOCH FROM (p_as_of - t.creation_ts)) / 3600.0 AS age_hours,
            p_as_of > t.expected_resolution_ts                       AS past_due
        FROM fact_ticket t
        WHERE t.creation_ts <= p_as_of
          AND COALESCE(t.resolution_ts,
                       CASE WHEN t.status = 'cancelled' THEN t.last_update_ts END,
                       'infinity'::timestamptz) > p_as_of
    ),
    bucketed AS (
        SELECT
            *,
            CASE
                WHEN age_hours < 24  THEN '0-24h'
                WHEN age_hours < 72  THEN '1-3d'
                WHEN age_hours < 168 THEN '3-7d'
                WHEN age_hours < 720 THEN '7-30d'
                ELSE '30d+'
            END AS age_bucket,
            CASE
                WHEN age_hours < 24  THEN 1
                WHEN age_hours < 72  THEN 2
                WHEN age_hours < 168 THEN 3
                WHEN age_hours < 720 THEN 4
                ELSE 5
            END::smallint AS bucket_order
        FROM open_at
    )
    SELECT
        p_as_of,
        assignment_group,
        age_bucket,
        bucket_order,
        count(*)                                                        AS open_tickets,
        count(*) FILTER (WHERE past_due)                                AS past_due_tickets,
        count(*) FILTER (WHERE severity IN ('critical', 'major'))       AS critical_or_major,
        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY age_hours)::numeric, 1)
                                                                        AS median_age_hours
    FROM bucketed
    GROUP BY assignment_group, age_bucket, bucket_order
$$;

CREATE OR REPLACE VIEW v_backlog_ageing_monthly AS
WITH bounds AS (
    SELECT
        date_trunc('month', min(t.creation_ts)) + interval '1 month' AS first_snapshot,
        (SELECT max(snapshot_ts) FROM load_run)                       AS last_snapshot
    FROM fact_ticket t
),
months AS (
    SELECT generate_series(first_snapshot, last_snapshot, interval '1 month') AS as_of
    FROM bounds
)
SELECT b.*
FROM months m
CROSS JOIN LATERAL f_backlog_ageing(m.as_of) b;

CREATE OR REPLACE VIEW v_backlog_ageing_current AS
SELECT *
FROM f_backlog_ageing((SELECT max(snapshot_ts) FROM load_run));
