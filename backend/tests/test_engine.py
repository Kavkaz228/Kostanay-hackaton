import json

import pytest

from app.engine import Engine, run_scenario


def conservation(line):
    metric = line.metrics()
    assert line.released == metric["good"] + metric["rejected"] + metric["wip"]
    for station in line.stations:
        assert len(station.queue) <= station.buffer_capacity
        assert len(station.processing) <= station.capacity
        assert station.rejected <= station.completed
        assert all(part.remaining >= 0 for part in station.processing)


def test_conservation_over_full_shift_and_capacity():
    line = Engine()
    line.configure("welding", {"capacity": 2, "cycle_seconds": 63, "defect_rate": 0.18})
    line.configure("quality", {"capacity": 3, "cycle_seconds": 29, "defect_rate": 0.1})
    for _ in range(48):
        line.advance(600)
        conservation(line)
    assert line.metrics()["good"] > 100
    assert line.metrics()["rejected"] > 50


def test_stop_builds_backpressure_and_resumes_without_losing_parts():
    line = Engine()
    for station in line.stations:
        line.configure(station.id, {"defect_rate": 0, "buffer_capacity": 2})
    line.advance(900)
    good_before = line.metrics()["good"]
    line.configure("assembly", {"manual_stop": True})
    line.advance(1800)
    conservation(line)
    assert line.station("painting").status == "blocked"
    assert len(line.station("assembly").queue) == 2
    assert line.station("assembly").downtime_seconds == 1800
    stationary = line.metrics()["good"]
    line.advance(300)
    assert line.metrics()["good"] == stationary
    assert stationary >= good_before
    line.configure("assembly", {"manual_stop": False})
    line.advance(1200)
    assert line.metrics()["good"] > stationary
    conservation(line)


def test_serialized_restart_preserves_rng_and_work_exactly():
    line = Engine()
    line.advance(1337)
    restored = Engine.from_dict(json.loads(json.dumps(line.to_dict())))
    line.advance(10000)
    restored.advance(10000)
    assert line.to_dict() == restored.to_dict()
    conservation(restored)


def test_identical_scenario_has_zero_effect_and_is_isolated():
    line = Engine()
    line.advance(1700)
    before = line.to_dict()
    result = run_scenario(line, "Без изменений", 60, [{"station_id": "painting", "cycle_seconds": 52}])
    assert result["baseline"] == result["variant"]
    assert all(value == 0 for value in result["delta"].values())
    assert line.to_dict() == before
    assert result["timeline"][-1]["minute"] == 60


def test_scenario_speed_improvement_is_measured():
    line = Engine()
    for station in line.stations:
        station.defect_rate = 0
    line.advance(3600)
    result = run_scenario(line, "Быстрая окраска", 120, [{"station_id": "painting", "speed_factor": 1.6}])
    assert result["delta"]["good"] > 0
    assert result["variant"]["good"] > result["baseline"]["good"]


def test_busy_capacity_or_buffer_cannot_be_removed():
    line = Engine()
    line.configure("warehouse", {"capacity": 2})
    line.advance(1)
    with pytest.raises(ValueError, match="рабочие"):
        line.configure("warehouse", {"capacity": 1})
    line.configure("welding", {"manual_stop": True})
    line.advance(500)
    with pytest.raises(ValueError, match="буфере"):
        line.configure("welding", {"buffer_capacity": 1})
    conservation(line)


def test_speed_change_keeps_fractional_work_and_quality_formula():
    line = Engine()
    line.advance(18)
    part = line.station("warehouse").processing[0]
    assert part.remaining == 18
    line.configure("warehouse", {"speed_factor": 2})
    assert part.remaining == 9
    line.advance(10000)
    metric = line.metrics()
    assert metric["quality"] == round(100 * metric["good"] / (metric["good"] + metric["rejected"]), 2)
    conservation(line)


def test_performance_does_not_double_count_manual_downtime():
    line = Engine()
    for station in line.stations:
        station.defect_rate = 0
        line.configure(station.id, {"manual_stop": True})
    line.advance(3600)
    for station in line.stations:
        line.configure(station.id, {"manual_stop": False})
    line.advance(3600)
    metric = line.metrics()
    assert metric["availability"] == 50
    expected_performance = min(100, 100 * metric["produced"] / (3600 / 52))
    assert metric["performance"] == round(expected_performance, 2)
    assert metric["oee"] == round(expected_performance / 2, 2)


def test_full_buffer_remains_an_active_risk_while_station_is_processing():
    line = Engine()
    line.advance(3600)
    full = [station for station in line.stations[1:] if len(station.queue) == station.buffer_capacity]
    assert full
    predictions = line.predictions()
    for station in full:
        alert = next(item for item in predictions if item["station_id"] == station.id and item["type"] == "buffer_full")
        assert alert["eta_minutes"] == 0
        assert alert["title"] == "Буфер заполнен"
    conservation(line)


def test_completed_quality_decision_does_not_change_while_blocked():
    # Find a reproducible part that would fail at a higher future defect rate.
    for seed in range(20):
        line = Engine(seed)
        line.configure("warehouse", {"defect_rate": 0})
        line.configure("welding", {"manual_stop": True, "buffer_capacity": 1})
        line.advance(100)
        station = line.station("warehouse")
        part = station.processing[0]
        assert station.status == "blocked"
        assert part.passed is True
        line.configure("warehouse", {"defect_rate": 0.5})
        if line.defect(part, station):
            line.advance(60)
            assert part in station.processing
            assert part.passed is True
            assert station.rejected == 0
            conservation(line)
            break
    else:
        pytest.fail("No qualifying deterministic test part found")
