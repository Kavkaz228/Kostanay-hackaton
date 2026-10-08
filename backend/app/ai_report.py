"""Source-isolated facts and deterministic advice for the local assistant.

This layer performs no inference, simulation steps, scans or equipment actions.
The same bounded facts are shown to the operator and supplied to the model.
"""
import copy
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select

from .analytics import LABELS, limit_predictions
from .automation import robot_snapshot
from .database import QualityRecord, ScadaRecord
from .robots import SENSORS, predictions as robot_predictions
from .scada import ai_context

UNITS = {'joint_temperature_c': '°C', 'vibration_mm_s': 'мм/с',
         'hydraulic_pressure_bar': 'бар', 'pneumatic_pressure_bar': 'бар',
         'paint_volume_l': 'л', 'paint_capacity_l': 'л', 'electrode_count': 'шт.',
         'welding_current_a': 'А', 'motor_current_a': 'А', 'speed_percent': '%',
         'cycle_progress_pct': '%'}
LABELS_BY_SOURCE = {'plant': 'Фактические данные', 'simulation': 'Симуляция производства',
                    'emulation': 'Учебный стенд SCADA'}
PRIORITY = {'critical': 0, 'alarm': 0, 'warning': 1, 'warn': 1, 'info': 2, 'ok': 3}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _pick(data, keys):
    return {k: copy.deepcopy(data[k]) for k in keys if k in data}


