"""Deterministic finite-buffer production line. All counters come from moving parts."""
from __future__ import annotations

import copy
import hashlib
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Part:
    id: int
    fault_key: int
    remaining: float = 0.0
    passed: bool | None = None


@dataclass
class Station:
    id: str
    name: str
    order: int
    cycle_seconds: float
    capacity: int = 1
    buffer_capacity: int = 12
    defect_rate: float = 0.01
    speed_factor: float = 1.0
    manual_stop: bool = False
    queue: list[Part] = field(default_factory=list)
    processing: list[Part] = field(default_factory=list)
    completed: int = 0
    rejected: int = 0
    busy_seconds: float = 0.0
    downtime_seconds: int = 0
    status: str = "starved"
    status_since: int = 0

    @property
    def duration(self) -> float:
        return self.cycle_seconds / self.speed_factor

    @property
    def rate(self) -> float:
        return 0.0 if self.manual_stop else self.capacity / self.duration


DEFAULT_STATIONS = [
    ("warehouse", "Комплектация", 36.0, 16, 0.002),
    ("welding", "Сварка", 44.0, 12, 0.015),
    ("painting", "Окраска", 52.0, 10, 0.025),
    ("assembly", "Сборка", 48.0, 12, 0.010),
    ("quality", "Контроль качества", 38.0, 10, 0.005),
]


def _tuples(value: Any) -> Any:
    return tuple(_tuples(v) for v in value) if isinstance(value, list) else value


