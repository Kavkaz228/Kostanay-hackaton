"""Transparent descriptive statistics and guarded linear trend extrapolation."""
from datetime import datetime, timedelta
from statistics import fmean, pstdev

from .robots import SENSORS

LABELS = {
    'paint_volume_l': 'Остаток краски', 'paint_capacity_l': 'Объём ёмкости краски', 'electrode_count': 'Сварочные электроды',
    'welding_current_a': 'Сварочный ток', 'motor_current_a': 'Ток двигателя', 'speed_percent': 'Скорость', 'cycle_progress_pct': 'Выполнение цикла',
    'joint_temperature_c': 'Температура узла', 'vibration_mm_s': 'Вибрация',
    'hydraulic_pressure_bar': 'Гидравлическое давление',
    'pneumatic_pressure_bar': 'Пневматическое давление, бар',
    'pneumatic_pressure': 'Пневматическое давление, исходные единицы',
}


def analyze(rows, config):
    interval = config.get('expected_interval_seconds', 60)
    duration, unknown, states = 0.0, 0.0, {}
    for left, right in zip(rows, rows[1:]):
        dt = (datetime.fromisoformat(right['timestamp']) - datetime.fromisoformat(left['timestamp'])).total_seconds()
        duration += dt
        if dt > interval * 3:
            unknown += dt
        else:
            states[left['cycle_status']] = states.get(left['cycle_status'], 0) + dt
    results = {}
    for sensor in sorted(SENSORS):
        observed = [r for r in rows if r.get(sensor) is not None]
        values = [r[sensor] for r in observed]
        stats = {'count': len(values), 'missing': len(rows) - len(values), 'min': min(values) if values else None,
                 'max': max(values) if values else None, 'mean': fmean(values) if values else None,
                 'stddev': pstdev(values) if values else None, 'last': rows[-1].get(sensor) if rows else None,
                 'trend': None, 'trend_reason': 'Нужно минимум 6 измерений за 60 секунд без пропусков.'}
        # Only the last contiguous observed segment is eligible for a trend.
        segment = []
        for row in reversed(rows[-200:]):
            if row.get(sensor) is None:
                break
            if segment and (datetime.fromisoformat(segment[-1]['timestamp']) - datetime.fromisoformat(row['timestamp'])).total_seconds() > interval * 3:
                break
            segment.append(row)
        segment.reverse()
        if len(segment) >= 6:
            start = datetime.fromisoformat(segment[0]['timestamp'])
            times = [(datetime.fromisoformat(r['timestamp']) - start).total_seconds() for r in segment]
            if times[-1] >= 60:
                ys = [r[sensor] for r in segment]
                mx, my = fmean(times), fmean(ys)
                xx = sum((x - mx) ** 2 for x in times)
                slope = sum((x - mx) * (y - my) for x, y in zip(times, ys)) / xx
                intercept = my - slope * mx
                total = sum((y - my) ** 2 for y in ys)
                residual = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(times, ys))
                r2 = max(0.0, 1 - residual / total) if total > 0 else 1.0
                trend = {'per_minute': slope * 60, 'r_squared': r2, 'samples': len(segment),
                         'window_seconds': times[-1], 'crossing': None}
                stats['trend'] = trend
                stats['trend_reason'] = 'Линейный тренд наблюдений; не прогноз поломки.'
                if r2 >= .8 and abs(slope) > 1e-12:
                    candidates = []
                    for name, limit in config.get('limits', {}).get(sensor, {}).items():
                        if limit is None or (name.startswith('high') != (slope > 0)):
                            continue
                        eta = (limit - ys[-1]) / slope
                        # Forecast only a future threshold from the last measurement, up to 24h.
                        if 0 < eta <= 86400:
                            candidates.append((eta, name, limit))
                    if candidates:
                        eta, name, limit = min(candidates)
                        trend['crossing'] = {'limit': limit, 'kind': name, 'eta_minutes': eta / 60,
                            'timestamp': (datetime.fromisoformat(segment[-1]['timestamp']) + timedelta(seconds=eta)).isoformat()}
                elif r2 < .8:
                    stats['trend_reason'] = 'Тренд нестабилен (R² < 0,8); время достижения границы не вычисляется.'
        results[sensor] = stats
    return {'measurements': len(rows), 'duration_seconds': duration, 'unknown_seconds': unknown,
            'status_seconds': states, 'sensors': results,
            'interval_assumption': f'Статус удерживается до следующего наблюдения при разрыве не более {interval * 3} с; более длинные интервалы неизвестны.'}


def limit_predictions(row, config):
    result = []
    for sensor, limits in config.get('limits', {}).items():
        value = row.get(sensor)
        if value is None:
            continue
        for direction, compare in [('low', lambda v, limit: v <= limit), ('high', lambda v, limit: v >= limit)]:
            for level in ('critical', 'warning'):
                limit = limits.get(f'{direction}_{level}')
                if limit is not None and compare(value, limit):
                    result.append({'station_id': row['robot_id'], 'station_name': row['robot_id'],
                        'type': f'limit:{sensor}:{direction}', 'severity': level,
                        'title': f"{row['robot_id']}: {LABELS[sensor]} — {'ниже' if direction == 'low' else 'выше'} границы",
                        'description': f'Измерение {value:g}; заданная пользователем граница {limit:g}. Проверьте оборудование по регламенту.',
                        'eta_minutes': None, 'observed_at': row['timestamp']})
                    break
    return result