def build_context(twin, source, robot_id=None, allow_command=False):
    if source not in LABELS_BY_SOURCE:
        raise HTTPException(422, 'Неизвестный источник данных')
    if source != 'plant' and allow_command:
        raise HTTPException(422, 'Учебные источники не создают физические команды')
    context = {'data_source': source, 'captured_at': now_iso(),
               'command_proposal_allowed': bool(allow_command and source == 'plant')}
    if source == 'simulation':
        if robot_id is not None:
            raise HTTPException(422, 'В симуляции анализируется производственная линия')
        with twin.lock:
            # state() follows the selected dashboard source, which may be telemetry.
            state = twin.engine.public_state()
        context['simulation'] = state
        return context
    if source == 'emulation':
        if robot_id not in (None, 'R1', 'R2', 'R3', 'R4', 'CV'):
            raise HTTPException(422, 'Выберите R1–R4 или CV учебного стенда')
        from .emulation import current
        from .emulation_model import Stand
        publication = current(twin)
        snapshot = json.loads(publication['view'])
        calendar = Stand(copy.deepcopy(publication['data']))
        components = [c for c in snapshot['components'] if robot_id is None or c['robot'] == robot_id]
        components.sort(key=lambda c: (PRIORITY.get(c['level'], 2), c['remaining_hours'] if c['remaining_hours'] is not None else float('inf')))
        selected = []
        for c in components[:12]:
            component = _pick(c, ('id', 'robot', 'node', 'name', 'level', 'wear_pct', 'remaining_hours', 'load_pct', 'forecast', 'action', 'part'))
            sensors = sorted(c['sensors'], key=lambda s: PRIORITY.get(s['level'], 2))
            component['sensors'] = [_pick(s, ('k', 'label', 'unit', 'value', 'level', 'sampled_at', 'warn', 'alarm', 'lo')) for s in sensors[:6]]
            for sensor in component['sensors']:
                sampled = sensor.get('sampled_at')
                # The stand stores elapsed working hours, not Unix timestamps.
                sensor['sampled_at'] = calendar.clock(sampled) if isinstance(sampled, (int, float)) else None
            selected.append(component)
        parts = [p for p in snapshot['parts'] if p.get('deficit', 0) > 0]
        summary = copy.deepcopy(snapshot['summary'])
        if not summary.get('has_production'):
            for key in ('availability_pct', 'quality_pct', 'performance_pct', 'oee_pct'):
                summary[key] = None
            summary['text'] = 'Выпуск ещё не накоплен; OEE, доступность, производительность и качество не оцениваются.'
        stand = _pick(snapshot, ('model_time', 'paused', 'line', 'revision', 'cars', 'rejected', 'run_hours', 'down_hours', 'stops'))
        stand.update(source='emulation', selected_robot=robot_id, summary=summary,
            notice='Все показания, ресурс, остатки, поставщики и цены относятся только к учебному стенду.',
            production_scope='whole_stand', inventory_scope='whole_stand',
            component_count=len(components), component_sample_limit=12, components=selected,
            deviation_count=sum(c['level'] != 'ok' for c in components),
            alarms=[a for a in snapshot['alarms'] if robot_id is None or a['component_id'].startswith(robot_id + '-')][:12],
            shortage_count=len(parts), shortage_sample_limit=10,
            shortages=[_pick(p, ('id', 'name', 'stock', 'minimum', 'needed_90d', 'in_transit', 'deficit', 'recommended_offer')) for p in parts[:10]],
            conveyor=snapshot['conveyor'] if robot_id in (None, 'CV') else None, commands_allowed=False)
        context['stand'] = stand
        return context
    with twin.lock:
        all_robots = robot_snapshot(twin, robot_id)
        with twin.sessions() as db:
            query = select(ScadaRecord).where(ScadaRecord.kind == 'monitor_alert', ScadaRecord.data['status'].as_string() == 'active')
            if robot_id:
                query = query.where(ScadaRecord.owner == robot_id)
            count = db.scalar(select(func.count()).select_from(query.subquery()))
            alert_rows = db.scalars(query.order_by(ScadaRecord.data['severity'].as_string(), ScadaRecord.created_at.desc()).limit(200))
            alerts = [_pick(r.data, ('id', 'robot_id', 'type', 'severity', 'title', 'description', 'observed_at', 'acknowledged', 'recommendation')) for r in alert_rows]
        alerts.sort(key=lambda a: (PRIORITY.get(a['severity'], 2), a.get('acknowledged', False)))
        important = {a['robot_id'] for a in alerts}
        all_robots.sort(key=lambda r: (r['robot_id'] not in important, r['connection'] == 'fresh', r['robot_id']))
        robots = [_pick(r, tuple(k for k in r if k not in ('row', 'run_id'))) for r in all_robots[:10]]
        identifiers = {r['robot_id'] for r in robots}
        configs = {key: copy.deepcopy(twin.robot_configs.get(key, {})) for key in identifiers}
        # Partial packets must not erase a sensor's last known reading.
        sensors = {}
        for row in twin.robot_rows:
            if row['robot_id'] not in identifiers:
                continue
            for sensor in sorted(SENSORS):
                if row.get(sensor) is not None:
                    sensors[(row['robot_id'], sensor)] = {'robot_id': row['robot_id'], 'sensor': sensor,
                        'value': row[sensor], 'timestamp': row['timestamp']}
        production = copy.deepcopy(twin.production_telemetry)
        if production:
            production = _pick(production, ('source', 'telemetry_updated_at', 'stations', 'metrics', 'model'))
    # Cached readers acquire their own locks before the service lock. Do not
    # invert that order by calling them while holding twin.lock.
    manufacturing = twin.manufacturing()
    quality = _pick(manufacturing['quality'], ('total', 'quantity', 'rejected', 'accepted', 'groups'))
    quality['groups'] = quality['groups'][:20]
    with twin.sessions() as db:
        quality['period_start'], quality['period_end'] = db.execute(select(func.min(QualityRecord.timestamp), func.max(QualityRecord.timestamp))).one()
    report = manufacturing['report']
    if report:
        report = _pick(report, ('lines', 'downtimes', 'plans', 'quality', 'targets', 'planned_total', 'plan_gap', 'warnings', 'imported_at', 'filename'))
        for field in ('lines', 'downtimes', 'plans', 'quality'):
            report[field] = report.get(field, [])[-30:]
    maintenance = copy.deepcopy(ai_context(twin))
    context.update(selected_robot=robot_id, robots=robots, robot_count=len(all_robots), robot_sample_limit=10,
        robot_configs=configs, sensor_readings=list(sensors.values())[:120], daily_report=report,
        vehicle_quality=quality, maintenance_and_stock=maintenance, production_telemetry=production,
        quality_and_inventory_scope='whole_installation',
        monitor_alerts=alerts[:12], monitor_active_count=count)
    return context


def _fmt(value):
    if value is None:
        return 'не рассчитан'
    return f'{value:.2f}'.rstrip('0').rstrip('.') if isinstance(value, (float, int)) else str(value)


