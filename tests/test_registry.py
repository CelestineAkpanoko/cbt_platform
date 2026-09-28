import pytest

from registration_service.registry import (
    RegistryError,
    add_entry,
    deactivate_entry,
    list_clarity_ids,
    list_cosinuss_ids,
    list_entries,
    list_orgs,
)


@pytest.fixture
def registry(dynamodb):
    return dynamodb.Table("Registry")


def test_add_and_list_per_kind(registry):
    add_entry(registry, "org", "org1", name="Farm A", location="Immokalee")
    add_entry(registry, "cosinuss", "ZC5C5W")
    add_entry(registry, "clarity", "DGFVZ0274", location="Packing house")

    orgs = list_orgs(registry)
    assert [o["org_id"] for o in orgs] == ["org1"]
    assert orgs[0]["name"] == "Farm A"
    assert orgs[0]["location"] == "Immokalee"
    assert list_cosinuss_ids(registry) == ["ZC5C5W"]
    assert list_clarity_ids(registry) == ["DGFVZ0274"]


def test_duplicate_active_entry_is_rejected(registry):
    add_entry(registry, "cosinuss", "ZC5C5W")
    with pytest.raises(RegistryError):
        add_entry(registry, "cosinuss", "ZC5C5W")


def test_deactivate_hides_and_readd_reactivates(registry):
    add_entry(registry, "cosinuss", "ZC5C5W")
    deactivate_entry(registry, "cosinuss", "ZC5C5W")
    assert list_cosinuss_ids(registry) == []
    assert [e["sk"] for e in
            list_entries(registry, "cosinuss", include_inactive=True)] == ["ZC5C5W"]

    add_entry(registry, "cosinuss", "ZC5C5W")  # reactivate, not a conflict
    assert list_cosinuss_ids(registry) == ["ZC5C5W"]


def test_unknown_kind_raises(registry):
    with pytest.raises(RegistryError):
        add_entry(registry, "fitbit", "D58MBD")
    with pytest.raises(RegistryError):
        list_entries(registry, "fitbit")


def test_empty_id_raises(registry):
    with pytest.raises(RegistryError):
        add_entry(registry, "org", "   ")


def test_clarity_org_map(registry):
    from registration_service.registry import add_entry, clarity_org_map, deactivate_entry

    add_entry(registry, "clarity", "DGFVZ0274", org_id="org1")
    add_entry(registry, "clarity", "DIAFM3720", org_id="org2")
    add_entry(registry, "clarity", "NOORG1234")  # no org assigned

    assert clarity_org_map(registry) == {
        "DGFVZ0274": "org1", "DIAFM3720": "org2"}

    deactivate_entry(registry, "clarity", "DGFVZ0274")
    assert clarity_org_map(registry) == {"DIAFM3720": "org2"}
