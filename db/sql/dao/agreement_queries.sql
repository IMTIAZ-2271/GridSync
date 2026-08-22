-- Net-metering agreement DAO. See db/sql/dao/site_queries.sql for the loader
-- convention.


-- name: list_pending_agreements
SELECT nma.agreement_id,
       nma.site_id,
       s.label AS site_label,
       s.district,
       a.full_name AS account_name,
       nma.billing_device_id,
       d.serial_no AS billing_device_serial,
       nma.approval_ref,
       nma.sanctioned_capacity_kw,
       nma.export_cap_pct,
       nma.settlement_type,
       nma.credit_rollover_months,
       nma.effective_from,
       nma.effective_to,
       nma.status,
       nma.created_at
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN account a ON a.account_id = s.account_id
JOIN device d ON d.device_id = nma.billing_device_id
WHERE nma.status = 'pending'
ORDER BY nma.created_at;


-- name: get_agreement
SELECT nma.agreement_id,
       nma.site_id,
       s.label AS site_label,
       s.district,
       a.full_name AS account_name,
       nma.billing_device_id,
       d.serial_no AS billing_device_serial,
       nma.approval_ref,
       nma.sanctioned_capacity_kw,
       nma.export_cap_pct,
       nma.settlement_type,
       nma.credit_rollover_months,
       nma.effective_from,
       nma.effective_to,
       nma.status,
       nma.created_at
FROM net_metering_agreement nma
JOIN site s ON s.site_id = nma.site_id
JOIN account a ON a.account_id = s.account_id
JOIN device d ON d.device_id = nma.billing_device_id
WHERE nma.agreement_id = $1;


-- name: decide_agreement
-- Guarded on status = 'pending' so two reviewers racing on the same agreement
-- cannot both win: the loser updates zero rows and the handler answers 409.
UPDATE net_metering_agreement
SET status = $2::nma_status
WHERE agreement_id = $1
  AND status = 'pending'
RETURNING agreement_id;
