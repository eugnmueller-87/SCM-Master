"""Domain exceptions, decoupled from HTTP.

Services raise these; the API layer (a single exception handler) maps each to a
status code. This keeps the service layer framework-agnostic and testable.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base class for expected, client-facing service failures."""


class NotFoundError(ServiceError):
    """A requested entity does not exist. -> 404"""


class ConflictError(ServiceError):
    """A uniqueness or state constraint was violated. -> 409"""


class ValidationError(ServiceError):
    """Input was structurally valid but breaks a business rule. -> 422"""
