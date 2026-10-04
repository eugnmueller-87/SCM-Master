"""Write the ``devices_by_customer`` data stream to a JSON file.

    python -m app.export_stream --out ../exports/devices_by_customer.json     (run from backend/)

The stream is what a downstream consumer ingests, one that consolidates streams from
several systems into one picture for a team: how many devices sit at which customer, when
they come back, what open purchase orders bring in the same months, and the defects that
drive new orders. Its content is ``services.rented_fleet.stream``; this module only opens
the database the app is configured for (``DATABASE_URL``), reads, and writes the file. It
never writes to the database.

**It refuses rather than overwrite a good file with an empty one.** A database without a
rented fleet (the datacenter scenario) or without a single customer has no stream to give,
and a valid but empty file over the committed sample would read as "no devices anywhere".
Both are checked before anything is written. A refusal leaves the target as it was and
exits with 3; 2 stays argparse's code for bad arguments, so a caller can tell them apart.

**The target is never left half written.** The stream goes to a temporary file beside the
target and replaces it only once complete; an exception or Ctrl-C removes the temporary
file. A run killed outright (a hard kill, a power cut) can leave a
``.devices_by_customer.*.tmp`` file beside the target, never a half-written target, and the
repository's ``.gitignore`` ignores that name. A symlinked ``--out`` is written through:
the file it points to is replaced and the link stays. The new file keeps the permission
bits of the file it replaces; a first export gets 0644, the stream being a file to hand on.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from typing import Optional

from app.core.db import SessionLocal
from app.services import fleet, rented_fleet

EXIT_REFUSED = 3        # the database has no stream to give; argparse uses 2 for bad arguments
NEW_FILE_MODE = 0o644   # a first export: readable by whoever the file is handed on to


class ExportRefused(RuntimeError):
    """The database has no stream to give; the target file is left as it was."""


def _mode_for(target: str) -> int:
    """The permission bits of the file being replaced, or NEW_FILE_MODE when there is none."""
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return NEW_FILE_MODE


def export(out: str, *, months: int = rented_fleet.MONTHS) -> dict:
    db = SessionLocal()
    try:
        if fleet.scenario(db) != "daas":
            raise ExportRefused("this database holds no rented fleet (the datacenter scenario): there is no "
                                "devices_by_customer stream to write")
        if not rented_fleet._customer_orgs(db):
            raise ExportRefused("this database holds no customer (no organisation that is neither a supplier nor "
                                "a manufacturer): the stream would have no rows")
        data = rented_fleet.stream(db, months=months)
    finally:
        db.close()
    target = os.path.realpath(out)      # through a symlink: the file it points to is replaced, the link stays
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    mode = _mode_for(target)
    fd, tmp = tempfile.mkstemp(prefix=".devices_by_customer.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, mode)             # mkstemp makes it private (0600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)    # a read-only mode copied over would block the removal on Windows
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.export_stream", description=__doc__.splitlines()[0],
                                 epilog=f"Exit codes: 0 written, 2 bad arguments, {EXIT_REFUSED} refused (the file is left as it was).")
    ap.add_argument("--out", required=True, help="path of the JSON file to write")
    ap.add_argument("--months", type=int, default=rented_fleet.MONTHS, help="months in the time grid (default 12)")
    args = ap.parse_args(argv)
    try:
        data = export(args.out, months=args.months)
    except ExportRefused as e:
        print(f"devices_by_customer not written, {args.out} left as it was: {e}", file=sys.stderr)
        return EXIT_REFUSED
    t = data["totals"]
    print(f"devices_by_customer as of {data['as_of']}: {t['customers']} customers, {t['devices_at_customer']:,} devices at customers, "
          f"{t['contracts_ending_90d']:,} contracts ending in 90 days, {t['returns_overdue']:,} returns overdue -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
