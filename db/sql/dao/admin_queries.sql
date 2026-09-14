-- Admin DAO: what the admin panel reads, and the few account changes it makes.
--
-- Every statement here is reached only through `require_role("admin")`, and
-- every write is paired with an `insert_audit_entry` in the same transaction by
-- the route (services/api/routes_admin_accounts.py). The writes are guarded on
-- the value the admin was looking at, so two admins acting on one account
-- produce one change and a 409, never a silent overwrite.


-- name: insert_audit_entry
-- The trail is append-only (migration a7c3e9f15b20); this is its only writer
-- outside scripts/create_admin.py.
INSERT INTO audit_log (
    actor_account_id, action, entity_type, entity_id,
    before_state, after_state, client_ip
)
VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::inet)
RETURNING audit_id;


-- name: admin_overview_accounts
-- Accounts per role and status, for the overview tiles.
SELECT role::text AS role, status::text AS status, count(*)::int AS n
FROM account
GROUP BY role, status;


-- name: admin_overview_pending
-- The work waiting on an official somewhere, across every district. An admin
-- is unscoped, so these are the whole system's queues, not one district's.
SELECT
    (SELECT count(*) FROM worker_profile   WHERE approval_status = 'pending')::int
        AS pending_workers,
    (SELECT count(*) FROM supplier_profile WHERE approval_status = 'pending')::int
        AS pending_suppliers,
    (SELECT count(*) FROM meter_application
      WHERE status IN ('submitted', 'under_review'))::int
        AS open_meter_applications,
    (SELECT count(*) FROM net_metering_agreement WHERE status = 'pending')::int
        AS pending_agreements;


-- name: admin_accounts
-- One page of accounts. $1 free text (email, name, phone or National ID,
-- case-insensitive substring), $2 role, $3 status, $4 limit, $5 offset.
--
-- `district` is whichever the account's role carries -- a worker's region, an
-- installer's registered district, an official's district -- and NULL for a
-- household and an admin. `total` rides along on every row (window count) so
-- the page and its pager can never disagree.
SELECT a.account_id,
       a.email::text                AS email,
       a.full_name,
       a.phone,
       a.national_id,
       a.role::text                 AS role,
       a.status::text               AS status,
       a.created_at,
       a.sessions_valid_after,
       COALESCE(wp.service_district, sp.service_district, gp.district) AS district,
       COALESCE(wp.approval_status, sp.approval_status)::text          AS approval_status,
       (SELECT count(*) FROM site s WHERE s.account_id = a.account_id)::int AS site_count,
       count(*) OVER ()::int        AS total
FROM account a
LEFT JOIN worker_profile     wp ON wp.account_id = a.account_id
LEFT JOIN supplier_profile   sp ON sp.account_id = a.account_id
LEFT JOIN government_profile gp ON gp.account_id = a.account_id
WHERE ($1::text IS NULL
       OR a.email::text  ILIKE '%' || $1 || '%'
       OR a.full_name    ILIKE '%' || $1 || '%'
       OR a.phone        ILIKE '%' || $1 || '%'
       OR a.national_id  ILIKE '%' || $1 || '%')
  AND ($2::text IS NULL OR a.role::text = $2)
  AND ($3::text IS NULL OR a.status::text = $3)
ORDER BY a.created_at DESC, a.account_id
LIMIT $4 OFFSET $5;


-- name: admin_account
-- One account, in the same shape as a row of admin_accounts. Never selects
-- password_hash: nothing an admin sees needs it.
SELECT a.account_id,
       a.email::text                AS email,
       a.full_name,
       a.phone,
       a.national_id,
       a.role::text                 AS role,
       a.status::text               AS status,
       a.created_at,
       a.sessions_valid_after,
       COALESCE(wp.service_district, sp.service_district, gp.district) AS district,
       COALESCE(wp.approval_status, sp.approval_status)::text          AS approval_status,
       (SELECT count(*) FROM site s WHERE s.account_id = a.account_id)::int AS site_count,
       1                            AS total
FROM account a
LEFT JOIN worker_profile     wp ON wp.account_id = a.account_id
LEFT JOIN supplier_profile   sp ON sp.account_id = a.account_id
LEFT JOIN government_profile gp ON gp.account_id = a.account_id
WHERE a.account_id = $1;


-- name: admin_account_sites
-- The sites an account owns, for its detail view.
SELECT s.site_id, s.label, s.district, s.status::text AS status,
       (SELECT count(*) FROM billing_point bp WHERE bp.site_id = s.site_id)::int
           AS connection_count
FROM site s
WHERE s.account_id = $1
ORDER BY s.label;


-- name: lock_active_admins
-- Every active admin, locked, so a change that could leave none is decided
-- against a count nobody else can move until this transaction ends. Two admins
-- demoting each other concurrently serialize here: the second re-reads the
-- first's committed change and sees one admin left.
SELECT account_id
FROM account
WHERE role = 'admin' AND status = 'active'
ORDER BY account_id
FOR UPDATE;


