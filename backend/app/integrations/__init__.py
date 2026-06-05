"""Integration layer: how external ERP / P2P systems feed this one.

This package is the **adapter boundary** that lets SCM-Master live *alongside* an
existing landscape (SAP as ERP, Coupa as P2P) rather than replace it. The shape
is deliberately a hexagonal port/adapter:

    upstream feed  ->  Adapter (parse + map)  ->  canonical records  ->  sync engine
                                                                          (existing services,
                                                                           idempotent upsert)

- An **Adapter** (e.g. ``coupa.CoupaCsvAdapter``) knows ONE upstream's wire
  format. It parses the raw feed and maps it onto the canonical record shapes in
  ``schemas`` — it contains no database logic.
- The **sync engine** (``sync.py``) takes those canonical records and upserts
  them through the *same* domain services the rest of the app uses, keyed on the
  ``(source_system, external_ref)`` pair. Re-running a feed updates rows instead
  of duplicating them.

Adding SAP later means adding ``sap.py`` with another Adapter that maps IDoc /
OData payloads onto the same canonical records — the sync engine is unchanged.
"""
from __future__ import annotations
