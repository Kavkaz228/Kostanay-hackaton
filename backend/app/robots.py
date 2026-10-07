"""Robot observations are independent of production counters and model thresholds."""
import csv
import io
import math
from datetime import datetime, timezone
from uuid import uuid4

from .engine import utcnow
from .telemetry import ImportValidationError

HEADER = ['timestamp', 'robot_id', 'line_section', 'joint_temperature_c', 'vibration_mm_s', 'hydraulic_pressure_bar', 'cycle_status', 'error_code']
SENSORS = {'joint_temperature_c', 'vibration_mm_s', 'hydraulic_pressure_bar', 'pneumatic_pressure', 'pneumatic_pressure_bar'}
EXTRA_SENSORS = {'paint_volume_l', 'paint_capacity_l', 'electrode_count', 'welding_current_a', 'motor_current_a', 'speed_percent', 'cycle_progress_pct', *{f'joint_{i}_deg' for i in range(1, 7)}}
SENSORS |= EXTRA_SENSORS
METADATA = {'operation', 'controller_mode', 'safety_state'}
FIELDS = HEADER + ['pneumatic_pressure_bar', 'pneumatic_pressure'] + sorted(EXTRA_SENSORS) + sorted(METADATA)


def decode(content):
    if len(content) > 5 * 1024 * 1024:
        raise ImportValidationError([{'row': 0, 'message': 'Файл превышает 5 МБ'}])
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportValidationError([{'row': 0, 'message': 'Нужен CSV в кодировке UTF-8'}])


def is_robot_csv(content):
    try:
        header = next(csv.reader(io.StringIO(decode(content)), strict=True), [])
    except csv.Error:
        return False
    return bool({'robot_id', 'node_id'} & {v.strip() for v in header})


def validate(content):
    errors, rows = [], []
    try:
        reader = csv.reader(io.StringIO(decode(content)), strict=True)
        header = [v.strip() for v in next(reader, [])]
        # Accept the line continuation markers in the supplied chat example.
        if header:
            header[-1] = header[-1].removesuffix('\\')
        if 'robot_id' in header and 'node_id' in header:
            raise ValueError('Используйте один идентификатор: robot_id или node_id')
        header = ['robot_id' if v == 'node_id' else v for v in header]
        required = {'timestamp', 'robot_id', 'cycle_status'}
        allowed = required | SENSORS | {'line_section', 'error_code'} | METADATA
        if len(set(header)) != len(header) or not required <= set(header) or set(header) - allowed or not SENSORS & set(header):
            raise ValueError('Нужны timestamp, robot_id (или node_id), cycle_status и хотя бы один датчик. Неизвестные и повторные столбцы запрещены')
        for values in reader:
            number = reader.line_num
            if len(rows) + len(errors) >= 20000:
                raise ValueError('Не более 20 000 строк данных')
            try:
                if len(values) != len(header):
                    raise ValueError('Число значений не совпадает с заголовком')
                values = [v.strip() for v in values]
                values[-1] = values[-1].removesuffix('\\')
                row = dict(zip(header, values))
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError('timestamp должен содержать часовой пояс')
                row['timestamp'] = stamp.astimezone(timezone.utc).isoformat()
                for field in ('robot_id', 'cycle_status'):
                    if not row[field] or len(row[field]) > 128 or any(ord(c) < 32 for c in row[field]):
                        raise ValueError(f'{field}: требуется текст длиной 1–128 символов без управляющих символов')
                for field in ('line_section', 'error_code', *sorted(METADATA)):
                    row.setdefault(field, '')
                    if len(row[field]) > 128 or any(ord(c) < 32 for c in row[field]):
                        raise ValueError(f'{field}: максимум 128 символов без управляющих символов')
                for field in SENSORS:
                    value = row.get(field, '')
                    row[field] = float(value) if value else None
                    lower = -273.15 if field == 'joint_temperature_c' else -3600 if field.startswith('joint_') and field.endswith('_deg') else 0
                    upper = 100 if field in ('speed_percent', 'cycle_progress_pct') else 3600 if field.endswith('_deg') else 1e9
                    if row[field] is not None and (not math.isfinite(row[field]) or not lower <= row[field] <= upper):
                        raise ValueError(f'{field}: недопустимое числовое значение')
                    if field == 'electrode_count' and row[field] is not None:
                        if not row[field].is_integer():
                            raise ValueError('electrode_count: требуется целое количество')
                        row[field] = int(row[field])
                if row['paint_volume_l'] is not None and row['paint_capacity_l'] is not None and row['paint_volume_l'] > row['paint_capacity_l']:
                    raise ValueError('Остаток краски больше объёма ёмкости')
                for field, allowed_values in {'operation': ('', 'welding', 'painting', 'assembly'), 'controller_mode': ('', 'automatic', 'manual', 'offline', 'unknown'), 'safety_state': ('', 'normal', 'protective_stop', 'emergency_stop', 'unknown')}.items():
                    if row[field] not in allowed_values:
                        raise ValueError(f'{field}: допустимы {", ".join(allowed_values[1:])}')
                if all(row[field] is None for field in SENSORS):
                    raise ValueError('В строке нет ни одного измерения датчика')
                row['row'] = number
                rows.append(row)
            except (ValueError, OverflowError) as exc:
                errors.append({'row': number, 'message': str(exc)})
    except (csv.Error, ValueError) as exc:
        errors.append({'row': 1, 'message': str(exc)})
    if not rows and not errors:
        errors.append({'row': 2, 'message': 'CSV не содержит измерений'})
    seen = set()
    for row in rows:
        key = (row['robot_id'], row['timestamp'])
        if key in seen:
            errors.append({'row': row['row'], 'message': 'Повтор timestamp и robot_id'})
        seen.add(key)
    if errors:
        raise ImportValidationError(errors[:100])
    return sorted(rows, key=lambda r: (r['timestamp'], r['robot_id']))


