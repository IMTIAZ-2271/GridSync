-- GridSync billing engine.
--
--   run_billing(p_point_id uuid, p_period_start date) RETURNS uuid
--
-- The unit of billing is a BILLING POINT, not a site: since migration
-- d5a7c2b91e40 a site may hold several billing meters, each on its own point,
-- each with its own periods, bills and credit balance. Rule 3 is unchanged in
-- substance -- bill the connection, not the device -- so a meter swap still
-- leaves the period, the bill and the ledger where they were.
--
-- One transaction or none of it. The caller is responsible for the isolation
-- level -- run under REPEATABLE READ or SERIALIZABLE and retry on 40001, per
-- CLAUDE.md. The SELECT ... FOR UPDATE on the period row serializes concurrent
-- runs for the same point regardless.
--
-- Idempotent: a period already in status 'billed' returns its existing
-- bill_id and changes nothing. That matters because bill, bill_line_item and
-- credit_ledger are all append-only (rule 1) -- there is no second chance to
-- correct a row, only a new bill pointing at the old one.

-- A TOU bucket: one (period_name, day_type window) with its energy and the
-- rate that priced it. rate_id is carried so bill_line_item keeps its
-- traceability FK, and the rates are carried so they can be FROZEN onto the
-- line item rather than re-looked-up later (rule 2).
DROP TYPE IF EXISTS tou_bucket CASCADE;
CREATE TYPE tou_bucket AS (
    period_name        tou_period,
    rate_id            uuid,
    import_kwh         numeric(12,4),
    export_kwh         numeric(12,4),
    import_rate        numeric(10,6),
    export_credit_rate numeric(10,6)
);


-- Dropped rather than replaced: CREATE OR REPLACE refuses to rename an input
-- parameter, and p_site_id became p_point_id here.
DROP FUNCTION IF EXISTS run_billing(uuid, date);

CREATE OR REPLACE FUNCTION run_billing(p_point_id uuid, p_period_start date)
    RETURNS uuid
    LANGUAGE plpgsql
AS $fn$
DECLARE
    -- The point's site. Carried onto billing_period and bill as a snapshot
    -- (rule 2) and kept honest by the composite FKs against
    -- billing_point (point_id, site_id).
    v_site_id      uuid;

    -- Period bounds. period_start is normalized to the first of the month so
    -- the caller may pass any date inside it.
    v_period_start date := date_trunc('month', p_period_start)::date;
    v_period_end   date;
    v_window_from  timestamptz;
    v_window_to    timestamptz;

    v_period_id    uuid;
    v_status       period_status;
    v_bill_id      uuid;

    v_plan         tariff_plan%ROWTYPE;
    v_meter_id     uuid;
    v_interval_min smallint;

    v_buckets      tou_bucket[];
    v_reading_count   integer;
    v_expected_count  integer;
    v_coverage        numeric(5,2);
    v_device_count    smallint;

    v_total_import numeric(12,4);
    v_total_export numeric(12,4);
    v_total_generation numeric(12,4);

    v_energy_charge  numeric(14,4);
    v_export_credit  numeric(14,4);
    v_fixed_charge   numeric(14,4);
    v_tax_amount     numeric(14,4);
    v_gross_amount   numeric(14,4);

    v_credit_rate        numeric(10,6);
    v_opening_kwh        numeric(12,4);
    v_opening_amount     numeric(14,4);
    v_applied_kwh        numeric(12,4);
    v_applied_amount     numeric(14,4);
    v_closing_kwh        numeric(12,4);
    v_amount_due         numeric(14,4);

    v_sort smallint := 0;

    -- Rule 8. coverage_pct is stored as a percentage, so 95% is 95.00.
    c_coverage_threshold constant numeric := 95.00;
