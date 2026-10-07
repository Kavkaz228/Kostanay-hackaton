"""Forecasting, internal notices and purchasing for the virtual SCADA stand.

The catalogue and deterministic rules come from the supplied scada-stand.zip.
They have no network clients and cannot create plant orders or send messages.
"""
import re

SUPPLIERS = {
    'A': 'РобоЗапчасть', 'B': 'ПромПривод-Сервис', 'C': 'ВакуумТехника',
    'D': 'КабельСистемы', 'E': 'Метрология-Центр', 'F': 'ПневмоСнаб',
    'G': 'Служба ТО цеха',
}
CHANNELS = ('alarms', 'maintenance', 'stock', 'summary')
BASIS = {
    'motor': 'Температура обмотки; удвоение темпа износа при +10 °C',
    'reducer': 'Нагрузка в степени 10/3 и относительная скорость',
    'brake': 'Количество срабатываний тормоза', 'cable': 'Циклы изгиба',
    'battery': 'Разряд батареи', 'fan': 'Наработка вентилятора',
    'cvmot': 'Нагрузка привода', 'cvgbx': 'Нагрузка в степени 10/3 и состояние масла',
    'cvoil': 'Интервал замены масла 5000 ч', 'cvbrk': 'Наработка и остановки',
    'vfd': 'Температура радиатора', 'sprk': 'Износ цепи',
    'chain': 'Пробег, нагрузка и подача смазки', 'slats': 'Наработка',
    'rollers': 'Наработка роликов', 'lube': 'Расход смазки',
    'take': 'Наработка натяжителя', 'enc': 'Наработка измерительного колеса',
    'pos': 'Интервал юстировки 3000 ч',
}


def migrate(state):
    """Add only missing fields; never reset a saved stand during an upgrade."""
    state.setdefault('orders', [])
    state.setdefault('order_sequence', 0)
    state.setdefault('notifications', [])
    state.setdefault('notification_sequence', 0)
    state.setdefault('channels', {k: True for k in CHANNELS})
    for key in CHANNELS:
        state['channels'].setdefault(key, True)
    state.setdefault('stock_low', {})
    state.setdefault('trips', {a['id']: 1 for a in state.get('alarms', [])})
    state['version'] = 2


def remaining(model, component):
    rate = model.rate(component, True)
    return max(0, 1 - component['wear']) / rate if rate > 0 else None


def forecast(model, component):
    hours = remaining(model, component)
    beyond = hours is None or hours > 16000
    basis = BASIS.get(model.kind(component)) or (
        f"Интервал {component['hours']:g} ч" if component.get('hours') else 'Количество циклов')
    return dict(remaining_hours=hours, working_days=hours / 16 if hours is not None else None,
                **{'from': None if beyond else model.clock(model.s['t'] + hours * .8),
                   'to': None if beyond else model.clock(model.s['t'] + hours * 1.2)},
                after=model.clock(model.s['t'] + 16000) if beyond else None,
                open_ended=beyond, basis=basis, uncertainty_pct=20)


def notify(model, channel, level, text):
    s = model.s
    s['notification_sequence'] += 1
    s['notifications'].insert(0, dict(id=s['notification_sequence'], t=s['t'],
        channel=channel, level=level, text=text, status='recorded' if s['channels'][channel] else 'muted',
        external_delivery=False, read=False, read_at=None))
    del s['notifications'][120:]


def needs(model):
    """Calculate demand once for all parts, not once per offer/component."""
    result = {key: dict(count=0, hours=None, components=[]) for key in model.parts}
    for c in model.cs:
        hours = remaining(model, c)
        if c['wear'] >= .8 or hours is not None and hours <= 90 * 16:
            item = result[c['part']]
            item['count'] += 1
            item['components'].append(c['id'])
            if hours is not None and (item['hours'] is None or hours < item['hours']):
                item['hours'] = hours
    return result