def predictions(row, config=None):
    from .analytics import limit_predictions
    config = config or {}
    result = limit_predictions(row, config)
    status = row['cycle_status'].casefold()
    error = row['error_code']
    has_error = error.casefold() not in ('', '0', 'none', 'null', 'ok')
    warning = status in ('warning', 'предупреждение')
    critical = status in ('error', 'fault', 'alarm', 'ошибка', 'авария')
    if not (has_error or warning or critical):
        return result
    definition = config.get('error_codes', {}).get(error)
    description = (definition['description'] + (' Рекомендация: ' + definition['action'] if definition.get('action') else '')) if definition else 'Расшифровка кода требует справочника оборудования.'
    return result + [{'station_id': row['robot_id'], 'station_name': row['robot_id'],
        'type': 'robot_error:' + error if has_error else 'robot_status',
        'severity': 'critical' if critical else definition['severity'] if definition else 'warning',
        'title': f"{row['robot_id']}: {error if has_error else row['cycle_status']}",
        'description': f"Статус из CSV: {row['cycle_status']}. Код: {error or 'не указан'}. Участок: {row['line_section'] or 'не указан'}. {description}",
        'eta_minutes': None, 'observed_at': row['timestamp']}]


def build(rows, engine, run_id=None, configs=None):
    configs = configs or {}
    latest = {r['robot_id']: r for r in rows}
    start, end = rows[0]['timestamp'], rows[-1]['timestamp']
    return {'run_id': run_id or str(uuid4()), 'source': 'telemetry', 'telemetry_kind': 'robots',
        'updated_at': utcnow(), 'telemetry_updated_at': end, 'running': False, 'speed': engine.speed,
        'sim_time': (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(),
        'shift_duration': engine.shift_duration, 'shift_plan': engine.shift_plan, 'stations': [],
        'robots': list(latest.values()), 'observation_count': len(rows), 'observation_start': start,
        'metrics': {k: None for k in ('produced', 'good', 'rejected', 'wip', 'throughput', 'availability', 'performance', 'quality', 'oee', 'plan_progress', 'forecast_good')},
        'predictions': current_predictions(rows, configs),
        'model': {'name': 'Измерения роботов', 'description': 'История датчиков, заданные пользователем границы, справочник кодов и линейные тренды. Производственные показатели не вычисляются без счётчиков. Время достижения границы — экстраполяция измерений, не прогноз поломки.'}}


def current_predictions(rows, configs):
    latest, sensor_rows = {}, {}
    for row in rows:
        latest[row['robot_id']] = row
        for sensor in configs.get(row['robot_id'], {}).get('limits', {}):
            if row.get(sensor) is not None:
                sensor_rows[(row['robot_id'], sensor)] = row
    result = [p for robot, row in latest.items() for p in predictions(row, configs.get(robot)) if not p['type'].startswith('limit:')]
    from .analytics import limit_predictions
    for (robot, sensor), row in sensor_rows.items():
        config = {'limits': {sensor: configs[robot]['limits'][sensor]}}
        result.extend(limit_predictions(row, config))
    return result


def export(rows):
    output = io.StringIO(newline='')
    fields = FIELDS
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formula execution without changing stored identifiers.
        writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k, v in row.items()})
    return output.getvalue()