-- name: admin_set_account_status
-- Guarded on the status the admin saw ($3). Any status change ends every
-- session the account holds: a reactivated account starts clean too, rather
-- than reviving a token issued before its suspension. clock_timestamp(), not
-- now(): the cut-off is the moment of this statement, not of the transaction's
-- start.
UPDATE account
SET status = $2::account_status,
    sessions_valid_after = clock_timestamp(),
    updated_at = now()
WHERE account_id = $1
  AND status = $3::account_status
RETURNING account_id;


-- name: admin_revoke_sessions
UPDATE account
SET sessions_valid_after = clock_timestamp(),
    updated_at = now()
WHERE account_id = $1
RETURNING sessions_valid_after;


-- name: admin_set_password
-- A temporary password, set by an admin. Ends every existing session.
UPDATE account
SET password_hash = $2,
    sessions_valid_after = clock_timestamp(),
    updated_at = now()
WHERE account_id = $1
RETURNING account_id;


-- name: admin_set_role
-- Only between 'consumer' and 'admin' ($3 is the role it must currently have).
-- Every other role rests on a profile row with real data; the route refuses
-- them before this runs. A token carries its role, so the change ends sessions.
UPDATE account
SET role = $2::account_role,
    sessions_valid_after = clock_timestamp(),
    updated_at = now()
WHERE account_id = $1
  AND role = $3::account_role
RETURNING account_id;


-- name: admin_audit
-- One page of the trail, newest first. $1 actor, $2 entity_type, $3 entity_id,
-- $4 action, $5 limit, $6 offset. The actor's email is joined for display and
-- NULL for a bootstrap row or an actor since deleted.
SELECT l.audit_id,
       l.occurred_at,
       l.actor_account_id,
       actor.email::text     AS actor_email,
       actor.full_name       AS actor_name,
       l.action,
       l.entity_type,
       l.entity_id,
       -- What the entity is called, when it is an account: an admin reading the
       -- trail wants "consumer8@demo.com", not a UUID. NULL for other entities
       -- and for an account since deleted.
       CASE WHEN l.entity_type = 'account' THEN (
           SELECT subject.email::text FROM account subject
           WHERE subject.account_id::text = l.entity_id
       ) END                 AS entity_label,
       l.before_state::text  AS before_state,
       l.after_state::text   AS after_state,
       host(l.client_ip)     AS client_ip,
       count(*) OVER ()::int AS total
FROM audit_log l
LEFT JOIN account actor ON actor.account_id = l.actor_account_id
WHERE ($1::uuid IS NULL OR l.actor_account_id = $1)
  AND ($2::text IS NULL OR l.entity_type = $2)
  AND ($3::text IS NULL OR l.entity_id = $3)
  AND ($4::text IS NULL OR l.action = $4)
ORDER BY l.audit_id DESC
LIMIT $5 OFFSET $6;


-- name: bootstrap_find_account
-- scripts/create_admin.py: who, if anyone, already holds this email.
SELECT account_id, role::text AS role
FROM account
WHERE email = $1::citext
FOR UPDATE;


-- name: bootstrap_create_admin
INSERT INTO account (email, password_hash, full_name, role)
VALUES ($1::citext, $2, $3, 'admin')
RETURNING account_id;


-- ---------------------------------------------------------------------------
-- Data browser (services/api/admin_browse.py). Catalog reads only: these
-- describe what exists, and every identifier the browser later puts into SQL
-- comes from `quote_ident` here -- never from the request.
-- ---------------------------------------------------------------------------


-- name: admin_browse_tables
-- Every ordinary or partitioned table in public, excluding partitions (they
-- are their parent, seen from inside). approx_rows is the planner's estimate --
-- free to read, and NULL for a table never analysed; a partitioned table's is
-- the sum of its partitions'.
SELECT c.relname                      AS name,
       quote_ident(c.relname)         AS quoted,
       CASE c.relkind WHEN 'p' THEN 'partitioned' ELSE 'table' END AS kind,
       CASE
           WHEN c.relkind = 'p' THEN (
               SELECT nullif(sum(greatest(child.reltuples, 0)), 0)::bigint
               FROM pg_inherits i
               JOIN pg_class child ON child.oid = i.inhrelid
               WHERE i.inhparent = c.oid)
           WHEN c.reltuples < 0 THEN NULL
           ELSE c.reltuples::bigint
       END                            AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
ORDER BY c.relname;


-- name: admin_browse_columns
-- Every live column of every table above, in column order, with its type as
-- PostgreSQL itself spells it (format_type) and whether it is in the primary key.
SELECT c.relname                              AS table_name,
       a.attname                              AS name,
       quote_ident(a.attname)                 AS quoted,
       format_type(a.atttypid, a.atttypmod)   AS type,
       NOT a.attnotnull                       AS nullable,
       COALESCE(a.attnum = ANY (pk.indkey), false) AS primary_key,
       array_position(pk.indkey, a.attnum)    AS pk_position
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_index pk ON pk.indrelid = c.oid AND pk.indisprimary
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT c.relispartition
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum;