def parts_view(model):
    demand = needs(model)
    transit = {key: 0 for key in model.parts}
    for order in model.s['orders']:
        if order['status'] == 'in_transit':
            transit[order['part']] += order['quantity']
    result = []
    for key, p in model.parts.items():
        need = demand[key]
        service = p.get('service', False)
        stock, minimum = model.s['stock'][key], p.get('min', 0)
        offers = [dict(supplier_id=sup, supplier=SUPPLIERS[sup], price_kzt=price,
                       lead_workdays=lead) for sup, price, lead in p['offers']]
        cheapest = min(offers, key=lambda x: (x['price_kzt'], x['lead_workdays']))
        fastest = min(offers, key=lambda x: (x['lead_workdays'], x['price_kzt']))
        covered = not service and stock + transit[key] >= need['count']
        days = need['hours'] / 16 if need['hours'] is not None else None
        if not covered and fastest != cheapest and days is not None and days < cheapest['lead_workdays'] + 3:
            chosen = fastest
            reason = (f"Замена ожидается через {days:.1f} раб. дн.; выбран минимальный срок "
                      f"{fastest['lead_workdays']} раб. дн. вместо {cheapest['lead_workdays']}.")
        else:
            chosen = cheapest
            reason = 'Потребность покрыта запасом и поставками; выбрана минимальная цена.' if covered else 'Выбрана минимальная цена из учебного каталога.'
        if days is not None and not covered and chosen['lead_workdays'] > days:
            reason += ' Даже самая быстрая поставка может не успеть к прогнозируемому сроку ТО.'
        result.append(dict(id=key, name=p['name'], stock=stock, service=service, minimum=minimum,
            needed_90d=need['count'], required_components=need['components'],
            needed_in_workdays=days, in_transit=transit[key],
            deficit=0 if service else max(0, need['count'] + minimum - stock - transit[key]),
            offers=offers, recommended_offer=dict(**chosen, reason=reason),
            catalogue='training', currency='KZT'))
    return result


def place_order(model, part, catalog=None):
    catalog = catalog if catalog is not None else parts_view(model)
    p = next((p for p in catalog if p['id'] == part), None)
    if p is None or p['service']:
        raise ValueError('Выберите материальную деталь из учебного каталога.')
    # A direct, deliberate command can order one spare even when demand is
    # covered, matching the archive. Automatic deficit orders never do this.
    offer, quantity = p['recommended_offer'], max(1, p['deficit'])
    if p['stock'] + p['in_transit'] + quantity > 10000:
        raise ValueError('Предельный учебный запас — 10 000 деталей с учётом поставок.')
    s = model.s
    if sum(o['status'] == 'in_transit' for o in s['orders']) >= 200:
        raise ValueError('Не более 200 активных учебных заказов; дождитесь поставок.')
    s['order_sequence'] += 1
    eta_t = s['t'] + offer['lead_workdays'] * 16
    order = dict(id=f"SIM-{s['order_sequence']:06d}", part=part, quantity=quantity,
        supplier_id=offer['supplier_id'], supplier=offer['supplier'], unit_price_kzt=offer['price_kzt'],
        total_kzt=offer['price_kzt'] * quantity, lead_workdays=offer['lead_workdays'],
        ordered_at=model.clock(), eta=model.clock(eta_t), eta_t=eta_t, status='in_transit',
        received_at=None, reason=offer['reason'], source='emulation')
    s['orders'].insert(0, order)
    model.log('action', f"Учебный заказ {order['id']}: {p['name']} × {quantity}, {offer['supplier']}.")
    notify(model, 'stock', 'info', f"Учебный заказ {order['id']}: {p['name']} × {quantity}; "
           f"{order['total_kzt']:,} ₸, {offer['lead_workdays']} раб. дн. Внешняя заявка не отправляется.")
    stock_check(model)
    return order


def stock_check(model):
    transit = {}
    for order in model.s['orders']:
        if order['status'] == 'in_transit':
            transit[order['part']] = transit.get(order['part'], 0) + order['quantity']
    for key, p in model.parts.items():
        if p.get('service'):
            continue
        low = model.s['stock'][key] + transit.get(key, 0) < p['min']
        if low and not model.s['stock_low'].get(key):
            text = f"Учебный склад ниже минимума: {p['name']}, {model.s['stock'][key]} из {p['min']}."
            model.log('warn', text)
            notify(model, 'stock', 'warn', text)
        model.s['stock_low'][key] = low


