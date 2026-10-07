from __future__ import annotations

import copy
import csv
import io
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text, func, insert
from sqlalchemy.orm import sessionmaker

from .database import History, Incident, Scenario, Snapshot, Observation, Receipt, make_database
from .security import audit
from .engine import Engine, run_scenario, utcnow
from .telemetry import ImportValidationError, build_telemetry, validate_csv
from . import robots
from .analytics import analyze
from .schemas import RobotConfig
from .manufacturing import Manufacturing, parse_case, quality_csv

logger = logging.getLogger(__name__)


class Service(Manufacturing):
    def __init__(self, database_url: str):
        self.database, self.sessions = make_database(database_url)
        self.owner_connection = None
        if self.database.dialect.name == 'postgresql':
            self.owner_connection = self.database.connect()
            if not self.owner_connection.scalar(text('SELECT pg_try_advisory_lock(714902610)')):
                self.owner_connection.close()
                self.database.dispose()
                raise RuntimeError('Another application owns this database. Exactly one API process is supported per installation.')
            self.owner_connection.commit()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.runner_thread = None
        self.engine = Engine()
        self.source = "simulation"
        self.telemetry = None
        self.telemetry_rows = []
        self.robot_rows = []
        self.robot_count = 0
        self.runner_error = None
        self.read_cache = {}
        self.cache_locks = {}
        self.cache_guard = threading.Lock()
        self.cache_revision = 0
        self.robot_configs = {}
        self.import_log = []
        self.production_telemetry = None
        self.updated_at = utcnow()
        with self.sessions.begin() as session:
            saved = session.get(Snapshot, "application")
            if saved:
                self._restore(saved.data)
                version = saved.data.get('storage_version', 1)
                if version > 2:
                    raise RuntimeError('Database schema is newer than this application; restore a matching release.')
                if version == 1 and self.robot_rows:
                    session.execute(insert(Observation), [{'robot_id': r['robot_id'], 'timestamp': r['timestamp'], 'data': r} for r in self.robot_rows])
                    self.robot_count = len(self.robot_rows)
                    self.robot_rows = self._compact(self.robot_rows)
                saved.data = self._dump()
        with self._mutation() as session:
            self._record(session)
            self._reconcile(session)

    def _dump(self):
        return {"simulation": self.engine.to_dict(), "source": self.source, "telemetry": self.telemetry,
                "telemetry_rows": self.telemetry_rows, "robot_rows": self.robot_rows,
                "storage_version": 2, "robot_count": self.robot_count,
                "production_telemetry": self.production_telemetry, "robot_configs": self.robot_configs,
                "import_log": self.import_log, "updated_at": self.updated_at}

    def _restore(self, saved):
        self.engine = Engine.from_dict(saved["simulation"])
        self.source = saved["source"]
        self.telemetry = saved.get("telemetry")
        self.telemetry_rows = saved.get("telemetry_rows", [])
        self.robot_rows = saved.get("robot_rows", [])
        self.robot_count = saved.get('robot_count', len(self.robot_rows))
        self.robot_configs = saved.get('robot_configs', {})
        self.import_log = saved.get('import_log', [])
        self.production_telemetry = saved.get("production_telemetry")
        if self.telemetry and self.telemetry.get("telemetry_kind") != "robots":
            self.production_telemetry = self.telemetry
        self.updated_at = saved.get("updated_at", utcnow())

    @contextmanager
    def _mutation(self, preserve_detail=False):
        with self.lock:
            self._check_owner()
            original = copy.deepcopy(self._dump())
            try:
                # Persist on the SAME PostgreSQL connection that owns the lease.
                # A database restart cannot allow a stale process to write through
                # an unrelated reconnected pool session before its next heartbeat.
                writer = sessionmaker(bind=self.owner_connection, expire_on_commit=False) if self.owner_connection is not None else self.sessions
                with writer.begin() as session:
                    yield session
                    audit(session)
                    self.updated_at = utcnow()
                    self.cache_revision += 1
                    # A live detail view has an explicit five-second freshness
                    # bound. Do not recompute every full history on each packet.
                    if preserve_detail:
                        self.read_cache = {key: value for key, value in self.read_cache.items()
                                           if isinstance(key, tuple) and key[0] == 'detail' and time.monotonic()-value[0] < 5}
                    else:
                        self.read_cache.clear()
                    saved = session.get(Snapshot, "application")
                    if saved:
                        saved.data = self._dump()
                    else:
                        session.add(Snapshot(key="application", data=self._dump()))
            except BaseException:
                self._restore(original)
                raise

    def _check_owner(self):
        if self.owner_connection is None:
            return
        with self.lock:
            try:
                if self.owner_connection.invalidated:
                    raise RuntimeError('Owner connection was invalidated')
                self.owner_connection.execute(text('SELECT 1'))
                self.owner_connection.commit()
            except Exception:
                logger.critical('Database owner connection lost; restarting to reload durable state')
                import os
                os._exit(1)

    def state(self):
        with self.lock:
            state = self.engine.public_state() if self.source == "simulation" else copy.deepcopy(self.telemetry)
            state["updated_at"] = self.updated_at
            state['telemetry_available'] = {'production': bool(self.production_telemetry), 'robots': bool(self.robot_rows)}
            if self.telemetry:
                state["telemetry_updated_at"] = self.telemetry["telemetry_updated_at"]
            return state

    def healthy(self):
        if self.runner_error:
            raise RuntimeError('Simulation worker could not persist its last step')
        if self.runner_thread and not self.runner_thread.is_alive():
            raise RuntimeError('Simulation worker stopped')
        with self.sessions() as session:
            session.execute(text("SELECT 1"))

    def cached(self, key, operation, seconds=2):
        cached = self.read_cache.get(key)
        if cached and time.monotonic() - cached[0] < seconds:
            return cached[1]
        # Reads of different robot histories must not queue behind the global
        # model writer lock. Coalesce only concurrent requests for the same view.
        with self.cache_guard:
            if len(self.cache_locks) > 512:
                self.cache_locks.clear()
            key_lock = self.cache_locks.setdefault(key, threading.Lock())
        with key_lock:
            cached = self.read_cache.get(key)
            if cached and time.monotonic() - cached[0] < seconds:
                return cached[1]
            revision = self.cache_revision
            value = operation()
            with self.lock:
                if revision == self.cache_revision:
                    if len(self.read_cache) > 512:
                        self.read_cache.clear()
                    self.read_cache[key] = (time.monotonic(), value)
            return value

    def dashboard(self):
        def read():
            with self.lock:
                return json.dumps({'state': self.state(), 'history': self.history(), 'incidents': self.incidents()}, ensure_ascii=False, allow_nan=False).encode()
        return self.cached('dashboard', read)

    @staticmethod
    def _compact(rows):
        # Preserve earliest observation, latest status and last non-null reading for EVERY sensor.
        # Historical rows live only in the indexed database table.
        if not rows:
            return []
        keep = {('first', ''): rows[0]}
        for row in rows:
            keep[(row['robot_id'], 'latest')] = row
            for sensor in robots.SENSORS:
                if row.get(sensor) is not None:
                    keep[(row['robot_id'], sensor)] = row
        unique = {(r['robot_id'], r['timestamp']): r for r in keep.values()}
        return sorted(unique.values(), key=lambda r: (r['timestamp'], r['robot_id']))

    def _robot_state(self):
        state = robots.build(self.robot_rows, self.engine, self.robot_rows[0]['run_id'], self.robot_configs)
        state['observation_count'] = self.robot_count
        return state

    def _simulation_required(self):
        if self.source != "simulation":
            raise HTTPException(409, "Изменение модели недоступно в режиме измерений. Переключитесь на симуляцию.")

    def _record(self, session):
        if self.source != "simulation":
            return
        last = session.scalars(select(History).where(History.run_id == self.engine.run_id).order_by(History.id.desc()).limit(1)).first()
        if last and last.data["sim_time"] == self.engine.sim_time:
            return
        metrics = self.engine.metrics()
        record = {"sim_time": self.engine.sim_time, "timestamp": utcnow(), **{key: metrics[key] for key in ("produced", "good", "rejected", "throughput", "wip", "availability", "quality")}}
        session.add(History(run_id=self.engine.run_id, source="simulation", data=record))
        session.flush()

    def _reconcile(self, session, cache=None):
        state = self.engine.public_state() if self.source == "simulation" else self.telemetry
        if not state:
            return
        def incident_key(prediction):
            raw = f"{prediction['station_id']}:{prediction['type']}"
            return raw if len(raw) <= 128 else hashlib.sha256(raw.encode()).hexdigest()
        active = {incident_key(p): p for p in state["predictions"]}
        if cache is not None and 'open' in cache:
            open_by_key = cache['open']
        else:
            rows = session.scalars(select(Incident).where(Incident.run_id == state["run_id"], Incident.source == self.source, Incident.data['status'].as_string() != 'resolved')).all()
            open_by_key = {row.key: row for row in rows}
            if cache is not None:
                cache['open'] = open_by_key
        for key, row in list(open_by_key.items()):
            if key not in active:
                row.data = {**row.data, "status": "resolved", "resolved_at": state.get("event_time", utcnow())}
                del open_by_key[key]
        for key, prediction in active.items():
            if key in open_by_key:
                row = open_by_key[key]
                row.data = {**row.data, "title": prediction["title"], "description": prediction["description"], "severity": prediction["severity"]}
                continue
            incident_id = str(uuid4())
            data = {"id": incident_id, "run_id": state["run_id"], "source": self.source,
                "station_id": prediction["station_id"], "station_name": prediction["station_name"],
                "severity": prediction["severity"], "kind": prediction["type"],
                "title": prediction["title"], "description": prediction["description"],
                "created_at": prediction.get("observed_at", utcnow()), "sim_time": state["sim_time"], "status": "open",
                "acknowledged_at": None, "resolved_at": None}
            incident = Incident(id=incident_id, run_id=state['run_id'], source=self.source, key=key, data=data)
            session.add(incident)
            open_by_key[key] = incident
        if cache is None:
            session.flush()

    def control(self, changes):
        with self._mutation() as session:
            self._simulation_required()
            if changes.get("running") and self.engine.sim_time >= self.engine.shift_duration:
                raise HTTPException(409, "Смена завершена. Начните новую смену.")
            for key, value in changes.items():
                setattr(self.engine, key, value)
            self._reconcile(session)
        return self.state()

    def _advance(self, seconds, session):
        remaining = min(seconds, max(0, self.engine.shift_duration - self.engine.sim_time))
        while remaining:
            chunk = min(60, remaining)
            self.engine.advance(chunk)
            self._record(session)
            self._reconcile(session)
            remaining -= chunk
        if self.engine.sim_time >= self.engine.shift_duration:
            self.engine.running = False

    def advance(self, seconds):
        with self._mutation() as session:
            self._simulation_required()
            if self.engine.sim_time >= self.engine.shift_duration:
                raise HTTPException(409, "Смена завершена. Начните новую смену.")
            self._advance(seconds, session)
        return self.state()

    def configure(self, station_id, changes):
        with self._mutation() as session:
            self._simulation_required()
            try:
                self.engine.configure(station_id, changes)
            except KeyError:
                raise HTTPException(404, "Участок не найден")
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            self._reconcile(session)
        return self.state()

    def reset(self):
        with self._mutation() as session:
            self._simulation_required()
            for incident in session.scalars(select(Incident).where(Incident.run_id == self.engine.run_id)):
                if incident.data["status"] != "resolved":
                    incident.data = {**incident.data, "status": "resolved", "resolved_at": utcnow(), "resolution": "Начата новая смена"}
            old = self.engine
            self.engine = Engine(seed=old.seed + 1)
            self.engine.shift_plan = old.shift_plan
            self.engine.speed = old.speed
            for station in old.stations:
                self.engine.configure(station.id, {key: getattr(station, key) for key in ("cycle_seconds", "capacity", "buffer_capacity", "defect_rate", "speed_factor", "manual_stop")})
            self._record(session)
            self._reconcile(session)
        return self.state()

    def switch_source(self, source, telemetry_kind=None):
        with self._mutation() as session:
            if telemetry_kind and source != 'telemetry':
                raise HTTPException(422, 'Тип телеметрии применяется только к источнику telemetry')
            if telemetry_kind == 'production':
                if not self.production_telemetry:
                    raise HTTPException(409, 'Сначала импортируйте CSV участков')
                self.telemetry = copy.deepcopy(self.production_telemetry)
            elif telemetry_kind == 'robots':
                if not self.robot_rows:
                    raise HTTPException(409, 'Сначала импортируйте CSV роботов')
                self.telemetry = self._robot_state()
            if source == "telemetry" and not self.telemetry:
                raise HTTPException(409, "Сначала импортируйте CSV с измерениями.")
            if source != self.source:
                self.engine.running = False
            self.source = source
            self._reconcile(session)
        return self.state()

    def history(self, limit=120, run_id=None):
        with self.lock, self.sessions() as session:
            current = self.engine.run_id if self.source == "simulation" else self.telemetry["run_id"]
            rows = session.scalars(select(History).where(History.run_id == (run_id or current)).order_by(History.id.desc()).limit(limit)).all()
            return [row.data for row in reversed(rows)]

    def incidents(self, include_archived=False):
        with self.lock, self.sessions() as session:
            current = self.engine.run_id if self.source == "simulation" else self.telemetry["run_id"]
            query = select(Incident).where(Incident.source == self.source)
            if not include_archived:
                query = query.where(Incident.run_id == current)
            rows = session.scalars(query).all()
            ordered = sorted([row.data for row in rows], key=lambda data: data['created_at'], reverse=True)
            active = [row for row in ordered if row['status'] != 'resolved']
            resolved = [row for row in ordered if row['status'] == 'resolved']
            # An old, still-open alarm must never disappear behind newer closed events.
            return active + resolved[:max(0, 2000 - len(active))]

    def export_incidents(self):
        output = io.StringIO(newline='')
        fields = ['id', 'run_id', 'source', 'station_id', 'station_name', 'kind', 'severity', 'title', 'description', 'status', 'created_at', 'acknowledged_at', 'resolved_at']
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        with self.sessions() as session:
            for row in session.scalars(select(Incident).order_by(Incident.id)).yield_per(1000):
                writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for key, value in row.data.items()})
        return output.getvalue()

    def acknowledge(self, incident_id):
        with self._mutation() as session:
            row = session.get(Incident, incident_id)
            if not row:
                raise HTTPException(404, "Инцидент не найден")
            if row.data["status"] == "resolved":
                raise HTTPException(409, "Инцидент уже завершён")
            if row.data["status"] == "open":
                row.data = {**row.data, "status": "acknowledged", "acknowledged_at": utcnow()}
            result = copy.deepcopy(row.data)
        return result

    def scenario(self, name, horizon_minutes, changes):
        with self.lock:
            if self.source == "telemetry":
                raise HTTPException(409, "CSV не содержит занятость рабочих мест и остаточное время обработки. Точно восстановить состояние для парного сценария невозможно. Переключитесь на симуляцию.")
            line = self.engine.clone()
        try:
            result = run_scenario(line, name, horizon_minutes, changes)
        except KeyError:
            raise HTTPException(404, "Участок сценария не найден")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        result["run_id"] = line.run_id
        result["start_sim_time"] = line.sim_time
        with self.sessions.begin() as session:
            session.add(Scenario(id=result["id"], data=result))
            audit(session)
        return result

    def scenarios(self):
        with self.sessions() as session:
            values = [row.data for row in session.scalars(select(Scenario)).all()]
            return sorted(values, key=lambda data: data["created_at"], reverse=True)[:200]

    def import_csv(self, content, filename='CSV'):
        if content.startswith(b'PK'):
            return self.import_case(content, filename)
        if b'record_id' in content.split(b'\n', 1)[0]:
            return self.quality_records(quality_csv(content), filename)
        if robots.is_robot_csv(content):
            return self.import_robots(content, filename)
        with self._mutation() as session:
            rows = validate_csv(content, self.engine)
            self._validate_incoming('production', rows)
            prior = [{**row, "time": datetime.fromisoformat(row["timestamp"])} for row in self.telemetry_rows]
            telemetry, history = build_telemetry(prior + rows, self.engine)
            if self.production_telemetry:
                telemetry["run_id"] = self.production_telemetry["run_id"]
            self._log_import('production', rows, filename)
            self.production_telemetry = telemetry
            self.telemetry = telemetry
            self.telemetry_rows += [{k: v for k, v in row.items() if k != "time"} for row in rows]
            new_start = rows[0]["time"]
            for row in history:
                if datetime.fromisoformat(row["timestamp"]) >= new_start:
                    session.add(History(run_id=telemetry["run_id"], source="telemetry", data=row))
            self.engine.running = False
            self.source = "telemetry"
            self._reconcile(session)
        return {"imported": len(rows), "source": "telemetry", "message": f"Импортировано {len(rows)} измерений. Симуляция приостановлена; открыт режим данных CSV."}

    def import_robots(self, content, filename='CSV', receipt_key=None):
        rows = robots.validate(content)
        with self._mutation(preserve_detail=True) as session:
            digest = hashlib.sha256(content).hexdigest()
            if receipt_key:
                receipt = session.get(Receipt, receipt_key)
                if receipt:
                    if receipt.digest != digest:
                        raise HTTPException(409, 'Idempotency-Key уже использован для другого содержимого')
                    return receipt.data
            prior_rows = self.robot_rows
            old = {r['robot_id']: r for r in prior_rows}
            self._validate_incoming('robots', rows)
            # The durable series identifier is stored on observations to survive source switches.
            run_id = self.robot_rows[0]['run_id'] if self.robot_rows else str(uuid4())
            rows = [{**r, 'run_id': run_id} for r in rows]
            self._log_import('robots', rows, filename)
            session.execute(insert(Observation), [{'robot_id': r['robot_id'], 'timestamp': r['timestamp'], 'data': r} for r in rows])
            from .scada import robot_bridge
            robot_bridge(session, rows)
            self.robot_count += len(rows)
            self.robot_rows = self._compact(sorted(self.robot_rows + rows, key=lambda r: (r['timestamp'], r['robot_id'])))
            self.engine.running = False
            self.source = 'telemetry'
            self.telemetry = self._robot_state()
            # Reconcile transitions within the file as well as the final snapshot.
            active = {robot_id: [] for robot_id in old}
            for prediction in robots.current_predictions(prior_rows, self.robot_configs):
                active.setdefault(prediction['station_id'], []).append(prediction)
            incident_cache = {}
            for row in rows:
                before = active.get(row['robot_id'], [])
                after = robots.predictions(row, self.robot_configs.get(row['robot_id']))
                # A missing sensor cannot confirm that an existing limit violation cleared.
                after += [p for p in before if p['type'].startswith('limit:') and row.get(p['type'].split(':')[1]) is None]
                active[row['robot_id']] = after
                if [{k: v for k, v in p.items() if k != 'observed_at'} for p in before] == [{k: v for k, v in p.items() if k != 'observed_at'} for p in after]:
                    continue
                self.telemetry['predictions'] = [p for values in active.values() for p in values]
                self.telemetry['event_time'] = row['timestamp']
                self.telemetry['sim_time'] = (datetime.fromisoformat(row['timestamp']) - datetime.fromisoformat(self.robot_rows[0]['timestamp'])).total_seconds()
                self._reconcile(session, incident_cache)
            self.telemetry = self._robot_state()
            result = {'imported': len(rows), 'source': 'telemetry', 'message': f'Импортировано {len(rows)} измерений роботов. Откройте обзор для просмотра датчиков.'}
            if receipt_key:
                session.add(Receipt(key=receipt_key, digest=digest, data=result, created_at=time.time()))
        return result

    def _validate_incoming(self, kind, rows):
        previous_rows = self.robot_rows if kind == 'robots' else self.telemetry_rows
        identifier = 'robot_id' if kind == 'robots' else 'station_id'
        old = {r[identifier]: r for r in previous_rows}
        errors = []
        for row in rows:
            prior = old.get(row[identifier])
            if not prior:
                continue
            if row['timestamp'] <= prior['timestamp']:
                errors.append({'row': row['row'], 'message': 'timestamp должен быть позже последнего наблюдения этого робота' if kind == 'robots' else 'timestamp должен быть позже последнего сохранённого снимка'})
            if kind == 'production':
                if row['produced'] < prior['produced'] or row['rejected'] < prior['rejected']:
                    errors.append({'row': row['row'], 'message': 'Счётчики меньше последних сохранённых значений'})
                if row['rejected'] - prior['rejected'] > row['produced'] - prior['produced']:
                    errors.append({'row': row['row'], 'message': 'Прирост брака больше прироста выпуска относительно сохранённых значений'})
        if kind == 'production' and len(previous_rows) + len(rows) > 200000:
            errors.append({'row': 0, 'message': 'Серия достигла ограничения 200 000 измерений'})
        if kind == 'robots' and len(set(old) | {r['robot_id'] for r in rows}) > 1000:
            errors.append({'row': 0, 'message': 'Не более 1000 роботов в серии'})
        if errors:
            raise ImportValidationError(errors[:100])

    def preview_csv(self, content):
        if content.startswith(b'PK'):
            report = parse_case(content)
            dates = sorted(r['date'] for r in report['lines'])
            return {'kind': 'case', 'count': report['count'], 'identifiers': sorted({r['line'] for r in report['lines']}), 'start': dates[0]+'T00:00:00+05:00', 'end': dates[-1]+'T00:00:00+05:00', 'preview': report['lines'][:10], 'warnings': report['warnings']}
        if b'record_id' in content.split(b'\n', 1)[0]:
            rows = quality_csv(content)
            return {'kind': 'quality', 'count': len(rows), 'identifiers': sorted({r['model'] for r in rows}), 'start': min(r['timestamp'] for r in rows), 'end': max(r['timestamp'] for r in rows), 'preview': rows[:10]}
        with self.lock:
            kind = 'robots' if robots.is_robot_csv(content) else 'production'
            rows = robots.validate(content) if kind == 'robots' else validate_csv(content, self.engine)
            self._validate_incoming(kind, rows)
            key = 'robot_id' if kind == 'robots' else 'station_id'
            return {'kind': kind, 'count': len(rows), 'identifiers': sorted({r[key] for r in rows}),
                    'start': rows[0]['timestamp'], 'end': rows[-1]['timestamp'],
                    'preview': [{k: v for k, v in r.items() if k not in ('time', 'row')} for r in rows[:10]]}

    def _log_import(self, kind, rows, filename):
        self.import_log = ([{'id': str(uuid4()), 'filename': filename[:200], 'kind': kind, 'count': len(rows),
                            'start': rows[0]['timestamp'], 'end': rows[-1]['timestamp'], 'imported_at': utcnow()}] + self.import_log)[:200]

    def imports(self):
        with self.lock:
            return copy.deepcopy(self.import_log)

    def robot_config(self, robot_id):
        with self.lock:
            if not any(r['robot_id'] == robot_id for r in self.robot_rows):
                raise HTTPException(404, 'Робот не найден')
            return copy.deepcopy(self.robot_configs.get(robot_id, RobotConfig().model_dump()))

    def configure_robot(self, robot_id, config):
        with self._mutation() as session:
            self.robot_config(robot_id)
            self.robot_configs = {**self.robot_configs, robot_id: config}
            # Evaluate the saved robot series without changing the currently selected source.
            original_source, original_telemetry = self.source, self.telemetry
            self.source = 'telemetry'
            self.telemetry = self._robot_state()
            self.telemetry['event_time'] = utcnow()
            self._reconcile(session)
            if original_source == 'telemetry' and original_telemetry.get('telemetry_kind') == 'robots':
                self.telemetry.pop('event_time', None)
            else:
                self.source, self.telemetry = original_source, original_telemetry
        return self.robot_config(robot_id)

    def _robot_query(self, robot_id=None, start=None, end=None):
        def parse(value):
            if value is None:
                return None
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError()
                from datetime import timezone
                return stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise HTTPException(422, 'Начало и конец периода должны быть ISO 8601 с часовым поясом')
        lower, upper = parse(start), parse(end)
        if lower and upper and lower > upper:
            raise HTTPException(422, 'Начало периода позже окончания')
        query = select(Observation)
        if robot_id is not None:
            query = query.where(Observation.robot_id == robot_id)
        if lower is not None:
            query = query.where(Observation.timestamp >= lower)
        if upper is not None:
            query = query.where(Observation.timestamp <= upper)
        return query

    def _robot_range(self, robot_id=None, start=None, end=None):
        with self.sessions() as session:
            rows = list(session.scalars(self._robot_query(robot_id, start, end).with_only_columns(Observation.data).order_by(Observation.timestamp, Observation.robot_id).limit(100001)))
        if len(rows) > 100000:
            raise HTTPException(422, 'В выбранном периоде более 100 000 измерений. Сузьте период анализа; полный CSV доступен через экспорт.')
        return rows

    def robot_history(self, robot_id, limit, start=None, end=None, offset=0):
        def read():
            with self.sessions() as session:
                query = self._robot_query(robot_id, start, end)
                total = session.scalar(select(func.count()).select_from(query.subquery()))
                rows = [r.data for r in session.scalars(query.order_by(Observation.timestamp.desc()).offset(offset).limit(limit))]
                return {'total': total, 'rows': list(reversed(rows)), 'offset': offset}
        return self.cached(('history', robot_id, limit, start, end, offset), read)

    def robot_analytics(self, robot_id, start=None, end=None):
        def read():
            config = self.robot_config(robot_id)
            rows = self._robot_range(robot_id, start, end)
            return analyze(rows, config)
        return self.cached(('analytics', robot_id, start, end), read, 10)

    def robot_detail(self, robot_id, start=None, end=None, offset=0):
        def read():
            config = self.robot_config(robot_id)
            try:
                # Fetch once for both chart and analytics. Avoid three queries and
                # duplicate ORM materialization on every dashboard refresh.
                rows = self._robot_range(robot_id, start, end)
                stop = max(0, len(rows)-offset)
                history = {'total': len(rows), 'rows': rows[max(0, stop-500):stop], 'offset': offset}
                analytics, warning = analyze(rows, config), None
            except HTTPException as exc:
                if exc.status_code != 422:
                    raise
                analytics, warning = None, exc.detail
                history = self.robot_history(robot_id, 500, start, end, offset)
            return json.dumps({'config': config, 'history': history, 'analytics': analytics, 'warning': warning, 'computed_at': utcnow()}, ensure_ascii=False, allow_nan=False).encode()
        return self.cached(('detail', robot_id, start, end, offset), read, 5)

    def export_robots(self, robot_id=None, start=None, end=None):
        with self.lock:
            return robots.export(self._robot_range(robot_id, start, end))

    def stream_robots(self, robot_id=None, start=None, end=None):
        query = self._robot_query(robot_id, start, end).order_by(Observation.timestamp, Observation.robot_id)
        def stream():
            yield '\ufeff' + robots.export([])
            with self.sessions() as session:
                for group in session.scalars(query).yield_per(1000).partitions(1000):
                    yield robots.export([r.data for r in group]).split('\n', 1)[1]
        return stream()

    def ingest_measurements(self, measurements, receipt_key=None):
        output = io.StringIO(newline='')
        fields = robots.FIELDS
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(measurements)
        return self.import_robots(output.getvalue().encode('utf-8'), 'API: JSON', receipt_key)

    def export(self):
        output = io.StringIO(newline="")
        fields = ["run_id", "source", "timestamp", "sim_time", "produced", "good", "rejected", "throughput", "wip", "availability", "quality"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        with self.sessions() as session:
            for row in session.scalars(select(History).order_by(History.id)).yield_per(1000):
                writer.writerow({"run_id": row.run_id, "source": row.source, **row.data})
        return output.getvalue()

    def start(self):
        self.runner_thread = threading.Thread(target=self._runner, name="simulation-runner", daemon=True)
        self.runner_thread.start()

    def _runner(self):
        last = time.monotonic()
        fractional = 0.0
        while not self.stop_event.wait(1.0):
            now = time.monotonic()
            elapsed, last = min(5.0, now - last), now
            try:
                # A lost database connection invalidates the single-writer lease.
                # Never reacquire transparently: another instance may now own the state.
                self._check_owner()
                from .emulation import tick as emulation_tick
                emulation_tick(self, elapsed)
                if now - getattr(self, '_scada_clock_checked', 0) >= 30:
                    from .scada import reconcile_clock
                    reconcile_clock(self)
                    self._scada_clock_checked = now
                with self.lock:
                    if self.source != "simulation" or not self.engine.running:
                        fractional = 0.0
                        self.runner_error = None
                        continue
                    fractional += elapsed * self.engine.speed
                    seconds = int(fractional)
                    fractional -= seconds
                    if seconds:
                        with self._mutation() as session:
                            self._advance(seconds, session)
                self.runner_error = None
            except Exception:
                self.runner_error = utcnow()
                logger.exception("Simulation step failed; durable snapshot retained")

    def close(self):
        self.stop_event.set()
        if self.runner_thread:
            self.runner_thread.join(timeout=10)
        if self.owner_connection is not None:
            self.owner_connection.close()
        self.database.dispose()



