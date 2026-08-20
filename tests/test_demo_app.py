from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from demo.app import DEMO_NOTICE, create_app
from qualityproof import qualityproof
from qualityproof.discovery import is_destructive


def _login(client: TestClient, email: str = "shopper@example.test") -> None:
    password = "admin-demo" if email.startswith("admin") else "shopper-demo"
    response = client.post("/login", data={"email": email, "password": password})
    assert response.status_code == 200


@qualityproof(
    requirements=["SEED-LOCATOR-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-LOCATOR-001",
    }],
)
def test_changed_locator_retains_semantics() -> None:
    v1 = TestClient(create_app("v1")).get("/products/1").text
    v2 = TestClient(create_app("v2")).get("/products/1").text
    assert 'id="add-to-cart"' in v1
    assert 'id="basket-add"' in v2
    assert ">Add to cart</button>" in v1
    assert ">Add to cart</button>" in v2


@qualityproof(
    requirements=["SEED-A11Y-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-A11Y-001",
    }],
)
def test_v2_phone_label_is_missing() -> None:
    clients = {version: TestClient(create_app(version)) for version in ("v1", "v2")}
    for client in clients.values():
        _login(client)
    assert '<label for="phone">' in clients["v1"].get("/profile").text
    assert '<label for="phone">' not in clients["v2"].get("/profile").text


@qualityproof(
    requirements=["SEED-LINK-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-LINK-001",
    }],
)
def test_v2_help_link_is_broken() -> None:
    v1 = TestClient(create_app("v1"))
    v2 = TestClient(create_app("v2"))
    assert v1.get("/help").status_code == 200
    assert v2.get("/missing-help").status_code == 404


@qualityproof(
    requirements=["SEED-AUTHZ-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-AUTHZ-001",
    }],
)
def test_v2_has_permission_regression() -> None:
    clients = {version: TestClient(create_app(version)) for version in ("v1", "v2")}
    for client in clients.values():
        _login(client)
    assert clients["v1"].get("/admin").status_code == 403
    assert clients["v2"].get("/admin").status_code == 200


@qualityproof(
    requirements=["SEED-VALIDATION-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-VALIDATION-001",
    }],
)
def test_v2_has_profile_validation_regression() -> None:
    clients = {version: TestClient(create_app(version)) for version in ("v1", "v2")}
    for client in clients.values():
        _login(client)
    data = {"display_name": "Sam", "contact_email": "invalid", "phone": "123"}
    assert "Enter a valid contact email" in clients["v1"].post("/profile", data=data).text
    assert "Profile saved" in clients["v2"].post("/profile", data=data).text


@qualityproof(
    requirements=["SEED-TOTAL-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-TOTAL-001",
    }],
)
def test_v2_has_seeded_total_defect() -> None:
    totals: dict[str, str] = {}
    for version in ("v1", "v2"):
        client = TestClient(create_app(version))
        _login(client)
        client.post("/cart/add", data={"product_id": "1", "quantity": "2"})
        page = client.get("/checkout").text
        matched = re.search(r'data-testid="total">£([^<]+)', page)
        assert matched is not None
        totals[version] = matched.group(1)
    assert totals == {"v1": "28.00", "v2": "15.50"}


@qualityproof(
    requirements=["SEED-LAYOUT-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-LAYOUT-001",
    }],
)
def test_v2_has_overflow_marker() -> None:
    assert (
        'data-seed-defect="layout-overflow"'
        not in TestClient(create_app("v1")).get("/products").text
    )
    assert (
        'data-seed-defect="layout-overflow"' in TestClient(create_app("v2")).get("/products").text
    )


@qualityproof(
    requirements=["SEED-JOURNEY-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-JOURNEY-001",
    }],
)
def test_v2_removes_legacy_journey() -> None:
    assert TestClient(create_app("v1")).get("/legacy-order").status_code == 200
    assert TestClient(create_app("v2")).get("/legacy-order").status_code == 404


@qualityproof(
    requirements=["SEED-SAFETY-001"],
    provenance=[{
        "kind": "REQUIREMENT",
        "source": "demo/seeded-defects.json",
        "locator": "seed:SEED-SAFETY-001",
    }],
)
def test_destructive_action_is_declared_for_discovery_guard() -> None:
    assert is_destructive("Delete account")
    for version in ("v1", "v2"):
        client = TestClient(create_app(version))
        _login(client)
        assert 'href="/danger/delete-account">Delete account</a>' in client.get("/checkout").text
        assert client.get("/danger/delete-account").status_code == 405


def test_manifest_and_reset_are_deterministic() -> None:
    manifest = json.loads(
        (Path(__file__).parents[1] / "demo" / "seeded-defects.json").read_text(encoding="utf-8")
    )
    assert len(manifest["seeds"]) == 9
    assert all(seed["implemented"] for seed in manifest["seeds"])
    client = TestClient(create_app("v1"))
    assert DEMO_NOTICE in client.get("/products").text
    client.post("/cart/add", data={"product_id": "1", "quantity": "2"})
    assert "Accessible Mug" in client.get("/cart").text
    assert client.post("/__demo/reset").json() == {"status": "reset", "version": "v1"}
    assert "Accessible Mug" not in client.get("/cart").text
