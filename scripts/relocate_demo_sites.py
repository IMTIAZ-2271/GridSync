"""Move every demo site into a district that actually has staff on it.

An official governs exactly one district -- `government_profile.district`
comes from the single-use code they claimed -- so three officials can cover
three districts and no more. `db/sql/seed_orgs.sql` picks those three (Badda,
Dhanmondi, Uttara), staffs them with ten technicians and five installer
logins, and marks the other five canonical districts `is_selectable = false`
so nothing new can be filed where nobody is on duty.

That leaves the sites that were already filed elsewhere. This script moves
them, and it is the half that cannot live in the seed: `seed_demo.sql` builds
eight sites and can simply build them in the right place, but the dev database
has ten more from onboarding and device-health verification, four of them in
the legacy free-text districts (`dhaka`, `Dhaka`, `g`) that migration
e7c4b19a2d83 preserved and that no utility serves.

What moves with a site:

  * the district, and the centroid that goes with it. `site.latitude` /
    `longitude` are read by the simulator's solar geometry, so a site whose
    coordinates still point at its old district is a site that will generate
    the wrong curve the day `services/ingest` lands.
  * the address, because "12 Probe Road" and "BUET,DHAKA" are what a
    verification script types, not what a household lives at. Seeded sites
    keep their `Seed Site NN` label -- it is the identifier every verification
    note in CLAUDE.md uses -- and everything else takes its address as its
    label, which is what `POST /api/sites` has done since the supplier fleet
    table started identifying sites by name.
  * the utility on each of its billing points, when the one it had does not
    serve the district it has moved to. DESCO covers Badda and Uttara, DPDC
    covers Badda and Dhanmondi; a point pointing at the wrong one would put a
    household's meter under a company with no licence there.

Nothing here touches money. Bills, credit ledger entries, readings and meters
all hang off the billing point, which does not move -- rule 3 is exactly what
makes relocating a site a safe edit.

Placement is deterministic: sites already in a covered district stay, and the
rest are dealt round-robin in `(created_at, site_id)` order into whichever
covered district currently holds fewest. Re-running is a no-op.

    python -m scripts.relocate_demo_sites --dry-run
    python -m scripts.relocate_demo_sites
"""
from __future__ import annotations

import asyncio
import re
import sys

import asyncpg

from services.api.db import database_url

COVERED = ("Badda", "Dhanmondi", "Uttara")

# House-and-road addresses in the local style, one series per district so two
# relocated sites never land on the same door.
STREET = {
    "Badda": "House {house}, Road {road}, Middle Badda",
    "Dhanmondi": "House {house}, Road {road}, Dhanmondi",
    "Uttara": "House {house}, Road {road}, Sector {sector}, Uttara",
}

SEED_LABEL = re.compile(r"^Seed Site \d+$")


def address_for(district: str, n: int) -> str:
    return STREET[district].format(house=10 + n * 3, road=4 + n, sector=3 + n % 6)


async def main() -> int:
    dry_run = "--dry-run" in sys.argv
    conn = await asyncpg.connect(database_url())
    try:
        async with conn.transaction():
            centroid = {
                r["name"]: (r["latitude"], r["longitude"])
                for r in await conn.fetch(
                    "SELECT name, latitude, longitude FROM district"
                    " WHERE name = ANY($1::text[])",
                    list(COVERED),
                )
            }
            missing = [d for d in COVERED if d not in centroid]
            if missing:
                print(f"  MISSING DISTRICT {', '.join(missing)} --"
                      " run db/sql/seed_orgs.sql first", file=sys.stderr)
                return 1

            sites = await conn.fetch(
                """
                SELECT s.site_id, s.label, s.district, s.address_line,
                       a.email::text AS owner
                FROM site s JOIN account a USING (account_id)
                ORDER BY s.created_at, s.site_id
                """
            )

            # Covered sites keep their district; the rest fill the thinnest.
            held = {d: 0 for d in COVERED}
            for row in sites:
                if row["district"] in held:
                    held[row["district"]] += 1

            plan: list[tuple[asyncpg.Record, str, str, str]] = []
            seq = dict(held)  # per-district address counter, continuing the row
            for row in sites:
                target = row["district"]
                if target not in held:
                    target = min(COVERED, key=lambda d: (held[d], d))
                    held[target] += 1
                # Leave an address alone when it already names the district
                # it is in -- that is what `seed_demo.sql` writes, so a fresh
                # build and a repaired one do not disagree about door numbers.
                settled = row["district"] == target and (
                    (row["address_line"] or "").rstrip().endswith(target)
                )
                if settled:
                    address = row["address_line"]
                    label = row["label"]
                else:
                    n = seq[target] = seq.get(target, 0) + 1
                    address = address_for(target, n)
                    label = (row["label"] if SEED_LABEL.match(row["label"] or "")
                             else address)
                plan.append((row, target, address, label))

            moved = relabelled = 0
            for row, target, address, label in plan:
                changes = []
                if row["district"] != target:
                    changes.append(f"{row['district']} -> {target}")
                    moved += 1
                if row["address_line"] != address or row["label"] != label:
                    changes.append(f"{row['address_line']!r} -> {address!r}")
                    relabelled += 1
                mark = "->" if changes else "  "
                note = "; ".join(changes) or "unchanged"
                print(f"  {mark} {row['owner']:<22} {target:<10} {note}")

            if dry_run:
                print("\n  --dry-run: nothing written")
                return 0

            for row, target, address, label in plan:
                lat, lon = centroid[target]
                await conn.execute(
                    """
                    UPDATE site
                       SET district = $2, latitude = $3, longitude = $4,
                           address_line = $5, label = $6, city = 'Dhaka'
                     WHERE site_id = $1
                    """,
                    row["site_id"], target, lat, lon, address, label,
                )

            # Re-point any billing meter whose utility does not serve the
            # district its site now sits in. Lowest code wins where both do,
            # matching seed_orgs.sql's own tie-break.
            repointed = await conn.execute(
                """
                UPDATE billing_point bp
                SET distribution_company_id = pick.company_id
                FROM site s
                CROSS JOIN LATERAL (
                    SELECT dc.company_id
                    FROM distribution_company_area a
                    JOIN distribution_company dc ON dc.company_id = a.company_id
                    WHERE a.district = s.district
                    ORDER BY dc.code
                    LIMIT 1
                ) AS pick
                WHERE bp.site_id = s.site_id
                  AND (
                      bp.distribution_company_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1 FROM distribution_company_area a
                          WHERE a.company_id = bp.distribution_company_id
                            AND a.district = s.district
                      )
                  )
                """
            )

            stranded = await conn.fetchval(
                "SELECT count(*) FROM site WHERE district <> ALL($1::text[])",
                list(COVERED),
            )
            assert stranded == 0, f"{stranded} sites left outside a staffed district"

        print(f"\n  {len(plan)} sites: {moved} moved, {relabelled} readdressed,"
              f" {repointed.split()[-1]} billing points re-pointed.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
