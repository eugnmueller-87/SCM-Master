"""Boot step: take today's KPI measurement, in a process of its own.

A KPI is measured once a day. The boot takes that measurement so nobody opens the KPIs
tab and waits for 32 reads over a 400,000-device fleet.

**Why this is a separate step and not a line inside the seed.** Seeding 431,200 serials
peaks around 170 MB, and measuring peaks around 140 MB. Run in one process those peaks
add up, because a Python process does not hand freed memory straight back to the
operating system: the interpreter keeps the arenas it has grown. On 23.09.2026 the
hosted container was killed for running out of memory doing exactly that, on the boot
that seeded the fleet and then measured it. Run as two processes, the boot's peak is the
larger of the two rather than their sum, and the operating system reclaims everything in
between.

The same rule the other seed steps follow applies here: never in production, and not
when the operator opted out with ``SEED_DEMO=0``. A measurement that fails never stops
the service from coming up; the KPIs tab measures on demand instead.
"""
from __future__ import annotations

from app.core.safety import should_seed_demo
from app.seed_reset import ensure_measured

if __name__ == "__main__":
    if should_seed_demo():
        ensure_measured()
    else:
        print("Skipping the KPI measurement (production, or SEED_DEMO=0).")