def build_report(context):
    source = context['data_source']
    report = dict(data_source=source, source_label=LABELS_BY_SOURCE[source], captured_at=context.get('captured_at', now_iso()),
        observed_at=None, has_data=False, notice='', metrics=[], readings=[], findings=[], missing_data=[], next_steps=[])

    def metric(key, label, value, unit='', detail=''):
        report['metrics'].append(dict(key=key, label=label, value=value, unit=unit, detail=detail))

    def reading(identifier, asset, label, value, unit='', observed_at=None, status=''):
        report['readings'].append(dict(id=identifier, asset=asset, label=label, value=value,
            unit=unit, observed_at=observed_at, status=status))

    def finding(identifier, title, severity, evidence, recommendation):
        if any(f['id'] == identifier for f in report['findings']):
            return
        report['findings'].append(dict(id=identifier, title=title, severity=severity,
            evidence=evidence, recommendation=recommendation))

    if source == 'emulation':
        stand = context['stand']
        report.update(has_data=bool(stand['component_count']), observed_at=stand['model_time'], notice=stand['notice'])
        metric('line_state', 'Состояние учебной линии', 'Модельное время на паузе' if stand['paused'] else 'Работает' if stand['line'] == 'run' else 'Остановлена')
        metric('components', 'Узлов в выбранном контуре', stand['component_count'], 'шт.', f"В анализе {len(stand['components'])} приоритетных узлов.")
        metric('deviations', 'Узлов с отклонениями', stand['deviation_count'], 'шт.')
        metric('cars', 'Выпуск учебной линии', stand.get('cars'), 'авт.', 'Весь стенд, накопительный итог модельного периода.')
        metric('rejected', 'Брак учебной линии', stand.get('rejected'), 'авт.')
        metric('downtime', 'Простой учебной линии', stand.get('down_hours'), 'ч')
        metric('oee', 'OEE учебной модели', stand['summary'].get('oee_pct'), '%', 'Не рассчитано до первого выпуска.' if not stand['summary'].get('has_production') else 'Расчёт виртуальных циклов; не заводской OEE.')
        metric('shortages', 'Дефицитных позиций на складе', stand['shortage_count'], 'шт.', 'Общий учебный склад всего стенда.')
        for c in stand['components']:
            reading(c['id'] + ':wear', c['id'], c['name'] + ' · расчётный износ', c['wear_pct'], '%', stand['model_time'], c['level'])
            deviations = []
            for sensor in c['sensors'][:2]:
                reading(c['id'] + ':' + sensor['k'], c['id'], sensor['label'], sensor.get('value'), sensor.get('unit', ''), sensor.get('sampled_at'), sensor['level'])
            for sensor in c['sensors']:
                if sensor['level'] not in ('warn', 'alarm') or sensor.get('value') is None:
                    continue
                boundary = sensor.get(sensor['level'])
                text = f"{sensor['label']}: {_fmt(sensor['value'])} {sensor.get('unit', '')}"
                if boundary is not None:
                    text += f"; учебная граница {'снизу' if sensor.get('lo') else 'сверху'} {_fmt(boundary)}"
                deviations.append(text)
            if c['level'] != 'ok':
                evidence = '; '.join(deviations) or f"Расчётный износ {_fmt(c['wear_pct'])}%."
                if c.get('remaining_hours') is not None:
                    evidence += f" Остаточный ресурс модели {_fmt(c['remaining_hours'])} рабочих ч; это не время до гарантированного отказа."
                finding('component:' + c['id'], c['id'] + ' · ' + c['name'], 'critical' if c['level'] == 'alarm' else 'warning', evidence,
                    c.get('action') or 'Проверьте карточку узла и запланируйте учебное обслуживание с учётом остатка ресурса и запчастей.')
        for a in stand['alarms']:
            finding('alarm:' + a['id'], 'Зафиксированная авария: ' + a['component_id'], 'warning' if a.get('recovered') else 'critical',
                a.get('label', '') + ('; показание восстановилось, событие ещё не квитировано.' if a.get('recovered') else '; событие учебного стенда.'),
                'Проверьте карточку узла и подтвердите восстановление в стенде.' if a.get('recovered') else a.get('action') or 'Разберите причину по карточке узла учебного стенда.')
        for p in stand['shortages']:
            finding('stock:' + p['id'], 'Учебный склад: ' + p['id'], 'warning',
                f"{p['name']}: остаток {p['stock']}, нужно на 90 рабочих дней {p['needed_90d']}, в пути {p['in_transit']}, дефицит {p['deficit']}.",
                f"Проверьте учебное предложение для {p['id']} и подготовьте пополнение на {p['deficit']} шт. в разделе эмуляции.")
        if not stand['summary'].get('has_production'):
            report['missing_data'].append('В учебном периоде ещё нет выпуска для оценки OEE и качества.')
        if stand['paused']:
            report['next_steps'].append('В разделе «Оборудование и ТО → Эмуляция» запустите модельное время, чтобы увидеть изменение показаний и выпуска.')
        report['next_steps'].append('Откройте приоритетный узел стенда, проверьте его датчики и выполните предложенное учебное обслуживание.')
    elif source == 'simulation':
        state = context['simulation']
        m = state['metrics']
        report.update(has_data=True, observed_at=state.get('updated_at'), notice='Синтетическая производственная линия. Все числа относятся к модели; это не показания физических роботов.')
        elapsed = state.get('sim_time', 0)
        for key, label, unit in [('good', 'Годный выпуск', 'шт.'), ('rejected', 'Брак всей линии', 'шт.'), ('wip', 'Незавершённое производство', 'шт.'), ('throughput', 'Темп годного выпуска', 'шт./ч'), ('plan_progress', 'Выполнение плана модели', '%'), ('quality', 'Качество модели', '%'), ('oee', 'OEE модели', '%')]:
            value = m.get(key)
            if key in ('quality', 'oee') and not m.get('good', 0) + m.get('rejected', 0):
                value = None
            metric(key, label, value, unit, f'Модельное время {_fmt(elapsed / 3600)} ч; план {state["shift_plan"]} шт.' if key == 'plan_progress' else '')
        metric('downtime', 'Сумма ручных простоев участков', sum(s.get('downtime_seconds', 0) for s in state['stations']) / 60, 'мин', 'Не календарный простой всей линии; сумма по рабочим местам.')
        for s in state['stations']:
            for key, label, unit in [('completed', 'Обработано', 'шт.'), ('rejected', 'Брак участка', 'шт.'), ('queue', 'Буфер', 'шт.'), ('cycle_seconds', 'Базовый цикл', 'с'), ('throughput', 'Темп годного выпуска участка', 'шт./ч'), ('downtime_seconds', 'Ручной простой участка', 'с')]:
                reading(s['id'] + ':' + key, s['name'], label, s.get(key), unit, state.get('updated_at'), s.get('status', ''))
        recommendations = {'buffer_full': 'Сравните такт соседних участков и заполнение буфера; проверьте вариант балансировки в разделе сценариев.',
            'blocked': 'Проверьте заполнение следующего буфера и пропускную способность следующего участка в модели.',
            'starved': 'Проверьте выпуск и доступность предыдущего участка модели.',
            'manual_stop': 'Проверьте причину ручной остановки модели и условия возобновления.',
            'supply_stop': 'Оцените запас изделий перед остановленным участком и последствия для следующих участков.',
            'quality': 'Сравните брак участка с заданной вероятностью и проверьте отдельный сценарий улучшения качества.'}
        for p in state.get('predictions', []):
            finding('simulation:' + p['station_id'] + ':' + p['type'], p['station_name'] + ' · ' + p['title'], p['severity'], p['description'], recommendations.get(p['type'], 'Проверьте настройки участка и сравните варианты в разделе сценариев.'))
        bottleneck = min(state['stations'], key=lambda s: s['capacity'] * s['speed_factor'] / s['cycle_seconds']) if state['stations'] else None
        if bottleneck:
            capacity = bottleneck['capacity'] * bottleneck['speed_factor'] * 3600 / bottleneck['cycle_seconds']
            finding('simulation:bottleneck', 'Ограничение мощности: ' + bottleneck['name'], 'info',
                f"Минимальная настроенная мощность {_fmt(capacity)} шт./ч; цикл {bottleneck['cycle_seconds']} с, параллельных мест {bottleneck['capacity']}.",
                'Сравните сценарий изменения такта или мощности этого участка с исходным: оцените годный выпуск и накопление изделий в буферах.')
        if elapsed >= state['shift_duration']:
            finding('simulation:shift', 'Модельная смена завершена', 'info',
                f"Годных {m['good']} при плане {state['shift_plan']}; отклонение {m['good'] - state['shift_plan']:+d} шт.",
                'Сохраните итоги смены; новую модельную смену запускайте после проверки выбранных параметров.')
        report['next_steps'].append('Сравните проблемный участок в разделе «Сценарии»; проверяйте эффект по приросту годного выпуска и НЗП.')
    else:
        robots = context.get('robots', [])
        quality = context.get('vehicle_quality', {})
        maintenance = context.get('maintenance_and_stock', {})
        daily = context.get('daily_report')
        production = context.get('production_telemetry')
        alarms = context.get('monitor_alerts', [])
        report.update(has_data=bool(robots or quality.get('total') or daily or production or maintenance.get('totals', {}).get('components') or maintenance.get('shortages') or alarms),
            notice='Загруженные измерения, отчёты и учёт установки. Время каждого показания указано отдельно; источники могут охватывать разные периоды. Качество и склад относятся ко всей установке.')
        if not report['has_data']:
            report['notice'] = 'В фактическом источнике ещё нет измерений роботов, отчётов или записей учёта. Подключите данные либо выберите учебный источник.'
        observed = [r['timestamp'] for r in robots] + [a['observed_at'] for a in alarms if a.get('observed_at')]
        report['observed_at'] = max(observed) if observed else production.get('telemetry_updated_at') if production else quality.get('period_end') or (daily.get('imported_at') if daily else None)
        metric('robots', 'Роботов с показаниями', context.get('robot_count', len(robots)), 'шт.', f'В анализ передано {len(robots)} роботов.')
        metric('active_alerts', 'Активных событий мониторинга', context.get('monitor_active_count', len(alarms)), 'шт.')
        metric('quality_total', 'Записей контроля качества', quality.get('total', 0), 'шт.')
        for key, label in [('quantity', 'Проверено изделий'), ('accepted', 'Принято изделий'), ('rejected', 'Отклонено изделий')]:
            period = f" Период: {quality['period_start']} — {quality['period_end']}." if quality.get('period_start') else ''
            metric(key, label, quality.get(key) if quality.get('total') else None, 'шт.', 'Все загруженные записи качества; отдельный период от роботов.' + period)
        metric('shortages', 'Дефицитных позиций', maintenance.get('totals', {}).get('shortages', 0), 'шт.')
        for a in alarms:
            finding('monitor:' + a['id'], a['title'], a['severity'], a['description'], a.get('recommendation') or 'Проверьте событие в разделе «Мониторинг роботов».')
        known = {(a['robot_id'], a['type']) for a in alarms}
        now = datetime.fromisoformat(context.get('captured_at', report['captured_at']))
        for row in context.get('sensor_readings', []):
            config = context.get('robot_configs', {}).get(row['robot_id'], {})
            age = (now - datetime.fromisoformat(row['timestamp'])).total_seconds()
            status = 'future' if age < -5 else 'stale' if age > config.get('expected_interval_seconds', 60) * 3 else 'fresh'
            prediction_row = {'robot_id': row['robot_id'], 'timestamp': row['timestamp'], row['sensor']: row['value']}
            deviations = limit_predictions(prediction_row, {'limits': {row['sensor']: config.get('limits', {}).get(row['sensor'], {})}})
            reading(row['robot_id'] + ':' + row['sensor'], row['robot_id'], LABELS.get(row['sensor'], 'Положение оси ' + row['sensor'][6:7] if row['sensor'].startswith('joint_') else row['sensor']), row['value'], UNITS.get(row['sensor'], '°' if row['sensor'].endswith('_deg') else ''), row['timestamp'], status)
            for p in deviations:
                if (row['robot_id'], p['type']) not in known:
                    finding(row['robot_id'] + ':' + p['type'], p['title'], p['severity'], p['description'] + (' Последнее известное показание устарело; текущее состояние неизвестно.' if status != 'fresh' else ''), 'Сверьте показание и настроенную границу с паспортом оборудования; проверьте датчик и условия измерения по регламенту.')
        for robot in robots:
            reading(robot['robot_id'] + ':state', robot['robot_id'], 'Состояние контроллера', robot['cycle_status'], '', robot['timestamp'], robot.get('connection', ''))
            config = context.get('robot_configs', {}).get(robot['robot_id'], {})
            for p in robot_predictions(robot, {'error_codes': config.get('error_codes', {})}):
                if (robot['robot_id'], p['type']) not in known:
                    finding(robot['robot_id'] + ':' + p['type'], p['title'], p['severity'], p['description'], 'Сверьте состояние и код с журналом и руководством контроллера; передайте сведения ответственному специалисту.')
            if robot.get('connection') != 'fresh' and (robot['robot_id'], 'telemetry_' + robot.get('connection', 'stale')) not in known:
                finding('connection:' + robot['robot_id'], robot['robot_id'] + ' · проверьте время и связь', 'warning', f"Последнее показание {robot['timestamp']}; состояние связи: {robot.get('connection')}.", 'Проверьте поступление пакетов, питание шлюза, часовой пояс и синхронизацию часов контроллера.')
            if not any(v is not None for limit in config.get('limits', {}).values() for v in limit.values()):
                report['missing_data'].append(robot['robot_id'] + ': не заданы границы показателей для оценки отклонений.')
        for p in maintenance.get('shortages', []):
            finding('stock:' + p['id'], 'Дефицит запчасти ' + p['id'], 'warning', f"{p['name']}: остаток {p['stock']}, нужно {p['needed']}, в заказе {p['on_order']}, дефицит {p['deficit']}.", f"Сверьте учёт {p['id']} и предложения поставщиков; подготовьте заявку на покрытие {p['deficit']} шт. после проверки потребности.")
        for c in maintenance.get('components', []):
            if c['severity'] in ('alarm', 'warning'):
                finding('maintenance:' + c['id'], 'Обслуживание: ' + c['name'], 'critical' if c['severity'] == 'alarm' else 'warning', f"Узел {c['id']}: расчётный износ {_fmt(c.get('wear_pct'))}%, остаток {_fmt(c.get('remaining_hours'))} ч; свежесть {c.get('connection')}.", 'Откройте карточку узла, проверьте основания расчёта ресурса и запланируйте обслуживание по регламенту.')
        if daily:
            for i, line in enumerate(daily.get('lines', [])[:8]):
                reading('daily:' + str(i), line['line'], 'Суточный факт / план', f"{line['actual']} / {line['plan']}", 'шт.', line['date'], 'daily_report')
                if line['actual'] < line['plan']:
                    finding('daily:' + str(i), line['line'] + ' · отставание от суточного плана', 'warning', f"{line['date']}: факт {line['actual']}, план {line['plan']}, недовыпуск {line['plan'] - line['actual']} шт.", 'Сопоставьте простои и выпуск за ту же дату; проверьте причины в журнале смены перед изменением плана.')
        if production:
            for station in production.get('stations', []):
                reading('production:' + station['id'], station['name'], 'Накопительный выпуск участка', station.get('completed'), 'шт.', station.get('observed_at'), station.get('status', ''))
        # Keep a large sensor fleet from hiding all plan/fact evidence.
        production_readings = [r for r in report['readings'] if r['id'].startswith(('daily:', 'production:'))][:16]
        robot_readings = []
        for robot in robots:
            own = [r for r in report['readings'] if r['asset'] == robot['robot_id']]
            own.sort(key=lambda r: (r['id'] != robot['robot_id'] + ':state', r['status'] == 'fresh'))
            robot_readings.extend(own[:4])
        report['readings'] = production_readings + robot_readings[:36 - len(production_readings)]
        if not robots:
            report['missing_data'].append('Нет загруженных показаний роботов.')
            report['next_steps'].append('В разделе «Данные» импортируйте CSV с timestamp, robot_id, cycle_status и показаниями датчиков либо подключите отправку измерений от шлюза.')
        if not quality.get('total'):
            report['missing_data'].append('Нет записей контроля качества; отсутствие записей не означает нулевой брак.')
        if not daily and not production:
            report['missing_data'].append('Не загружены суточный отчёт и производственные измерения для анализа плана и простоев.')
        if not report['has_data']:
            report['next_steps'].extend(['После загрузки роботов задайте ожидаемый интервал и границы по паспорту оборудования; проверьте появление событий в «Мониторинг роботов».', 'Для анализа уже имеющихся учебных данных явно выберите «Симуляция производства» или «Учебный стенд SCADA».'])
        else:
            report['next_steps'].append('Откройте приоритетное событие в «Мониторинг роботов», проверьте время показаний и назначьте сотруднику проверку через «Принять в работу».')
    report['metrics'] = report['metrics'][:8]
    report['readings'] = report['readings'][:36]
    report['findings'].sort(key=lambda f: PRIORITY.get(f['severity'], 2))
    report['findings'] = report['findings'][:12]
    report['missing_data'] = list(dict.fromkeys(report['missing_data']))[:12]
    report['next_steps'] = list(dict.fromkeys([f['recommendation'] for f in report['findings'][:3]] + report['next_steps']))[:6]
    return report
