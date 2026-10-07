"""Daily case reports and vehicle quality records, kept separate from live telemetry."""
import csv
import hashlib
import io
import math
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select, func

from .database import CaseReport, QualityRecord
from .engine import utcnow
from .schemas import StrictModel
from .telemetry import ImportValidationError

QUALITY_FIELDS = ['timestamp', 'record_id', 'brand', 'model', 'color', 'quantity', 'rejected']
SECTIONS = {'Сварка': 'welding', 'Окраска': 'painting', 'Покраска': 'painting', 'Сборка': 'assembly'}


class VehicleQuality(StrictModel):
    timestamp: str = Field(max_length=64)
    record_id: str = Field(min_length=1, max_length=128)
    brand: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    color: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=1000000)
    rejected: int = Field(ge=0, le=1000000)

    @field_validator('record_id', 'brand', 'model', 'color')
    @classmethod
    def clean(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Нужен непустой текст без управляющих символов')
        return value

    @field_validator('timestamp')
    @classmethod
    def timestamp_with_zone(cls, value):
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Нужен часовой пояс')
        return stamp.astimezone(timezone.utc).isoformat()

    @model_validator(mode='after')
    def valid_counts(self):
        if self.rejected > self.quantity:
            raise ValueError('Брак не может превышать проверенное количество')
        return self


class QualityBatch(StrictModel):
    records: list[VehicleQuality] = Field(min_length=1, max_length=1000)


def quality_csv(content):
    if len(content) > 5*1024*1024:
        raise ImportValidationError([{'row': 0, 'message': 'Файл превышает 5 МБ'}])
    result, errors = [], []
    try:
        reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')), strict=True)
        if not reader.fieldnames or set(reader.fieldnames) != set(QUALITY_FIELDS) or len(reader.fieldnames) != len(QUALITY_FIELDS):
            raise ValueError('Заголовок качества: ' + ','.join(QUALITY_FIELDS))
        for row in reader:
            if len(result)+len(errors) >= 20000:
                raise ValueError('Не более 20 000 строк')
            try:
                row['quantity'], row['rejected'] = int(row['quantity']), int(row['rejected'])
                result.append(VehicleQuality.model_validate(row).model_dump())
            except (ValueError, TypeError) as exc:
                errors.append({'row': reader.line_num, 'message': str(exc)[:500]})
        if not result and not errors:
            raise ValueError('Нет записей качества')
    except (ValueError, UnicodeError, csv.Error) as exc:
        errors.append({'row': 1, 'message': str(exc)[:500]})
    if errors:
        raise ImportValidationError(errors[:100])
    return result


def parse_case(content):
    """Read only bounded OOXML tables. Never execute embedded objects or document instructions."""
    try:
        if len(content) > 5*1024*1024:
            raise ValueError('DOCX превышает 5 МБ')
        with ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > 500 or sum(i.file_size for i in archive.infolist()) > 20*1024*1024:
                raise ValueError('Слишком большой распакованный документ')
            xml = archive.read('word/document.xml')
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise ValueError('DTD и XML-сущности не разрешены')
        root = ET.fromstring(xml)
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        report = {'lines': [], 'downtimes': [], 'plans': [], 'quality': [], 'targets': {}, 'warnings': []}
        def number(value, integer=False):
            n = float(value.replace(',', '.'))
            if not math.isfinite(n) or n < 0 or n > 1e9 or integer and n != int(n):
                raise ValueError('Недопустимое число в таблице: ' + value)
            return int(n) if integer else n
        def date(value):
            return datetime.strptime(value, '%d.%m.%Y').date().isoformat()
        def section(value):
            return next((code for prefix, code in SECTIONS.items() if value.startswith(prefix)), 'unknown')
        seen_tables = set()
        for table in root.findall('.//w:tbl', ns):
            rows = [[' '.join(t.text or '' for t in c.findall('.//w:t', ns)).strip() for c in r.findall('w:tc', ns)] for r in table.findall('w:tr', ns)]
            if not rows: continue
            header, body = rows[0], rows[1:]
            if header == ['Дата', 'Линия', 'План', 'Факт', 'Время работы, ч', 'Загрузка, %']: kind = 'lines'
            elif header == ['Дата', 'Участок', 'Оборудование', 'Причина', 'Длительность, мин']: kind = 'downtimes'
            elif header == ['Модель', 'План на месяц']: kind = 'plans'
            elif header == ['Дата', 'Участок', 'Выпущено', 'Брак', '% брака']: kind = 'quality'
            else: raise ValueError('Неизвестная таблица: ' + ', '.join(header))
            if kind in seen_tables: raise ValueError('Таблица повторяется: ' + kind)
            seen_tables.add(kind)
            if not body or len(body) > 20000: raise ValueError('Пустая или слишком большая таблица')
            for values in body:
                if len(values) != len(header) or any(len(v) > 500 for v in values): raise ValueError('Некорректная строка таблицы')
                if kind == 'lines':
                    d, line, plan, actual, hours, utilization = values
                    item = {'date': date(d), 'line': line, 'operation': section(line), 'plan': number(plan, True), 'actual': number(actual, True), 'hours': number(hours), 'utilization': number(utilization)}
                    if item['hours'] > 24 or item['utilization'] > 100: raise ValueError('Недопустимое время/загрузка')
                elif kind == 'downtimes':
                    d, site, equipment, reason, minutes = values
                    item = {'date': date(d), 'section': site, 'operation': section(site), 'equipment': equipment, 'reason': reason, 'minutes': number(minutes)}
                    if item['minutes'] > 1440: raise ValueError('Простой больше суток')
                elif kind == 'plans':
                    model, plan = values
                    item = {'vehicle': model, 'quantity': number(plan, True)}
                else:
                    d, site, quantity, rejected, percent = values
                    q, r, p = number(quantity, True), number(rejected, True), number(percent)
                    if r > q or p > 100: raise ValueError('Недопустимый брак')
                    item = {'date': date(d), 'section': site, 'operation': section(site), 'quantity': q, 'rejected': r, 'reported_percent': p, 'calculated_percent': r/q*100 if q else None}
                    if q and abs(p-r/q*100) > .11: report['warnings'].append(f'{site}, {date(d)}: процент брака не совпадает с количеством')
                report[kind].append(item)
        if seen_tables != {'lines', 'downtimes', 'plans', 'quality'}:
            raise ValueError('Нужны четыре таблицы: линии, простои, план моделей и качество')
        paragraphs = [''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('.//w:p', ns)]
        text = '\n'.join(paragraphs)
        patterns = {'oee_min_percent': r'OEE\s*[-—–]\s*не менее\s*([\d,. ]+)%', 'defect_max_percent': r'уровень брака\s*[-—–]\s*не более\s*([\d,. ]+)%', 'downtime_max_minutes': r'простой критического оборудования\s*[-—–]\s*([\d,. ]+)\s*минут', 'monthly_min_quantity': r'План выпуска\s*[-—–]\s*не менее\s*([\d ]+)\s*автомобилей'}
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.I)
            if match: report['targets'][key] = number(match[1].replace(' ', ''), key.endswith('quantity'))
        shifts = re.search(r'(\d+)\s*смены? по\s*(\d+)\s*час', text)
        if shifts: report['targets'].update(shifts_per_day=int(shifts[1]), hours_per_shift=int(shifts[2]))
        report['planned_total'] = sum(p['quantity'] for p in report['plans'])
        target = report['targets'].get('monthly_min_quantity')
        report['plan_gap'] = max(0, target-report['planned_total']) if target is not None else None
        report['warnings'].append('В документе нет марок/моделей/цветов проверенных автомобилей и текущих датчиков роботов. Суточная загрузка не равна OEE.')
        if report['plan_gap']: report['warnings'].append(f"Сумма планов по моделям {report['planned_total']}; до общего ориентира не хватает {report['plan_gap']} автомобилей.")
        report['count'] = sum(len(report[k]) for k in ('lines', 'downtimes', 'plans', 'quality'))
        return report
    except (ValueError, BadZipFile, KeyError, ET.ParseError, OverflowError, RuntimeError, NotImplementedError) as exc:
        raise ImportValidationError([{'row': 0, 'message': 'DOCX: ' + str(exc)[:400]}])