class Engine:
    def __init__(self, seed: int = 20261016):
        self.run_id = str(uuid4())
        self.sim_time = 0
        self.shift_duration = 8 * 3600
        self.shift_plan = 480
        self.running = False
        self.speed = 30
        self.released = 0
        self.seed = seed
        self.random = random.Random(seed)
        self.stations = [Station(id=s[0], name=s[1], order=i, cycle_seconds=s[2], buffer_capacity=s[3], defect_rate=s[4]) for i, s in enumerate(DEFAULT_STATIONS)]

    def clone(self) -> Engine:
        return Engine.from_dict(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "sim_time": self.sim_time, "shift_duration": self.shift_duration,
            "shift_plan": self.shift_plan, "running": self.running, "speed": self.speed,
            "released": self.released, "seed": self.seed, "rng_state": self.random.getstate(),
            "stations": [asdict(s) for s in self.stations],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Engine:
        engine = cls(seed=data["seed"])
        for key in ("run_id", "sim_time", "shift_duration", "shift_plan", "running", "speed", "released"):
            setattr(engine, key, data[key])
        engine.random.setstate(_tuples(data["rng_state"]))
        engine.stations = []
        for raw in data["stations"]:
            raw = copy.deepcopy(raw)
            raw["queue"] = [Part(**p) for p in raw["queue"]]
            raw["processing"] = [Part(**p) for p in raw["processing"]]
            engine.stations.append(Station(**raw))
        return engine

    def station(self, station_id: str) -> Station:
        for station in self.stations:
            if station.id == station_id:
                return station
        raise KeyError(station_id)

    def configure(self, station_id: str, changes: dict) -> None:
        station = self.station(station_id)
        if "buffer_capacity" in changes and changes["buffer_capacity"] < len(station.queue):
            raise ValueError(f"В буфере {len(station.queue)} изделий: сначала освободите места, затем уменьшите ёмкость.")
        if "capacity" in changes and changes["capacity"] < len(station.processing):
            raise ValueError(f"В обработке {len(station.processing)} изделий: нельзя убрать занятые рабочие места.")
        previous_duration = station.duration
        for key, value in changes.items():
            setattr(station, key, value)
        # A speed/cycle change preserves already completed fractional work.
        if station.duration != previous_duration:
            for part in station.processing:
                part.remaining *= station.duration / previous_duration
        self.refresh_statuses()

    @staticmethod
    def defect(part: Part, station: Station) -> bool:
        # Common random numbers by part and station keep paired experiments fair
        # even when the order of operations diverges after an intervention.
        digest = hashlib.blake2b(f"{part.fault_key}:{station.id}".encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") / 2**64 < station.defect_rate

    def advance(self, seconds: int) -> None:
        for _ in range(seconds):
            self._tick()

    def _tick(self) -> None:
        # Each tick processes one model second; transfers happen only after work.
        for index, station in enumerate(self.stations):
            if station.manual_stop:
                continue
            while len(station.processing) < station.capacity:
                if index == 0:
                    self.released += 1
                    part = Part(self.released, self.random.getrandbits(64))
                elif station.queue:
                    part = station.queue.pop(0)
                else:
                    break
                part.remaining = station.duration
                part.passed = None
                station.processing.append(part)
        for index in range(len(self.stations) - 1, -1, -1):
            station = self.stations[index]
            if station.manual_stop:
                station.downtime_seconds += 1
                continue
            active = sum(1 for p in station.processing if p.remaining > 0)
            station.busy_seconds += active / station.capacity
            for part in station.processing[:]:
                part.remaining = max(0, part.remaining - 1)
                if part.remaining > 0:
                    continue
                if part.passed is None:
                    part.passed = not self.defect(part, station)
                if not part.passed:
                    station.rejected += 1
                    station.completed += 1
                    station.processing.remove(part)
                elif index == len(self.stations) - 1:
                    station.completed += 1
                    station.processing.remove(part)
                else:
                    downstream = self.stations[index + 1]
                    if len(downstream.queue) < downstream.buffer_capacity:
                        downstream.queue.append(part)
                        station.completed += 1
                        station.processing.remove(part)
        self.sim_time += 1
        self.refresh_statuses()

    def refresh_statuses(self) -> None:
        for index, station in enumerate(self.stations):
            old_status = station.status
            if station.manual_stop:
                station.status = "stopped"
            elif any(p.remaining > 0 for p in station.processing):
                station.status = "running"
            elif station.processing:
                station.status = "blocked"
            elif index == 0 or station.queue:
                station.status = "running"
            else:
                station.status = "starved"
            if station.status != old_status:
                station.status_since = self.sim_time

    def metrics(self) -> dict:
        elapsed = self.sim_time
        last = self.stations[-1]
        good = last.completed - last.rejected
        rejected = sum(s.rejected for s in self.stations)
        wip = sum(len(s.queue) + len(s.processing) for s in self.stations)
        downtime = sum(s.downtime_seconds for s in self.stations)
        availability = max(0, 1 - downtime / (elapsed * len(self.stations))) if elapsed else 1
        quality = good / (good + rejected) if good + rejected else 1
        nominal_rate = min(s.capacity / s.cycle_seconds for s in self.stations)
        available_seconds = elapsed * availability
        performance = min(1, last.completed / (available_seconds * nominal_rate)) if available_seconds else 0
        forecast = self.forecast_good()
        return {
            "produced": last.completed, "good": good, "rejected": rejected, "wip": wip,
            "throughput": round(good * 3600 / elapsed, 2) if elapsed else 0,
            "availability": round(100 * availability, 2), "performance": round(100 * performance, 2),
            "quality": round(100 * quality, 2), "oee": round(100 * availability * performance * quality, 2),
            "plan_progress": round(100 * good / self.shift_plan, 2), "forecast_good": forecast,
        }

    def forecast_good(self) -> int:
        """Expected steady-state shift estimate, conservative for a held stop.

        Rate is capacity propagated through all downstream yield factors. Empty
        pipeline startup deducts travel time. This is an estimate, not an ML claim.
        """
        good = self.stations[-1].completed - self.stations[-1].rejected
        remaining = max(0, self.shift_duration - self.sim_time)
        if remaining == 0:
            return good
        rates = []
        for index, station in enumerate(self.stations):
            rate = station.rate
            for downstream in self.stations[index:]:
                rate *= 1 - downstream.defect_rate
            rates.append(rate)
        rate = min(rates)
        # Estimate travel left until the first downstream output at startup.
        occupied = [i for i, s in enumerate(self.stations) if s.processing or s.queue]
        if self.sim_time == 0:
            delay = sum(s.duration for s in self.stations)
        elif not good and occupied:
            delay = sum(s.duration for s in self.stations[max(occupied) + 1:])
        else:
            delay = 0
        return good + max(0, round(rate * max(0, remaining - delay)))

    def predictions(self) -> list[dict]:
        predictions = []
        def add(station: Station, kind: str, severity: str, title: str, description: str, eta=None):
            predictions.append({"station_id": station.id, "station_name": station.name,
                "type": kind, "severity": severity, "title": title,
                "description": description, "eta_minutes": round(eta, 1) if eta is not None else None})
        for index, station in enumerate(self.stations):
            if station.manual_stop:
                add(station, "manual_stop", "critical", "Участок остановлен", "Включена ручная остановка. Обработка приостановлена; изделия и выполненная работа сохранены.", 0)
            elif station.status == "blocked":
                add(station, "blocked", "critical", "Выход участка заблокирован", "Готовое изделие удерживается на рабочем месте: в следующем буфере нет места.", 0)
            elif station.status == "starved" and self.sim_time > sum(s.duration for s in self.stations[:index + 1]) and self.sim_time - station.status_since >= max(60, station.duration):
                add(station, "starved", "warning", "Участок ожидает изделия", "Буфер пуст, рабочих изделий нет. Проверьте выпуск предыдущих участков.", 0)
            if index:
                upstream = self.stations[index - 1]
                input_rate = upstream.rate * (1 - upstream.defect_rate)
                drain_rate = station.rate
                free = station.buffer_capacity - len(station.queue)
                if free == 0:
                    add(station, "buffer_full", "warning", "Буфер заполнен", f"Заняты все {station.buffer_capacity} мест. Поступление следующего изделия может заблокировать предыдущий участок до освобождения места.", 0)
                elif input_rate > drain_rate + 1e-9:
                    eta = free / (input_rate - drain_rate) / 60
                    if eta <= 60:
                        add(station, "buffer_full", "warning", "Риск заполнения буфера", f"Свободно {free} мест. Расчётная подача {input_rate * 3600:.1f}/ч, обработка {drain_rate * 3600:.1f}/ч. Оценка при непрерывной подаче и текущих настройках.", eta)
                if upstream.manual_stop and not station.manual_stop:
                    supply = len(station.queue) + len(station.processing)
                    eta = supply / station.rate / 60 if station.rate else None
                    add(station, "supply_stop", "warning", "Риск отсутствия подачи", "Предыдущий участок остановлен. Время исчерпания рассчитано по текущему запасу и мощности.", eta)
            if station.completed >= 30 and station.rejected / station.completed > max(0.05, station.defect_rate * 2):
                add(station, "quality", "warning", "Повышенная доля брака", f"Отклонено {station.rejected} из {station.completed} изделий; заданная вероятность брака {station.defect_rate * 100:.1f}%. Это правило по наблюдаемым счётчикам.")
        return predictions

    def public_stations(self) -> list[dict]:
        result = []
        for station in self.stations:
            progresses = [100 * (1 - p.remaining / station.duration) for p in station.processing]
            result.append({
                "id": station.id, "name": station.name, "order": station.order,
                "cycle_seconds": station.cycle_seconds, "capacity": station.capacity,
                "buffer_capacity": station.buffer_capacity, "defect_rate": station.defect_rate,
                "speed_factor": station.speed_factor, "manual_stop": station.manual_stop,
                "status": station.status, "queue": len(station.queue), "in_process": len(station.processing),
                "completed": station.completed, "rejected": station.rejected,
                "utilization": round(min(100, 100 * station.busy_seconds / self.sim_time), 2) if self.sim_time else 0,
                "downtime_seconds": station.downtime_seconds,
                "throughput": round((station.completed - station.rejected) * 3600 / self.sim_time, 2) if self.sim_time else 0,
                "progress": round(sum(progresses) / len(progresses), 1) if progresses else 0,
            })
        return result

    def public_state(self) -> dict:
        return {
            "run_id": self.run_id, "source": "simulation", "updated_at": utcnow(),
            "running": self.running, "speed": self.speed, "sim_time": self.sim_time,
            "shift_duration": self.shift_duration, "shift_plan": self.shift_plan,
            "stations": self.public_stations(), "metrics": self.metrics(), "predictions": self.predictions(),
            "model": {"name": "Дискретная модель • 1 секунда", "description": "Синтетическая линия из пяти участков с конечными буферами, браком и обратным давлением. Прогноз смены: стационарная мощность узкого места с учётом выхода годных; текущие остановки сохраняются. Качество = годные / (годные + брак всей линии). Доступность A = 1 − сумма ручных простоев / (время × число участков). Производительность P = выпуск последнего участка / (время × A × номинальная мощность узкого места), ограничена 100%. OEE = A × P × качество: модельная оценка, не аттестованный заводской показатель."},
            "telemetry_updated_at": None,
        }


def run_scenario(engine: Engine, name: str, horizon_minutes: int, changes: list[dict]) -> dict:
    baseline, variant = engine.clone(), engine.clone()
    for change in changes:
        variant.configure(change["station_id"], {k: v for k, v in change.items() if k != "station_id"})
    start = engine.metrics()
    start_downtime = sum(s.downtime_seconds for s in engine.stations)
    timeline = [{"minute": 0, "baseline_good": 0, "variant_good": 0}]
    interval = max(1, horizon_minutes // 48)
    for minute in range(1, horizon_minutes + 1):
        baseline.advance(60)
        variant.advance(60)
        if minute % interval == 0 or minute == horizon_minutes:
            timeline.append({"minute": minute, "baseline_good": baseline.metrics()["good"] - start["good"], "variant_good": variant.metrics()["good"] - start["good"]})
    def result(line):
        metric = line.metrics()
        return {"good": metric["good"] - start["good"], "rejected": metric["rejected"] - start["rejected"], "wip": metric["wip"], "downtime_minutes": round((sum(s.downtime_seconds for s in line.stations) - start_downtime) / 60, 2)}
    base, varied = result(baseline), result(variant)
    return {"id": str(uuid4()), "name": name, "created_at": utcnow(), "horizon_minutes": horizon_minutes,
        "changes": changes, "baseline": base, "variant": varied,
        "delta": {k: round(varied[k] - base[k], 2) for k in base}, "timeline": timeline,
        "explanation": "Два независимых прогона из одного состояния. Одинаковые изделия имеют одинаковые случайные исходы качества; изменены только заданные параметры. Выпуск, брак и простой — прирост за горизонт; НЗП — остаток в конце горизонта. Прогноз не учитывает будущие незаданные отказы и кадровые ограничения."}
