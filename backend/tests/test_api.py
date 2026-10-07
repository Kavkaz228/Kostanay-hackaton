import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from auth_client import TestClient
from sqlalchemy import select

from app.database import History
from app.main import create_app


@pytest.fixture
def db_url(tmp_path):
    return "sqlite:///" + str(tmp_path / "allur-test.db")


@pytest.fixture
def client(db_url):
    with TestClient(create_app(db_url, runner_enabled=False)) as connection:
        yield connection


def csv_content(client, start=0, count=2, offset=0, invalid=None):
    stations = client.get("/api/state").json()["stations"]
    rows = []
    origin = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
    for step in range(start, start + count):
        for station in stations:
            rows.append([(origin + timedelta(minutes=10 * step)).isoformat(), station["id"], "running", str(step * 10 + offset), "0", "0", str(station["cycle_seconds"])])
    if invalid:
        invalid(rows)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "station_id", "status", "produced", "rejected", "queue", "cycle_seconds"])
    writer.writerows(rows)
    return output.getvalue().encode()


def upload(client, content):
    return client.post("/api/import", files={"file": ("telemetry.csv", content, "text/csv")})


def test_health_advance_control_history_and_export(client):
    assert client.get("/api/health").json() == {"status": "ok", "database": "ok"}
    initial = client.get("/api/state").json()
    assert initial["source"] == "simulation"
    assert initial["metrics"]["good"] == 0
    controlled = client.post("/api/control", json={"speed": 60, "shift_plan": 600}).json()
    assert controlled["speed"] == 60
    assert controlled["shift_plan"] == 600
    advanced = client.post("/api/advance", json={"seconds": 3600}).json()
    assert advanced["sim_time"] == 3600
    assert advanced["metrics"]["good"] > 40
    history = client.get("/api/history").json()
    assert len(history) == 61
    assert history[-1]["produced"] == advanced["metrics"]["produced"]
    exported = client.get("/api/export")
    assert exported.status_code == 200
    assert "run_id,source,timestamp,sim_time" in exported.text