def receive_due(model):
    for order in model.s['orders']:
        if order['status'] != 'in_transit' or order['eta_t'] > model.s['t'] + 1e-9:
            continue
        part = order['part']
        model.s['stock'][part] += order['quantity']
        order.update(status='received', received_at=model.clock())
        text = f"Учебная поставка {order['id']}: {model.parts[part]['name']} × {order['quantity']} принята на склад."
        model.log('info', text)
        notify(model, 'stock', 'info', text)
    # Keep all active orders and the latest 200 received ones.
    received = 0
    retained = []
    for order in model.s['orders']:
        received += order['status'] == 'received'
        if order['status'] == 'in_transit' or received <= 200:
            retained.append(order)
    model.s['orders'] = retained
    stock_check(model)


def summary(model, catalog=None):
    s = model.s
    catalog = catalog if catalog is not None else parts_view(model)
    elapsed = s['runH'] + s['downH']
    cars, rejected = int(s['carsF']), int(s['nokF'])
    availability = s['runH'] / elapsed if elapsed else 1
    performance = min(1, s['carsF'] / (s['runH'] * 60)) if s['runH'] else 1
    quality = (cars - rejected) / cars if cars else 1
    due = sum(remaining(model, c) is not None and remaining(model, c) <= 30 * 16 for c in model.cs)
    shortages = sum(p['deficit'] > 0 for p in catalog)
    state = 'время на паузе' if s['paused'] else 'работает' if s['line'] == 'run' else 'остановлена'
    text = (f"Учебная линия: {state}. Выпуск {cars} авт., брак {rejected}, "
            f"работа {s['runH']:.2f} ч, простой {s['downH']:.2f} ч. "
            f"Активных аварий: {len(s['alarms'])}; ТО в 30 рабочих дней: {due}; "
            f"позиций с дефицитом: {shortages}. OEE модели: {availability * performance * quality * 100:.1f}%.")
    return dict(availability_pct=availability * 100, quality_pct=quality * 100,
        performance_pct=performance * 100, oee_pct=availability * performance * quality * 100,
        maintenance_30d=due, shortages=shortages, text=text, takt_seconds=60,
        has_production=bool(cars), metrics_basis='virtual_cycles_and_runtime')


def stand_context(model, max_items=12):
    """Bounded, data-only context for the separately configured local LLM."""
    catalog = parts_view(model)
    components = sorted(model.cs, key=lambda c: remaining(model, c) if remaining(model, c) is not None else float('inf'))
    return dict(source='emulation', model_time=model.clock(), revision=model.s['revision'],
        summary=summary(model, catalog), physical_control=False,
        components=[dict(id=c['id'], name=c['name'], robot=c['robot'], wear_pct=c['wear'] * 100,
            level=c['level'], forecast=forecast(model, c), sensors=c['val']) for c in components[:max_items]],
        alarms=[dict(id=a['id'], component_id=a['component_id'], label=a['label'],
                     trips=model.s['trips'].get(a['id'], 1)) for a in model.s['alarms'][:max_items]],
        shortages=[p for p in catalog if p['deficit']][:max_items],
        pending_orders=[o for o in model.s['orders'] if o['status'] == 'in_transit'][:max_items],
        notice='Виртуальный стенд. Каталог и поставщики учебные. Внешние заказы и сообщения не отправляются.')


def find_robot(text):
    match = re.search(r'(?:^|[^a-zа-я])[rр]\s?([1-4])(?![0-9])|(?:станц\w*|ст\.)\s*№?\s*([1-4])', text)
    if match:
        return 'R' + (match[1] or match[2])
    for pattern, robot in ((r'конвейер|цеп[ьи]|натяж|лубрик|энкодер|ролик|настил', 'CV'),
                            (r'стекл|остекл|вакуум|присоск', 'R1'), (r'колес|гайковерт', 'R2'),
                            (r'сиден|кресл', 'R3'), (r'панел|модул|тяжел', 'R4')):
        if re.search(pattern, text):
            return robot
    return None


