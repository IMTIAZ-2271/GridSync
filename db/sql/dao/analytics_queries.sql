-- Analytics DAO. See db/sql/dao/site_queries.sql for the loader convention and
-- the billing-role/generation invariants this rollup relies on.


-- name: analytics_by_area
-- Rollup by district over all recorded telemetry.
--
-- The per-site totals are computed in a lateral and only then grouped, so a
-- district's site_count counts sites, not reading rows -- and a site that has
-- never reported still counts, with zeros.
--
-- $1 optionally narrows to one district, which is how government requirement 2
-- ("monitor consumption within their own region") is served without breaking
-- requirement 4 ("observe total overall power usage"). Those two are the same
-- endpoint asking different questions, so the scope is a parameter rather than
-- a hard filter applied to the role: an official passes their own district for
-- the first, and passes nothing for the second. Hard-scoping the endpoint to
-- the caller's district would have satisfied 2 by breaking 4.
SELECT s.district,
       COUNT(*) AS site_count,
       COUNT(*) FILTER (WHERE t.has_solar) AS solar_site_count,
       COALESCE(SUM(t.import_kwh), 0)::numeric(12,4) AS total_import_kwh,
       COALESCE(SUM(t.export_kwh), 0)::numeric(12,4) AS total_export_kwh,
       COALESCE(SUM(t.generation_kwh), 0)::numeric(12,4) AS total_generation_kwh
FROM site s
CROSS JOIN LATERAL (
    SELECT COALESCE(SUM(r.import_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS import_kwh,
           COALESCE(SUM(r.export_kwh) FILTER (WHERE ms.billing_role = 'billing'), 0)::numeric(12,4) AS export_kwh,
           COALESCE(SUM(r.generation_kwh), 0)::numeric(12,4)                                        AS generation_kwh,
           EXISTS (
               SELECT 1 FROM solar_array sa
               WHERE sa.site_id = s.site_id
                 AND sa.status <> 'decommissioned'
           ) AS has_solar
    FROM device_reading r
    JOIN device d ON d.device_id = r.device_id
    LEFT JOIN meter_spec ms ON ms.device_id = d.device_id
    WHERE d.site_id = s.site_id
) t
WHERE ($1::text IS NULL OR s.district = $1)
GROUP BY s.district
ORDER BY s.district;
