"""Service layer: business rules live here, not in route handlers.

Routes stay thin — they validate input via Pydantic, call a service, and shape
the response. Anything that is a *rule* (legal status transitions, uniqueness,
cross-entity invariants) belongs in this package so it is reusable and testable
independently of HTTP.
"""