def find_part(model, text):
    for key in sorted(model.parts, key=len, reverse=True):
        if re.search(r'(?<![a-z0-9-])' + re.escape(key.lower()) + r'(?![a-z0-9-])', text):
            return key
    conveyor, upper = bool(re.search(r'конвейер|привод', text)), bool(re.search(r'j[456]', text))
    patterns = [(r'присоск', 'VAC-CUP'), (r'батаре', 'ENC-BAT'), (r'эжектор', 'VAC-EJ'),
        (r'кабел', 'CAB-LO' if re.search(r'j[123]|основан', text) else 'CAB-UP'), (r'вентилят', 'FAN'),
        (r'шпиндел|гайковерт', 'NUT-SPN'), (r'калибр', 'TQ-CAL'), (r'цилиндр', 'CYL-KIT'),
        (r'зажим', 'CLAMP-KIT'), (r'цеп[ьи]', 'CHAIN'), (r'смазк|лубрик', 'LUBE'),
        (r'масл', 'GBX-OIL'), (r'ролик', 'ROLL-KIT'), (r'звездоч', 'SPRK'), (r'пластин|настил', 'SLAT'),
        (r'юстир|позиц', 'POS-ADJ'), (r'энкодер', 'ENC-WHL'), (r'преобразоват|частотн', 'VFD'),
        (r'натяж', 'TAKE-KIT'), (r'тормоз', 'CV-BRK' if conveyor else 'BRK'),
        (r'двигател|мотор', 'CV-MOT' if conveyor else 'MOT-S' if upper else 'MOT-L'),
        (r'редуктор', 'CV-GBX' if conveyor else 'RED-S' if upper else 'RED-L')]
    return next((part for pattern, part in patterns if re.search(pattern, text)), None)


