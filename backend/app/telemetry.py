"""CSV is validated completely before any state is committed."""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from uuid import uuid4

from .engine import Engine, utcnow

HEADER = ["timestamp", "station_id", "status", "produced", "rejected", "queue", "cycle_seconds"]
STATUSES = {"running", "stopped", "starved", "blocked"}


class ImportValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("CSV не прошёл проверку")


def validate_csv(content: bytes, engine: Engine) -> list[dict]:
    if len(content) > 5 * 1024 * 1024:
        raise ImportValidationError([{"row": 0, "message": "Файл превышает 5 МБ"}])
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportValidationError([{"row": 0, "message": "Нужен CSV в кодировке UTF-8"}])
    errors, rows = [], []
    try:
        reader = csv.reader(io.StringIO(decoded), strict=True)
        header = next(reader, [])
        if header != HEADER:
            raise ImportValidationError([{"row": 1, "message": "Заголовок должен быть: " + ",".join(HEADER)}])
        for number, values in enumerate(reader, 2):
            if number > 20001:
                raise ImportValidationError([{"row": number, "message": "Не более 20 000 строк данных"}])
            if len(values) != len(HEADER):
                errors.append({"row": number, "message": "Ожидается ровно 7 столбцов"})
                continue
            row = dict(zip(HEADER, (v.strip() for v in values)))
            row["row"] = number
            try:
                timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("timestamp: укажите часовой пояс, например +05:00 или Z")
                row["time"] = timestamp.astimezone(timezone.utc)
                row["timestamp"] = row["time"].isoformat()
                try:
                    station = engine.station(row["station_id"])
                except KeyError:
                    raise ValueError("Неизвестный station_id: " + row["station_id"])
                if row["status"] not in STATUSES:
                    raise ValueError("status: допустимы running, stopped, starved, blocked")
                for column in ("produced", "rejected", "queue"):
                    if not row[column].isascii() or not row[column].isdecimal():
                        raise ValueError(column + ": нужно неотрицательное целое число")
                    row[column] = int(row[column])
                    if row[column] > 10**9:
                        raise ValueError(column + ": значение больше 1 000 000 000")
                if row["rejected"] > row["produced"]:
                    raise ValueError("rejected не может превышать produced")
                if row["queue"] > station.buffer_capacity:
                    raise ValueError(f"queue превышает ёмкость буфера участка ({station.buffer_capacity})")
                row["cycle_seconds"] = float(row["cycle_seconds"])
                if not math.isfinite(row["cycle_seconds"]) or not 5 <= row["cycle_seconds"] <= 600:
                    raise ValueError("cycle_seconds: конечное число от 5 до 600")
                rows.append(row)
            except (ValueError, OverflowError) as exc:
                errors.append({"row": number, "message": str(exc)})
            if len(errors) >= 100:
                break
    except csv.Error as exc:
        errors.append({"row": reader.line_num, "message": "Некорректный CSV: " + str(exc)})
    if not rows and not errors:
        errors.append({"row": 2, "message": "Файл не содержит данных"})
    rows.sort(key=lambda row: (row["time"], row["station_id"]))
    previous = {}
    for row in rows:
        prior = previous.get(row["station_id"])
        if prior:
            if row["time"] == prior["time"]:
                errors.append({"row": row["row"], "message": "Повтор timestamp + station_id"})
            if row["produced"] < prior["produced"] or row["rejected"] < prior["rejected"]:
                errors.append({"row": row["row"], "message": "Накопительные счётчики не могут уменьшаться в пределах файла"})
            if row["rejected"] - prior["rejected"] > row["produced"] - prior["produced"]:
                errors.append({"row": row["row"], "message": "Прирост брака не может превышать прирост выпуска"})
        previous[row["station_id"]] = row
    missing = [station.id for station in engine.stations if station.id not in previous]
    if missing:
        errors.append({"row": 0, "message": "Нужны данные для всех участков: отсутствуют " + ", ".join(missing)})
    timestamps = {}
    for row in rows:
        timestamps.setdefault(row["timestamp"], set()).add(row["station_id"])
    expected = set(by_id.id for by_id in engine.stations)
    for timestamp, present in timestamps.items():
        if present != expected:
            errors.append({"row": 0, "message": f"На timestamp {timestamp} нужен полный снимок всех участков; отсутствуют: {', '.join(sorted(expected - present))}"})
    if errors:
        raise ImportValidationError(errors[:100])
    return rows