def test_invalid_mutations_leave_state_unchanged(client):
    original = client.app.state.service.engine.to_dict()
    for path, method, body in [
        ("/api/advance", "post", {"seconds": 3601}),
        ("/api/advance", "post", {"seconds": 1.2}),
        ("/api/control", "post", {"speed": 500}),
        ("/api/control", "post", {"speed": True}),
        ("/api/control", "post", {"running": "true"}),
        ("/api/control", "post", {"shift_plan": 0}),
        ("/api/stations/painting", "patch", {"defect_rate": 0.9}),
        ("/api/stations/painting", "patch", {"unexpected": 2}),
        ("/api/reset", "post", {"confirm": False}),
        ("/api/reset", "post", {"confirm": 1}),
    ]:
        assert getattr(client, method)(path, json=body).status_code == 422
    malformed = client.patch("/api/stations/painting", content='{"cycle_seconds": NaN}', headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422
    assert client.app.state.service.engine.to_dict() == original


def test_incident_acknowledge_and_automatic_resolution(client):
    assert client.patch("/api/stations/assembly", json={"manual_stop": True}).status_code == 200
    incidents = client.get("/api/incidents").json()
    incident = next(item for item in incidents if item["kind"] == "manual_stop")
    assert incident["status"] == "open"
    ack = client.post(f"/api/incidents/{incident['id']}/acknowledge").json()
    assert ack["status"] == "acknowledged"
    assert ack["acknowledged_at"]
    client.patch("/api/stations/assembly", json={"manual_stop": False})
    updated = next(item for item in client.get("/api/incidents").json() if item["id"] == incident["id"])
    assert updated["status"] == "resolved"
    assert updated["resolved_at"]
    assert client.post(f"/api/incidents/{incident['id']}/acknowledge").status_code == 409


def test_reset_preserves_configuration_and_archived_history(client):
    client.patch("/api/stations/painting", json={"cycle_seconds": 45})
    previous = client.post("/api/advance", json={"seconds": 600}).json()
    result = client.post("/api/reset", json={"confirm": True}).json()
    assert result["run_id"] != previous["run_id"]
    assert result["sim_time"] == 0
    assert result["metrics"]["good"] == 0
    assert next(s for s in result["stations"] if s["id"] == "painting")["cycle_seconds"] == 45
    assert client.get("/api/history", params={"run_id": previous["run_id"]}).json()[-1]["sim_time"] == 600


def test_restart_restores_simulation_incidents_and_scenarios(db_url):
    app = create_app(db_url, runner_enabled=False)
    with TestClient(app) as first:
        first.post("/api/advance", json={"seconds": 1337})
        first.patch("/api/stations/assembly", json={"manual_stop": True})
        result = first.post("/api/scenarios", json={"name": "Восстановление", "horizon_minutes": 15, "changes": [{"station_id": "assembly", "manual_stop": False}]})
        assert result.status_code == 200
        expected = app.state.service.engine.to_dict()
        incidents = first.get("/api/incidents").json()
    with TestClient(create_app(db_url, runner_enabled=False)) as second:
        assert second.app.state.service.engine.to_dict() == expected
        assert len(second.get("/api/scenarios").json()) == 1
        assert [i["id"] for i in second.get("/api/incidents").json()] == [i["id"] for i in incidents]
        second.post("/api/advance", json={"seconds": 120})
        from app.engine import Engine
        comparison = Engine.from_dict(expected)
        comparison.advance(120)
        assert second.app.state.service.engine.to_dict() == comparison.to_dict()


def test_scenario_endpoint_does_not_mutate_live_state(client):
    client.post("/api/advance", json={"seconds": 900})
    before = client.app.state.service.engine.to_dict()
    response = client.post("/api/scenarios", json={"name": "Парное сравнение", "horizon_minutes": 15, "changes": [{"station_id": "painting", "speed_factor": 1.5}]})
    assert response.status_code == 200
    assert response.json()["timeline"][-1]["minute"] == 15
    assert client.app.state.service.engine.to_dict() == before
    assert client.post("/api/scenarios", json={"name": "Нет участка", "horizon_minutes": 15, "changes": [{"station_id": "unknown", "manual_stop": True}]}).status_code == 404


def test_import_source_isolation_and_true_observed_metrics(client):
    client.post("/api/advance", json={"seconds": 900})
    simulation = client.app.state.service.engine.to_dict()
    assert upload(client, csv_content(client)).json()["imported"] == 10
    state = client.get("/api/state").json()
    assert state["source"] == "telemetry"
    assert state["metrics"]["good"] == 10
    assert state["metrics"]["throughput"] == 60
    assert state["metrics"]["wip"] is None
    assert state["metrics"]["oee"] is None
    assert state["stations"][0]["in_process"] is None
    for method, path, body in [
        ("post", "/api/control", {"running": True}),
        ("post", "/api/advance", {"seconds": 60}),
        ("patch", "/api/stations/painting", {"manual_stop": True}),
        ("post", "/api/reset", {"confirm": True}),
        ("post", "/api/scenarios", {"name": "Неизвестное НЗП", "horizon_minutes": 15, "changes": [{"station_id": "painting", "speed_factor": 1.5}]}),
    ]:
        assert getattr(client, method)(path, json=body).status_code == 409
    assert upload(client, csv_content(client, start=2, count=1)).status_code == 200
    assert len(client.get("/api/history").json()) == 3
    assert client.get("/api/state").json()["metrics"]["throughput"] == 60
    client.post("/api/source", json={"source": "simulation"})
    assert client.app.state.service.engine.to_dict() == simulation
    assert client.get("/api/state").json()["sim_time"] == 900
    client.post("/api/source", json={"source": "telemetry"})
    assert client.get("/api/state").json()["metrics"]["good"] == 20


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[0].__setitem__(1, "unknown"),
    lambda rows: rows[0].__setitem__(3, "-1"),
    lambda rows: rows[0].__setitem__(4, "1"),
    lambda rows: rows[0].__setitem__(5, "999"),
    lambda rows: rows[0].__setitem__(6, "nan"),
    lambda rows: rows[0].__setitem__(0, "2026-10-05T08:00:00"),
    lambda rows: rows[0].__setitem__(2, "idle"),
    lambda rows: rows.pop(),
    lambda rows: rows[5].__setitem__(3, "0") or rows[0].__setitem__(3, "1"),
])
def test_import_validation_is_atomic(client, mutate):
    before = client.app.state.service.engine.to_dict()
    response = upload(client, csv_content(client, invalid=mutate))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    assert client.app.state.service.engine.to_dict() == before
    assert client.get("/api/state").json()["source"] == "simulation"
    assert client.post("/api/source", json={"source": "telemetry"}).status_code == 409


def test_reimport_or_regression_does_not_overwrite_telemetry(client):
    original = csv_content(client)
    assert upload(client, original).status_code == 200
    state = client.get("/api/state").json()
    assert upload(client, original).status_code == 422
    regression = csv_content(client, start=2, count=1, invalid=lambda rows: rows[0].__setitem__(3, "0"))
    assert upload(client, regression).status_code == 422
    assert client.get("/api/state").json() == state


def test_database_failure_rolls_back_engine(client, monkeypatch):
    before = client.app.state.service.engine.to_dict()
    original = client.app.state.service._record
    def fail(session):
        raise RuntimeError("simulated persistence outage")
    monkeypatch.setattr(client.app.state.service, "_record", fail)
    assert client.post("/api/advance", json={"seconds": 60}).status_code == 500
    assert client.app.state.service.engine.to_dict() == before
    monkeypatch.setattr(client.app.state.service, "_record", original)