def answer(model, question):
    """Read-only deterministic assistant: action suggestions require a command."""
    text = question.casefold().replace('ё', 'е')
    robot, part = find_robot(text), find_part(model, text)
    selected = [c for c in model.cs if robot is None or c['robot'] == robot]
    ordered = sorted(selected, key=lambda c: remaining(model, c) if remaining(model, c) is not None else float('inf'))
    catalog = parts_view(model)
    actions, lines = [], []
    tag = lambda c: f"{c['robot']} · {c['name']}"

    def forecast_line(c):
        f = forecast(model, c)
        window = ('после ' + f['after'][:10]) if f['open_ended'] else f"{f['from'][:10]} — {f['to'][:10]}"
        return f"{tag(c)}: износ {c['wear'] * 100:.1f}%, окно ТО {window}; {f['basis'].lower()}."

    if re.search(r'закаж|заказать|оформи', text):
        if part is None and not re.search(r'вс[её]|недоста|дефицит', text):
            lines = ['Уточните деталь или напишите «закажи всё недостающее». Заказ не создан.']
        elif part is None or re.search(r'вс[её]|недоста|дефицит', text):
            deficient = [p for p in catalog if p['deficit']]
            lines = [f"Учебных позиций с дефицитом: {len(deficient)}. Ничего не заказано: подтвердите действие кнопкой."]
            if deficient:
                actions.append(dict(action='order_all', label='Учебные заказы по дефициту'))
        else:
            p = next(p for p in catalog if p['id'] == part)
            if p['service']:
                lines = [f"{p['name']} — услуга. Выполнение отмечается в карточке ТО; складской заказ не требуется."]
            elif not p['deficit']:
                lines = [f"{p['name']}: потребность уже покрыта складом и поставками."]
            else:
                offer = p['recommended_offer']
                lines = [f"Предложение: {p['name']} × {p['deficit']}, {offer['supplier']}, "
                         f"{offer['price_kzt'] * p['deficit']:,} ₸, {offer['lead_workdays']} раб. дн. "
                         f"{offer['reason']} Заказ не создан: подтвердите действие кнопкой."]
                actions.append(dict(action='order', target=part, label='Учебный заказ'))
    elif re.search(r'уведом|отправ|сводк|сообщи', text):
        lines = [summary(model, catalog)['text'], 'Сводку можно сохранить во внутреннем журнале стенда. Внешняя отправка не подключена.']
        actions.append(dict(action='summary', label='Сохранить сводку в журнале стенда'))
    elif re.search(r'авари|почему|останов|стоит|стоят', text):
        relevant = [a for a in model.s['alarms'] if robot is None or model.by[a['component_id']]['robot'] == robot]
        lines = [f"Активных аварий на выбранном участке: {len(relevant)}; на линии: {len(model.s['alarms'])}."]
        for alarm in relevant[:12]:
            c = model.by[alarm['component_id']]
            lines.append(f"{tag(c)}: {alarm['label']}; срабатываний {model.s['trips'].get(alarm['id'], 1)}. "
                         f"Причина: {model.causes.get(model.kind(c), 'требуется диагностика')}. "
                         f"Действие в учебной модели: {model.actions.get(model.kind(c), 'проверьте узел')}.")
        if relevant:
            lines.append('Устраните причину, квитируйте восстановленную аварию, затем запустите виртуальную линию.')
        elif model.s['line'] == 'stop':
            lines.append('Линия остановлена; после проверки датчиков доступен ручной пуск.')
    elif re.search(r'поставщ|купить|где взять|цен[аыу]|стоимост|предложен', text):
        choices = [p for p in catalog if p['id'] == part] if part else [p for p in catalog if p['deficit']][:8]
        lines = ['Каталог учебный: названия поставщиков и цены из исходного стенда, коммерческими предложениями не являются.']
        for p in choices:
            offers = '; '.join(f"{o['supplier']}: {o['price_kzt']:,} ₸, {o['lead_workdays']} раб. дн." for o in p['offers'])
            lines.append(f"{p['name']} ({p['id']}): {offers}. Рекомендация: {p['recommended_offer']['supplier']}. {p['recommended_offer']['reason']}")
        if not choices:
            lines.append('Дефицита нет. Назовите деталь или её артикул для сравнения предложений.')
    elif re.search(r'склад|запчаст|детал|дефицит|хватит|хватает|наличи', text):
        choices = [p for p in catalog if p['id'] == part] if part else [p for p in catalog if p['deficit'] or p['in_transit'] or p['needed_90d']]
        lines = ['Учебная потребность на 90 рабочих дней с учётом минимального запаса:']
        lines += [f"{p['name']}: склад {p['stock']}, минимум {p['minimum']}, нужно {p['needed_90d']}, "
                  f"в пути {p['in_transit']}, дефицит {p['deficit']}." for p in choices[:16]]
        if not choices:
            lines.append('Склад покрывает потребность, активных поставок нет.')
    elif re.search(r'замен|менять|износ|требует|прогноз|когда|сломает|выйдет из строя|отказ|срок|ресурс', text):
        attention = [c for c in ordered if c['level'] != 'ok' or remaining(model, c) is not None and remaining(model, c) <= 30 * 16]
        if re.search(r'прогноз|когда|сломает|выйдет из строя|отказ|срок|ресурс', text):
            attention = ordered
        lines = [f"Прогноз для {robot or 'всей линии'}: 2 смены по 8 ч, 5 рабочих дней в неделю, окно ±20%. Это расчёт износа виртуальной модели."]
        lines += [forecast_line(c) for c in (attention or ordered[:1])[:12]]
        part_ids = {c['part'] for c in attention}
        for p in catalog:
            if p['id'] not in part_ids:
                continue
            if p['service']:
                lines.append(f"Обслуживание: {p['name']} — услуга; отметьте выполнение в карточке ТО.")
            else:
                lines.append(f"{p['name']}: склад {p['stock']}, в пути {p['in_transit']}, "
                             f"потребность в 90 рабочих дней {p['needed_90d']}, дефицит с запасом {p['deficit']}.")
        early = [c for c in selected if model.kind(c) in ('reducer', 'cvgbx') and c['lv'].get('vib') in ('warn', 'alarm')]
        lines += [f"{tag(c)}: вибрация уже вне нормы, осмотр и проверка смазки нужны раньше расчётного срока." for c in early]
    elif re.search(r'нагруз|перегруз|момент|по осям', text):
        if robot == 'CV':
            rollers, chain = model.by['CV-rollers'], model.by['CV-chain']
            lines = [f"Нагрузка привода конвейера: {model.cv_load() * 100:.1f}% — база 60%, "
                     f"ролики +{25 * rollers['wear'] ** 3:.1f}%, цепь +{10 * chain['wear'] ** 2:.1f}%, "
                     f"заклинивание +{100 * rollers['fx'].get('jam', 0):.1f}%. "
                     f"Подача смазки: {model.lube_flow():.1f}% нормы."]
        else:
            lines = ['Нагрузка — доля номинального момента редуктора; плечо J2 и локоть J3 несут основной вес детали и оснастки.']
            for c in selected:
                if c['kind'] == 'reducer' and c['node'] in ('J2', 'J3'):
                    load = c['load'] * c['fx'].get('load', 1)
                    motor = model.by[c['id'].replace('-red', '-mot')]
                    full_hours = 6000 / (c['speed'] * load ** (10/3))
                    lines.append(f"{c['robot']} {c['node']}: нагрузка {load * 100:.1f}%, температура обмотки {motor['val'].get('temp', 0):.1f} °C; "
                                 f"полный ресурс редуктора по нагрузке {full_hours:.0f} ч ({full_hours / (16 * 260):.1f} лет по 260 рабочих дней); "
                                 f"множитель расхода ресурса от перегруза {c['fx'].get('load', 1) ** (10/3):.2f}.")
    elif re.search(r'выработ|сколько машин|собран|выпуск|oee|производ|доступност|брак', text):
        metrics = summary(model, catalog)
        lines = [metrics['text'], f"Доступность {metrics['availability_pct']:.1f}%, производительность {metrics['performance_pct']:.1f}%, "
                 f"качество {metrics['quality_pct']:.1f}%. Производительность рассчитывается из такта 60 с, выпуска и времени работы, без фиксированных 98%."]
    elif robot:
        lines = [f"Участок {robot}: узлов {len(selected)}, с отклонениями {sum(c['level'] != 'ok' for c in selected)}."]
        if robot == 'CV':
            chain, lube, motor = (model.by[k] for k in ('CV-chain', 'CV-lube', 'CV-cvmot'))
            lines.append(f"Состояние конвейера: {model.operational_status('CV')}; нагрузка привода {model.cv_load() * 100:.1f}%, "
                         f"ток двигателя {motor['val'].get('cur', 0):.1f} А, вытяжка цепи {chain['val'].get('el', 3 * chain['wear']):.2f}% "
                         f"при пределе 3%; запас смазки {lube['val'].get('lvl', 100 * max(0, 1-lube['wear'])):.1f}%, "
                         f"подача {model.lube_flow():.1f}% нормы.")
        for c in selected:
            if c['level'] != 'ok':
                deviations = [f"{d['label']} {c['val'].get(d['k'], 0):.{d['d']}f} {d['unit']}"
                              for d in model.defs(c) if c['lv'].get(d['k']) in ('warn', 'alarm')]
                lines.append(f"{tag(c)}: " + (', '.join(deviations) if deviations else f"износ {c['wear']*100:.1f}%"))
        lines += [forecast_line(c) for c in ordered[:3]]
        for fault in model.faults:
            if fault['id'] in model.s['faults'] and any(model.by[key]['robot'] == robot for key in fault['t']):
                lines.append(f"Активный сценарий: {fault['name']} — {fault['hint']}.")
    else:
        lines = ['Локальный помощник стенда работает по правилам и текущим данным эмуляции. Можно спросить: '
                 '«Что требует замены на R1?», «Прогноз отказов», «Нагрузка конвейера», «Почему стоит линия?», '
                 '«Чего не хватает на складе?», «Где купить присоски?», «Закажи всё недостающее», «Сводка». '
                 'Действия выполняются отдельными кнопками; этот ответ ничего не изменяет.']
    return dict(engine='rules', source='emulation', revision=model.s['revision'],
                model_time=model.clock(), text='\n\n'.join(lines), actions=actions)