def build_telemetry(rows: list[dict], engine: Engine) -> tuple[dict, list[dict]]:
    start, end = rows[0]["time"], rows[-1]["time"]
    by_station = {station.id: [] for station in engine.stations}
    for row in rows:
        by_station[row["station_id"]].append(row)
    stations = []
    total_observed, total_stopped = 0.0, 0.0
    predictions = []
    for configured in engine.stations:
        records = by_station[configured.id]
        first, last = records[0], records[-1]
        span = (last["time"] - first["time"]).total_seconds()
        stopped = sum((right["time"] - left["time"]).total_seconds() for left, right in zip(records, records[1:]) if left["status"] == "stopped")
        total_observed += span
        total_stopped += stopped
        throughput = ((last["produced"] - last["rejected"]) - (first["produced"] - first["rejected"])) * 3600 / span if span else None
        station = {
            "id": configured.id, "name": configured.name, "order": configured.order,
            "cycle_seconds": last["cycle_seconds"], "capacity": configured.capacity,
            "buffer_capacity": configured.buffer_capacity, "defect_rate": configured.defect_rate,
            "speed_factor": configured.speed_factor, "manual_stop": last["status"] == "stopped",
            "status": last["status"], "queue": last["queue"], "in_process": None,
            "completed": last["produced"], "rejected": last["rejected"], "utilization": None,
            "downtime_seconds": round(stopped, 2), "throughput": round(throughput, 2) if throughput is not None else None,
            "progress": None, "observed_at": last["timestamp"],
        }
        stations.append(station)
        if last["status"] in {"stopped", "blocked"}:
            predictions.append({"station_id": configured.id, "station_name": configured.name, "type": "telemetry_" + last["status"], "severity": "critical", "title": "Остановка по данным импорта" if last["status"] == "stopped" else "Блокировка по данным импорта", "description": f"Последнее измерение: {last['timestamp']}. Это состояние из CSV; подключение к оборудованию не установлено.", "eta_minutes": None})
    final = stations[-1]
    good = final["completed"] - final["rejected"]
    rejected = sum(station["rejected"] for station in stations)
    metrics = {
        "produced": final["completed"], "good": good, "rejected": rejected, "wip": None,
        "throughput": final["throughput"], "availability": round(100 * (1 - total_stopped / total_observed), 2) if total_observed else None,
        "performance": None, "quality": round(good / (good + rejected) * 100, 2) if good + rejected else None,
        "oee": None, "plan_progress": round(good / engine.shift_plan * 100, 2), "forecast_good": None,
    }
    state = {
        "run_id": str(uuid4()), "source": "telemetry", "updated_at": utcnow(), "running": False, "speed": 1,
        "sim_time": (end - start).total_seconds(), "shift_duration": engine.shift_duration,
        "shift_plan": engine.shift_plan, "stations": stations, "metrics": metrics,
        "predictions": predictions, "telemetry_updated_at": end.isoformat(),
        "model": {"name": "Импорт измерений CSV", "description": "Полные синхронные снимки всех участков. Новые импорты дополняют текущую серию; время и накопительные счётчики должны возрастать. Темп: прирост годных между первым и последним измерением контроля качества. Доступность: доля наблюдаемого времени без статуса stopped (статус действует до следующего измерения). НЗП, занятость рабочих мест, производительность, OEE и прогноз неизвестны: CSV не содержит нужных данных. План и ёмкости — настройки модели, не измерения."},
    }
    latest, history = {}, []
    groups = {}
    for row in rows:
        groups.setdefault(row["time"], []).append(row)
    for timestamp, batch in groups.items():
        for row in batch:
            latest[row["station_id"]] = row
        if len(latest) != len(engine.stations):
            continue
        last = latest[engine.stations[-1].id]
        first_final = by_station[engine.stations[-1].id][0]
        duration = (last["time"] - first_final["time"]).total_seconds()
        rejected_at = sum(row["rejected"] for row in latest.values())
        good_at = last["produced"] - last["rejected"]
        history.append({"sim_time": (timestamp - start).total_seconds(), "timestamp": timestamp.isoformat(),
            "produced": last["produced"], "good": good_at, "rejected": rejected_at,
            "throughput": round((good_at - first_final["produced"] + first_final["rejected"]) * 3600 / duration, 2) if duration else None,
            "wip": None, "availability": None, "quality": round(good_at / (good_at + rejected_at) * 100, 2) if good_at + rejected_at else None})
    return state, history


def template(engine: Engine) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADER)
    for timestamp in ("2026-10-05T08:00:00+05:00", "2026-10-05T08:10:00+05:00"):
        for station in engine.stations:
            # A blank measurement form, not fabricated production observations.
            writer.writerow([timestamp, station.id, "running", "", "", "", station.cycle_seconds])
    return output.getvalue()
