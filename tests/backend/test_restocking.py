"""
Tests for restocking API endpoints and the demand forecast pricing fields they rely on.
"""
from datetime import datetime, timedelta

import pytest

import main


@pytest.fixture(autouse=True)
def clear_restocking_orders():
    """Reset the in-memory restocking order store so tests stay independent."""
    main.restocking_orders.clear()
    yield
    main.restocking_orders.clear()


@pytest.fixture
def forecasts(client):
    """Demand forecasts keyed by SKU."""
    return {f["item_sku"]: f for f in client.get("/api/demand").json()}


class TestDemandPricingFields:
    """The demand forecast must expose the cost and lead time used for restocking."""

    def test_demand_forecast_pricing_types(self, client):
        """Test that every forecast has a positive unit_cost and lead_time_days."""
        response = client.get("/api/demand")
        assert response.status_code == 200

        for forecast in response.json():
            assert isinstance(forecast["unit_cost"], (int, float))
            assert isinstance(forecast["lead_time_days"], int)
            assert forecast["unit_cost"] > 0
            assert forecast["lead_time_days"] > 0


class TestRestockingEndpoints:
    """Test suite for restocking order endpoints."""

    def test_get_restocking_orders_empty(self, client):
        """Test that no restocking orders exist initially."""
        response = client.get("/api/restocking-orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_restocking_order(self, client, forecasts):
        """Test submitting a valid restocking order."""
        payload = {"items": [{"item_sku": "WDG-001", "quantity": 10}], "budget": 10000}
        response = client.post("/api/restocking-orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        for field in ["id", "order_number", "items", "total_cost", "budget",
                      "lead_time_days", "submitted_date", "expected_delivery", "status"]:
            assert field in order
        assert order["status"] == "Submitted"
        assert order["order_number"].startswith("RST-")
        assert len(order["items"]) == 1

        item = order["items"][0]
        assert item["item_sku"] == "WDG-001"
        assert item["item_name"] == forecasts["WDG-001"]["item_name"]
        assert item["quantity"] == 10

    def test_created_order_appears_in_list(self, client):
        """Test that submitted orders are listed, newest first."""
        client.post("/api/restocking-orders",
                    json={"items": [{"item_sku": "WDG-001", "quantity": 1}], "budget": 1000})
        client.post("/api/restocking-orders",
                    json={"items": [{"item_sku": "GSK-203", "quantity": 1}], "budget": 1000})

        response = client.get("/api/restocking-orders")
        assert response.status_code == 200

        data = response.json()
        assert len(data) == 2
        assert data[0]["items"][0]["item_sku"] == "GSK-203"
        assert data[1]["items"][0]["item_sku"] == "WDG-001"
        assert data[0]["order_number"] != data[1]["order_number"]

    def test_restocking_total_cost_calculation(self, client, forecasts):
        """Test that totals are computed from server-side unit costs."""
        payload = {
            "items": [
                {"item_sku": "WDG-001", "quantity": 10},
                {"item_sku": "MTR-304", "quantity": 3},
            ],
            "budget": 100000,
        }
        order = client.post("/api/restocking-orders", json=payload).json()

        expected = (10 * forecasts["WDG-001"]["unit_cost"]
                    + 3 * forecasts["MTR-304"]["unit_cost"])
        assert abs(order["total_cost"] - expected) < 0.01

        for item in order["items"]:
            assert item["unit_cost"] == forecasts[item["item_sku"]]["unit_cost"]
            assert abs(item["line_total"] - item["quantity"] * item["unit_cost"]) < 0.01

    def test_restocking_ignores_client_supplied_price(self, client, forecasts):
        """Test that extra price fields sent by the client do not change the total."""
        payload = {
            "items": [{"item_sku": "WDG-001", "quantity": 10, "unit_cost": 0.01}],
            "budget": 100000,
        }
        order = client.post("/api/restocking-orders", json=payload).json()
        assert order["items"][0]["unit_cost"] == forecasts["WDG-001"]["unit_cost"]

    def test_restocking_lead_time_calculation(self, client, forecasts):
        """Test that order lead time is the longest item lead time."""
        skus = ["WDG-001", "GSK-203", "MTR-304"]
        payload = {"items": [{"item_sku": s, "quantity": 1} for s in skus], "budget": 100000}
        order = client.post("/api/restocking-orders", json=payload).json()

        expected_lead = max(forecasts[s]["lead_time_days"] for s in skus)
        assert order["lead_time_days"] == expected_lead

        submitted = datetime.fromisoformat(order["submitted_date"])
        expected_delivery = datetime.fromisoformat(order["expected_delivery"])
        assert expected_delivery - submitted == timedelta(days=expected_lead)

    def test_restocking_over_budget(self, client):
        """Test that an order exceeding the budget is rejected."""
        payload = {"items": [{"item_sku": "WDG-001", "quantity": 1000}], "budget": 10}
        response = client.post("/api/restocking-orders", json=payload)
        assert response.status_code == 400
        assert "budget" in response.json()["detail"].lower()

        # Rejected orders must not be stored
        assert client.get("/api/restocking-orders").json() == []

    def test_restocking_empty_items(self, client):
        """Test that an order with no items is rejected."""
        response = client.post("/api/restocking-orders", json={"items": [], "budget": 1000})
        assert response.status_code == 400

    def test_restocking_non_positive_quantity(self, client):
        """Test that zero or negative quantities are rejected."""
        for quantity in [0, -5]:
            payload = {"items": [{"item_sku": "WDG-001", "quantity": quantity}], "budget": 1000}
            response = client.post("/api/restocking-orders", json=payload)
            assert response.status_code == 400

    def test_restocking_nonexistent_sku(self, client):
        """Test that an unknown SKU returns 404."""
        payload = {"items": [{"item_sku": "NOPE-999", "quantity": 1}], "budget": 1000}
        response = client.post("/api/restocking-orders", json=payload)
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_restocking_missing_fields(self, client):
        """Test that a malformed request fails Pydantic validation."""
        response = client.post("/api/restocking-orders", json={"items": [{"item_sku": "WDG-001"}]})
        assert response.status_code == 422
