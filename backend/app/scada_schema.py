"""Validated SCADA contracts. No fictional factory specifications are seeded."""
from datetime import datetime, timezone, date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from .schemas import StrictModel

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9А-Яа-яЁё_.:-]+$')]
Title = Annotated[str, Field(min_length=1, max_length=160)]
Stamp = Annotated[datetime, Field(strict=False)]
Day = Annotated[date, Field(strict=False)]
Operation = Literal['welding', 'painting', 'assembly', 'unknown']


class AssetBody(StrictModel):
    id: Identifier
    name: Title
    kind: Literal['robot', 'conveyor', 'equipment']
    section: str = Field(default='', max_length=160)
    robot_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation: Operation | None = None


class AssetOperationUpdate(StrictModel):
    operation: Operation | None
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=16, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class Threshold(StrictModel):
    metric: Identifier
    label: Title
    unit: str = Field(default='', max_length=24)
    direction: Literal['high', 'low'] = 'high'
    warning: float = Field(ge=-1e9, le=1e9)
    alarm: float = Field(ge=-1e9, le=1e9)
    hysteresis: float = Field(default=0, ge=0, le=1e9)

    @model_validator(mode='after')
    def ordered(self):
        if (self.direction == 'high' and self.warning >= self.alarm) or (self.direction == 'low' and self.warning <= self.alarm):
            raise ValueError('Пороги внимания и аварии должны следовать направлению контроля')
        if self.hysteresis >= abs(self.alarm-self.warning):
            raise ValueError('Гистерезис должен быть меньше расстояния между порогами')
        return self


class ComponentBody(StrictModel):
    id: Identifier
    asset_id: Identifier
    name: Title
    node: str = Field(default='', max_length=80)
    kind: Literal['motor', 'reducer', 'brake', 'cable', 'battery', 'fan', 'tool', 'chain', 'lubricator', 'sensor', 'other'] = 'other'
    part_id: Identifier | None = None
    part_quantity: int = Field(default=1, ge=1, le=100000)
    life_model: Literal['none', 'hours', 'cycles', 'distance', 'thermal', 'reducer', 'chain', 'calendar'] = 'none'
    rated_life: float | None = Field(default=None, gt=0, le=1e12)
    initial_wear_pct: float | None = Field(default=None, ge=0, le=100)
    reference_temperature_c: float = Field(default=80, ge=-50, le=250)
    commissioned_at: Stamp | None = None
    specification: str = Field(default='', max_length=500)
    expected_interval_seconds: int = Field(default=60, ge=1, le=86400)
    thresholds: list[Threshold] = Field(default_factory=list, max_length=30)
    robot_metric_map: dict[Identifier, Identifier] = Field(default_factory=dict, max_length=30)

    @model_validator(mode='after')
    def complete(self):
        if self.life_model != 'none' and (self.rated_life is None or not self.specification.strip()):
            raise ValueError('Для оценки ресурса нужны номинальный ресурс и основание из паспорта/регламента')
        if self.life_model == 'calendar' and self.commissioned_at is None:
            raise ValueError('Для календарного ресурса укажите дату установки')
        if self.commissioned_at and (self.commissioned_at.tzinfo is None or self.commissioned_at > datetime.now(timezone.utc)):
            raise ValueError('Дата установки должна содержать часовой пояс и не быть в будущем')
        keys = [t.metric for t in self.thresholds]
        if len(keys) != len(set(keys)):
            raise ValueError('Для одного показателя разрешён один набор порогов')
        if len(self.robot_metric_map.values()) != len(set(self.robot_metric_map.values())):
            raise ValueError('Нельзя направлять два поля робота в один показатель узла')
        return self


class Reading(StrictModel):
    record_id: Identifier
    component_id: Identifier
    timestamp: Stamp
    metrics: dict[Identifier, Annotated[float, Field(ge=-1e9, le=1e9)]] = Field(default_factory=dict, max_length=40)
    runtime_hours: float | None = Field(default=None, ge=0, le=1e9)
    total_cycles: int | None = Field(default=None, ge=0, le=10**15)
    distance_km: float | None = Field(default=None, ge=0, le=1e12)

    @field_validator('timestamp')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError('timestamp должен содержать часовой пояс')
        if value.timestamp() > datetime.now(timezone.utc).timestamp()+5:
            raise ValueError('Показания из будущего не принимаются: проверьте часы источника')
        return value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def has_values(self):
        if not self.metrics and all(getattr(self,k) is None for k in ('runtime_hours','total_cycles','distance_km')):
            raise ValueError('Нужно хотя бы одно показание или счётчик')
        for key, low, high in [('temperature_c',-100,350),('load_ratio',0,10),('speed_ratio',0,10),('lubrication_pct',0,100)]:
            if key in self.metrics and not low <= self.metrics[key] <= high:
                raise ValueError(f'{key}: допустимый диапазон {low}…{high}')
        return self


class ComponentUpdate(StrictModel):
    config: ComponentBody
    expected_revision: int = Field(ge=1)


class ReadingBatch(StrictModel):
    readings: list[Reading] = Field(min_length=1, max_length=1000)


class PartBody(StrictModel):
    id: Identifier
    name: Title
    unit: str = Field(default='шт.', min_length=1, max_length=24)
    minimum: int = Field(default=0, ge=0, le=1000000)


class StockBody(StrictModel):
    record_id: Identifier
    part_id: Identifier
    quantity: int = Field(ge=-1000000, le=1000000)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator('quantity')
    @classmethod
    def nonzero(cls, value):
        if not value: raise ValueError('Количество не должно быть нулевым')
        return value


class OfferBody(StrictModel):
    id: Identifier
    part_id: Identifier
    supplier: Title
    price_minor: int = Field(ge=0, le=10**12)
    currency: Literal['KZT','RUB','USD','EUR','CNY'] = 'KZT'
    lead_workdays: int = Field(ge=0, le=730)
    valid_until: Day
    reference: str = Field(min_length=3, max_length=500)


class OrderBody(StrictModel):
    id: Identifier
    offer_id: Identifier
    quantity: int = Field(ge=1, le=1000000)


class OrderConfirm(StrictModel):
    reference: str = Field(min_length=3, max_length=300)


class ReceiveBody(StrictModel):
    record_id: Identifier
    quantity: int = Field(ge=1, le=1000000)
    reference: str = Field(min_length=3, max_length=300)


class TaskBody(StrictModel):
    id: Identifier
    component_id: Identifier
    due_date: Day
    note: str = Field(min_length=5, max_length=500)


class TaskComplete(StrictModel):
    evidence: str = Field(min_length=5, max_length=500)
    reset_counters: bool = False


class CalendarBody(StrictModel):
    hours_per_day: int = Field(default=16, ge=1, le=24)
    weekdays: list[Annotated[int, Field(ge=0,le=6)]] = Field(default_factory=lambda:[0,1,2,3,4], min_length=1, max_length=7)
    holidays: list[Day] = Field(default_factory=list, max_length=1000)

    @field_validator('weekdays')
    @classmethod
    def unique(cls, value):
        return sorted(set(value))