def test_shift_stops_at_end(client):
    for _ in range(8):
        response = client.post("/api/advance", json={"seconds": 3600})
        assert response.status_code == 200
    state = response.json()
    assert state["sim_time"] == state["shift_duration"]
    assert state["running"] is False
    assert client.post("/api/control", json={"running": True}).status_code == 409
    assert client.post("/api/advance", json={"seconds": 1}).status_code == 409


def test_telemetry_single_snapshot_does_not_invent_rates(client):
    assert upload(client, csv_content(client, count=1)).status_code == 200
    state = client.get("/api/state").json()
    assert state["metrics"]["throughput"] is None
    assert state["metrics"]["availability"] is None
    assert state["metrics"]["performance"] is None


def test_telemetry_restart_preserves_both_sources_and_acknowledged_incident(db_url):
    with TestClient(create_app(db_url, runner_enabled=False)) as first:
        first.post("/api/advance", json={"seconds": 1080})
        simulation = first.app.state.service.engine.to_dict()
        content = csv_content(first, invalid=lambda rows: rows[-2].__setitem__(2, "stopped"))
        assert upload(first, content).status_code == 200
        incident = next(i for i in first.get("/api/incidents").json() if i["kind"] == "telemetry_stopped")
        first.post(f"/api/incidents/{incident['id']}/acknowledge")
        telemetry = first.get("/api/state").json()
        history = first.get("/api/history").json()
    with TestClient(create_app(db_url, runner_enabled=False)) as second:
        restored = second.get("/api/state").json()
        assert restored["source"] == "telemetry"
        assert restored["run_id"] == telemetry["run_id"]
        assert restored["stations"] == telemetry["stations"]
        assert restored["metrics"] == telemetry["metrics"]
        assert second.get("/api/history").json() == history
        restored_incident = next(i for i in second.get("/api/incidents").json() if i["id"] == incident["id"])
        assert restored_incident["status"] == "acknowledged"
        assert second.post("/api/source", json={"source": "simulation"}).status_code == 200
        assert second.app.state.service.engine.to_dict() == simulation
        assert upload(second, csv_content(second, start=2, count=1)).status_code == 200
        resolved = next(i for i in second.get("/api/incidents").json() if i["id"] == incident["id"])
        assert resolved["status"] == "resolved"


def test_concurrent_advance_and_control_serialize_without_lost_updates(client, db_url):
    service = client.app.state.service
    expected = service.engine.clone()
    expected.advance(12 * 60)
    def perform(index):
        if index % 2 == 0:
            return client.post("/api/advance", json={"seconds": 60})
        return client.post("/api/control", json={"speed": 60 if index % 3 else 30, "shift_plan": 500})
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(perform, range(24)))
    assert all(response.status_code == 200 for response in responses)
    assert service.engine.sim_time == 720
    assert service.engine.to_dict()["stations"] == expected.to_dict()["stations"]
    assert service.engine.to_dict()["rng_state"] == expected.to_dict()["rng_state"]
    assert service.engine.shift_plan == 500
    assert len(client.get("/api/history").json()) == 13
    from app.service import Service
    restored = Service(db_url)
    try:
        assert restored.engine.to_dict() == service.engine.to_dict()
    finally:
        restored.close()


def test_partly_invalid_append_preserves_telemetry_history_and_incidents(client):
    assert upload(client, csv_content(client)).status_code == 200
    before = client.get("/api/state").json()
    history = client.get("/api/history").json()
    incidents = client.get("/api/incidents").json()
    invalid = csv_content(client, start=2, count=3, invalid=lambda rows: rows[-1].__setitem__(4, "999"))
    response = upload(client, invalid)
    assert response.status_code == 422
    assert response.json()["detail"][0]["row"] == 16
    assert client.get("/api/state").json() == before
    assert client.get("/api/history").json() == history
    assert client.get("/api/incidents").json() == incidents
    assert upload(client, csv_content(client, start=2, count=3)).status_code == 200
    assert len(client.get("/api/history").json()) == 5


def test_import_database_failure_rolls_back_saved_and_memory_state(client, monkeypatch):
    assert upload(client, csv_content(client)).status_code == 200
    before = client.get("/api/state").json()
    history = client.get("/api/history").json()
    def fail(session):
        raise RuntimeError("incident storage failure")
    monkeypatch.setattr(client.app.state.service, "_reconcile", fail)
    assert upload(client, csv_content(client, start=2, count=1)).status_code == 500
    assert client.get("/api/state").json() == before
    assert client.get("/api/history").json() == history


def test_api_docs_are_served_under_proxy_prefix(client):
    assert client.get("/api/docs").status_code == 200
    schema = client.get("/api/openapi.json").json()
    assert "/api/scenarios" in schema["paths"]
    assert "/api/import" in schema["paths"]
