import pytest

from app.tenants import (InvalidGraphNameError, UnknownTenantError, graph_name_for,
                         validate_graph_name)


def test_registered_tenants_map_to_graphs():
    assert graph_name_for("acme") == "tenant_acme"
    assert graph_name_for("globex") == "tenant_globex"


@pytest.mark.parametrize("bad", ["", "ACME", "acme; DROP", "tenant-acme", "a", "x" * 80,
                                 "tenant acme", "../etc", None, 5])
def test_bad_graph_names_rejected(bad):
    with pytest.raises(InvalidGraphNameError):
        validate_graph_name(bad)


@pytest.mark.parametrize("bad", ["initech", "tenant_acme", "", None, "acme "])
def test_unknown_tenants_rejected(bad):
    with pytest.raises(UnknownTenantError):
        graph_name_for(bad)