class Manufacturing:
    def import_case(self, content, filename):
        data = parse_case(content)
        key = hashlib.sha256(content).hexdigest()
        with self._mutation() as db:
            if db.get(CaseReport, key):
                return {'imported': 0, 'kind': 'case', 'source': self.source, 'message': 'Этот документ уже сохранён; повторных записей нет.'}
            data.update(id=key, filename=filename[:200], imported_at=utcnow())
            db.add(CaseReport(id=key, imported_at=data['imported_at'], data=data))
            self._log_import('case', [{'timestamp': r['date']+'T00:00:00+05:00'} for r in data['lines']], filename)
            self.import_log[0]['count'] = data['count']
        return {'imported': data['count'], 'kind': 'case', 'source': self.source, 'message': 'Сохранены суточные отчёты. Откройте «Контроль качества» и «Автоматизация». Текущие показания роботов не изменены.'}

    def latest_case(self):
        with self.sessions() as db:
            return db.scalar(select(CaseReport.data).order_by(CaseReport.imported_at.desc()).limit(1))

    def quality_records(self, rows, filename='API: контроль качества'):
        seen = set()
        for r in rows:
            if r['record_id'] in seen: raise HTTPException(422, 'Повтор record_id внутри пакета')
            seen.add(r['record_id'])
        inserted = 0
        with self._mutation() as db:
            for r in rows:
                previous = db.get(QualityRecord, r['record_id'])
                if previous:
                    if any(getattr(previous, k) != v for k, v in r.items()):
                        raise HTTPException(409, 'record_id уже использован для другой записи: ' + r['record_id'])
                    continue
                db.add(QualityRecord(**r)); inserted += 1
            if inserted: self._log_import('quality', sorted(rows, key=lambda r: r['timestamp']), filename)
        return {'imported': inserted, 'kind': 'quality', 'source': self.source, 'message': f'Сохранено {inserted} записей контроля качества. Повторы не учитываются дважды.'}

    def manufacturing(self, search='', offset=0):
        def read():
            with self.sessions() as db:
                query = select(QualityRecord)
                if search:
                    query = query.where(QualityRecord.brand.contains(search, autoescape=True) | QualityRecord.model.contains(search, autoescape=True) | QualityRecord.color.contains(search, autoescape=True))
                subset = query.subquery()
                total, quantity, rejected = db.execute(select(func.count(), func.coalesce(func.sum(subset.c.quantity), 0), func.coalesce(func.sum(subset.c.rejected), 0))).one()
                rows = [{k: getattr(r, k) for k in QUALITY_FIELDS} for r in db.scalars(query.order_by(QualityRecord.timestamp.desc(), QualityRecord.record_id).offset(offset).limit(100))]
                groups = [dict(r._mapping) for r in db.execute(select(subset.c.brand, subset.c.model, subset.c.color, func.sum(subset.c.quantity).label('quantity'), func.sum(subset.c.rejected).label('rejected')).group_by(subset.c.brand, subset.c.model, subset.c.color).order_by(subset.c.brand, subset.c.model, subset.c.color).limit(100))]
            return {'report': self.latest_case(), 'quality': {'total': total, 'quantity': quantity, 'rejected': rejected, 'accepted': quantity-rejected, 'rows': rows, 'groups': groups, 'offset': offset}, 'updated_at': self.updated_at}
        return self.cached(('manufacturing', search, offset), read, 2)

    def quality_export(self):
        def line(values):
            output = io.StringIO(newline=''); writer = csv.writer(output)
            writer.writerow(["'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in values])
            return output.getvalue()
        yield '\ufeff'+line(QUALITY_FIELDS)
        with self.sessions() as db:
            for r in db.scalars(select(QualityRecord).order_by(QualityRecord.timestamp, QualityRecord.record_id)).yield_per(1000):
                yield line([getattr(r, k) for k in QUALITY_FIELDS])
