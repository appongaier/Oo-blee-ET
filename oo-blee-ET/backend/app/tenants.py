"""Tenant registry and graph-name safety.

One FalkorDB server holds one graph per tenant. Graph names are never built from
free-form input: a tenant id must be in the registry, and any graph name must pass
the strict pattern below before it is used.
"""
import re

# tenant_id -> graph name. Adding a tenant needs explicit approval (see project scope).
TENANTS: dict[str, str] = {"acme": "tenant_acme", "globex": "tenant_globex"}

_GRAPH_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


class UnknownTenantError(ValueError):
    pass


class InvalidGraphNameError(ValueError):
    pass


def validate_graph_name(name: str) -> str:
    if not isinstance(name, str) or not _GRAPH_NAME_RE.fullmatch(name):
        raise InvalidGraphNameError("graph names must match ^[a-z][a-z0-9_]{2,62}$")
    return name


def graph_name_for(tenant_id: str) -> str:
    try:
        name = TENANTS[tenant_id]
    except (KeyError, TypeError):
        raise UnknownTenantError(f"unknown tenant: {tenant_id!r}") from None
    return validate_graph_name(name)