BEGIN
    v_period_end  := (v_period_start + INTERVAL '1 month - 1 day')::date;
    v_window_from := v_period_start::timestamp AT TIME ZONE 'Asia/Dhaka';
    v_window_to   := (v_period_start + INTERVAL '1 month')::timestamp
                         AT TIME ZONE 'Asia/Dhaka';

    -- -----------------------------------------------------------------
    -- 1. Lock (or create) the period.
    -- -----------------------------------------------------------------
    SELECT period_id, status INTO v_period_id, v_status
    FROM billing_period
    WHERE billing_point_id = p_point_id AND period_start = v_period_start
    FOR UPDATE;

    IF v_period_id IS NOT NULL AND v_status = 'billed' THEN
        -- Already done. Hand back the same bill; do not write anything.
        SELECT bill_id INTO v_bill_id FROM bill WHERE period_id = v_period_id;
        RETURN v_bill_id;
    END IF;

    -- -----------------------------------------------------------------
    -- Point context: the site it sits on, the plan that site is billed
    -- under, and the point's own billing meter. Rule 7 guarantees there is
    -- exactly one of the last.
    --
    -- The tariff plan is still the site's: a household is on one tariff
    -- whether it has one connection or four.
    -- -----------------------------------------------------------------
    SELECT bp.site_id INTO v_site_id
    FROM billing_point bp
    WHERE bp.point_id = p_point_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'billing point % does not exist', p_point_id
            USING ERRCODE = '23503';
    END IF;

    -- Split from the lookup above rather than joined into it: plpgsql's
    -- SELECT ... INTO assigns one column per target, so a %ROWTYPE variable
    -- cannot share an INTO list with a scalar.
    SELECT tp.* INTO v_plan
    FROM site s
    JOIN tariff_plan tp ON tp.plan_id = s.tariff_plan_id
    WHERE s.site_id = v_site_id;

    SELECT d.device_id, d.interval_minutes INTO v_meter_id, v_interval_min
    FROM meter_spec ms
    JOIN device d ON d.device_id = ms.device_id
    WHERE ms.billing_point_id = p_point_id
      AND ms.billing_role = 'billing'
      AND d.removed_at IS NULL;

    IF v_meter_id IS NULL THEN
        RAISE EXCEPTION 'billing point % has no active billing meter',
            p_point_id
            USING ERRCODE = '23514',
                  HINT = 'rule 7: exactly one device per billing point has '
                         'meter_spec.billing_role = ''billing''';
    END IF;

    -- -----------------------------------------------------------------
    -- 2. Aggregate readings into TOU buckets.
    --
    -- day_type resolution: holiday_calendar wins, then the Bangladesh
    -- weekend (Friday and Saturday, dow 5 and 6), else weekday. Times are
    -- read in Asia/Dhaka so a TOU window means what the consumer's clock
    -- says, not what UTC says.
    -- -----------------------------------------------------------------
    WITH reading AS (
        SELECT dr.import_kwh,
               dr.export_kwh,
               (dr.interval_start AT TIME ZONE 'Asia/Dhaka')::date AS local_date,
               (dr.interval_start AT TIME ZONE 'Asia/Dhaka')::time AS local_time
        FROM device_reading dr
        WHERE dr.device_id = v_meter_id
          AND dr.interval_start >= v_window_from
          AND dr.interval_start <  v_window_to
    ),
    classified AS (
        SELECT r.*,
               CASE
                   WHEN EXISTS (SELECT 1 FROM holiday_calendar h
                                 WHERE h.holiday_date = r.local_date)
                       THEN 'holiday'
                   WHEN EXTRACT(dow FROM r.local_date) IN (5, 6)
                       THEN 'weekend'
                   ELSE 'weekday'
               END::rate_day_type AS day_type
        FROM reading r
    )
    SELECT array_agg(
               ROW(b.period_name, b.rate_id, b.import_kwh, b.export_kwh,
                   b.import_rate, b.export_credit_rate)::tou_bucket
               ORDER BY b.period_name, b.rate_id
           )
      INTO v_buckets
    FROM (
        SELECT tr.period_name,
               tr.rate_id,
               tr.import_rate,
               tr.export_credit_rate,
               round(sum(c.import_kwh), 4)::numeric(12,4) AS import_kwh,
               round(sum(c.export_kwh), 4)::numeric(12,4) AS export_kwh
        FROM classified c
        JOIN tariff_rate tr
          ON tr.plan_id  = v_plan.plan_id
         AND tr.day_type = c.day_type
         AND c.local_time >= tr.start_time
         AND c.local_time <  tr.end_time
        GROUP BY tr.period_name, tr.rate_id, tr.import_rate,
                 tr.export_credit_rate
    ) b;

    v_buckets := coalesce(v_buckets, ARRAY[]::tou_bucket[]);

    SELECT count(*)::integer,
           coalesce(round(sum(import_kwh), 4), 0)::numeric(12,4),
           coalesce(round(sum(export_kwh), 4), 0)::numeric(12,4)
      INTO v_reading_count, v_total_import, v_total_export
    FROM device_reading
    WHERE device_id = v_meter_id
      AND interval_start >= v_window_from
      AND interval_start <  v_window_to;

    -- Generation is reported by the inverter side, never by the meter
    -- (rule 6), so it is summed separately and only for the snapshot.
    --
    -- Scoped to this point's own devices, not the whole site: a site with two
    -- connections has two sets of hardware, and attributing all of it to
    -- whichever point billed first would double-count generation across the
    -- site's bills. A device belongs to this point if its own subtype row
    -- names the point -- meter_spec for a meter, inverter_spec for an
    -- inverter. Before migration d4f8a2c61e95 the inverter half was inferred
    -- from d.parent_device_id = v_meter_id; it is read off the inverter now,
    -- because an inverter need not hang off a meter at all.
    WITH point_device AS (
        SELECT d.device_id
        FROM device d
        LEFT JOIN meter_spec    ms  ON ms.device_id  = d.device_id
        LEFT JOIN inverter_spec ivs ON ivs.device_id = d.device_id
        WHERE d.site_id = v_site_id
          AND (ms.billing_point_id  = p_point_id
               OR ivs.billing_point_id = p_point_id)
    )
    SELECT coalesce(round(sum(dr.generation_kwh), 4), 0)::numeric(12,4),
           count(DISTINCT dr.device_id)::smallint
      INTO v_total_generation, v_device_count
    FROM device_reading dr
    JOIN point_device pd ON pd.device_id = dr.device_id
    WHERE dr.interval_start >= v_window_from
      AND dr.interval_start <  v_window_to;

    -- -----------------------------------------------------------------
    -- 3. Freeze the period with its snapshot, then gate on coverage.
    --    net_kwh and coverage_pct are GENERATED -- writing the inputs
    --    computes them.
    -- -----------------------------------------------------------------
    v_expected_count := ((v_period_end - v_period_start) + 1)
                        * (1440 / v_interval_min);

    IF v_period_id IS NULL THEN
        INSERT INTO billing_period (
            billing_point_id, site_id, period_start, period_end, status,
            total_import_kwh, total_export_kwh, total_generation_kwh,
            reading_count, expected_reading_count,
            contributing_device_count, frozen_at
        )
        VALUES (
            p_point_id, v_site_id, v_period_start, v_period_end, 'frozen',
            v_total_import, v_total_export, v_total_generation,
            v_reading_count, v_expected_count,
            coalesce(v_device_count, 0), now()
        )
        RETURNING period_id INTO v_period_id;
    ELSE
        UPDATE billing_period
        SET total_import_kwh          = v_total_import,
            total_export_kwh          = v_total_export,
            total_generation_kwh      = v_total_generation,
            reading_count             = v_reading_count,
            expected_reading_count    = v_expected_count,
            contributing_device_count = coalesce(v_device_count, 0),
            status                    = 'frozen',
            frozen_at                 = coalesce(frozen_at, now())
        WHERE period_id = v_period_id;
    END IF;

    SELECT coverage_pct INTO v_coverage
    FROM billing_period WHERE period_id = v_period_id;

    IF v_coverage IS NULL OR v_coverage < c_coverage_threshold THEN
        -- Note: RAISE parses '%%' before '%', so a literal percent sign
        -- adjacent to a placeholder reads backwards. Spelled out instead.
        RAISE EXCEPTION
            'billing point % period % has coverage % pct (% of % intervals), '
            'below the % pct threshold',
            p_point_id, v_period_start, coalesce(v_coverage, 0),
            v_reading_count, v_expected_count, c_coverage_threshold
            USING ERRCODE = '23514',
                  HINT = 'rule 8: never bill an incomplete period -- '
                         'estimate the gaps explicitly or refuse';
    END IF;

    -- -----------------------------------------------------------------
    -- 4. Charges.
    -- -----------------------------------------------------------------
    SELECT coalesce(round(sum(b.import_kwh * b.import_rate), 4), 0),
           coalesce(round(sum(b.export_kwh * b.export_credit_rate), 4), 0)
      INTO v_energy_charge, v_export_credit
    FROM unnest(v_buckets) b;

    v_fixed_charge := v_plan.fixed_monthly_charge;
    v_tax_amount   := round((v_energy_charge + v_fixed_charge)
                            * v_plan.tax_rate, 4);
    v_gross_amount := v_energy_charge + v_fixed_charge + v_tax_amount;

    -- -----------------------------------------------------------------
    -- 5. Credit.
    --
    -- Opening balance is what the ledger held BEFORE this period. Only the
    -- opening balance may be spent on this bill (bill_credit_applied_bounded)
    -- -- credit earned this month rolls forward, which is what makes the
    -- rollover visible rather than instantly self-cancelling.
    -- -----------------------------------------------------------------
    SELECT cl.balance_kwh_after, cl.balance_amount_after
      INTO v_opening_kwh, v_opening_amount
    FROM credit_ledger cl
    WHERE cl.billing_point_id = p_point_id
    ORDER BY cl.entry_id DESC
    LIMIT 1;

    v_opening_kwh    := coalesce(v_opening_kwh, 0);
    v_opening_amount := coalesce(v_opening_amount, 0);

    -- Value credit at the rate it was earned at this period; fall back to the
    -- plan's mean export rate when nothing was exported.
    IF v_total_export > 0 AND v_export_credit > 0 THEN
        v_credit_rate := round(v_export_credit / v_total_export, 6);
    ELSE
        SELECT round(avg(export_credit_rate), 6) INTO v_credit_rate
        FROM tariff_rate WHERE plan_id = v_plan.plan_id;
    END IF;
    v_credit_rate := coalesce(nullif(v_credit_rate, 0), 1);

    -- Spend as much of the opening balance as the bill can absorb.
    v_applied_amount := least(v_gross_amount,
                              round(v_opening_kwh * v_credit_rate, 4));
    v_applied_kwh    := least(v_opening_kwh,
                              round(v_applied_amount / v_credit_rate, 4));
    v_amount_due     := v_gross_amount - v_applied_amount;
    v_closing_kwh    := v_opening_kwh + v_total_export - v_applied_kwh;

    -- -----------------------------------------------------------------
    -- 4b. The bill. Inserted complete: it is append-only from here
    --     (forbid_mutation), so there is no post-insert correction.
    --     billing_point_id / site_id / account_id / tariff_plan_id are
    --     snapshots (rule 2).
    -- -----------------------------------------------------------------
    INSERT INTO bill (
        period_id, billing_point_id, site_id, account_id, tariff_plan_id,
        currency,
        energy_charge, export_credit_earned, fixed_charge, tax_amount,
        gross_amount, credit_opening_kwh, credit_applied_kwh,
        credit_applied_amount, credit_closing_kwh, amount_due,
        due_date, status
    )
    SELECT v_period_id, p_point_id, v_site_id, s.account_id, v_plan.plan_id,
           v_plan.currency,
           v_energy_charge, v_export_credit, v_fixed_charge, v_tax_amount,
           v_gross_amount, v_opening_kwh, v_applied_kwh,
           v_applied_amount, v_closing_kwh, v_amount_due,
           v_period_end + 15, 'issued'
    FROM site s
    WHERE s.site_id = v_site_id
    RETURNING bill_id INTO v_bill_id;

    -- -----------------------------------------------------------------
    -- Line items. rate_applied is FROZEN from the bucket, never a lookup:
    -- a rate correction next year must not rewrite this bill (rule 2).
    --
    -- energy_import + fixed + tax + adjustment = amount_due.
    -- export_credit lines are the provenance of the ledger's 'earned' entry;
    -- that value rolls forward rather than reducing this bill.
    -- -----------------------------------------------------------------
    FOR v_sort IN 1 .. coalesce(array_length(v_buckets, 1), 0) LOOP
        IF v_buckets[v_sort].import_kwh > 0 THEN
            INSERT INTO bill_line_item (
                bill_id, sort_order, rate_id, line_type, period_name,
                quantity_kwh, rate_applied, amount
            )
            VALUES (
                v_bill_id, v_sort, v_buckets[v_sort].rate_id,
                'energy_import', v_buckets[v_sort].period_name,
                v_buckets[v_sort].import_kwh,
                v_buckets[v_sort].import_rate,
                round(v_buckets[v_sort].import_kwh
                      * v_buckets[v_sort].import_rate, 4)
            );
        END IF;
    END LOOP;

    v_sort := 20;
    FOR i IN 1 .. coalesce(array_length(v_buckets, 1), 0) LOOP
        IF v_buckets[i].export_kwh > 0 THEN
            v_sort := v_sort + 1;
            INSERT INTO bill_line_item (
                bill_id, sort_order, rate_id, line_type, period_name,
                quantity_kwh, rate_applied, amount
            )
            VALUES (
                v_bill_id, v_sort, v_buckets[i].rate_id,
                'export_credit', v_buckets[i].period_name,
                v_buckets[i].export_kwh,
                v_buckets[i].export_credit_rate,
                round(v_buckets[i].export_kwh
                      * v_buckets[i].export_credit_rate, 4)
            );
        END IF;
    END LOOP;

    INSERT INTO bill_line_item (bill_id, sort_order, line_type,
                                quantity_kwh, rate_applied, amount)
    VALUES (v_bill_id, 90, 'fixed', NULL, NULL, v_fixed_charge),
           (v_bill_id, 91, 'tax',   NULL, v_plan.tax_rate, v_tax_amount);

    IF v_applied_amount > 0 THEN
        INSERT INTO bill_line_item (bill_id, sort_order, line_type,
                                    quantity_kwh, rate_applied, amount)
        VALUES (v_bill_id, 92, 'adjustment', -v_applied_kwh,
                v_credit_rate, -v_applied_amount);
    END IF;

    -- -----------------------------------------------------------------
    -- 5b. Ledger. Append-only; ledger_one_entry_per_period makes a second
    --     run for the same period impossible even if the guard above were
    --     bypassed.
    -- -----------------------------------------------------------------
    IF v_total_export > 0 THEN
        INSERT INTO credit_ledger (
            billing_point_id, site_id, period_id, bill_id, entry_type,
            kwh_delta, amount_delta,
            balance_kwh_after, balance_amount_after, expires_on, note
        )
        VALUES (
            p_point_id, v_site_id, v_period_id, v_bill_id, 'earned',
            v_total_export, v_export_credit,
            v_opening_kwh + v_total_export,
            v_opening_amount + v_export_credit,
            (v_period_end + INTERVAL '12 months')::date,
            format('Export credit for %s', to_char(v_period_start, 'YYYY-MM'))
        );
    END IF;

    IF v_applied_kwh > 0 THEN
        INSERT INTO credit_ledger (
            billing_point_id, site_id, period_id, bill_id, entry_type,
            kwh_delta, amount_delta,
            balance_kwh_after, balance_amount_after, note
        )
        VALUES (
            p_point_id, v_site_id, v_period_id, v_bill_id, 'applied',
            -v_applied_kwh, -v_applied_amount,
            v_opening_kwh + v_total_export - v_applied_kwh,
            v_opening_amount + v_export_credit - v_applied_amount,
            format('Applied to bill for %s',
                   to_char(v_period_start, 'YYYY-MM'))
        );
    END IF;

    -- -----------------------------------------------------------------
    -- 6. Close the period.
    -- -----------------------------------------------------------------
    UPDATE billing_period
    SET status = 'billed', billed_at = now()
    WHERE period_id = v_period_id;

    RETURN v_bill_id;
END;
$fn$;


COMMENT ON FUNCTION run_billing(uuid, date) IS
'Bills one BILLING POINT for the month containing p_period_start, in a '
'single transaction. A site may hold several points (several billing '
'meters); each is billed independently and keeps its own credit balance. '
'Idempotent: a period already billed returns its existing bill_id '
'unchanged. Refuses a period below 95% reading coverage (rule 8). Run under '
'REPEATABLE READ or SERIALIZABLE and retry on 40001.';
