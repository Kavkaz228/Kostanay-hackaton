from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Control(StrictModel):
    running: bool | None = None
    speed: Literal[1, 10, 30, 60, 120] | None = None
    shift_plan: int | None = Field(default=None, ge=1, le=1000000, strict=True)

    @field_validator("speed", mode="before")
    @classmethod
    def speed_is_number(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("Скорость должна быть целым числом")
        return value


class Advance(StrictModel):
    seconds: int = Field(ge=1, le=3600, strict=True)


class StationChange(StrictModel):
    cycle_seconds: float | None = Field(default=None, ge=5, le=600)
    capacity: int | None = Field(default=None, ge=1, le=10, strict=True)
    buffer_capacity: int | None = Field(default=None, ge=1, le=200, strict=True)
    defect_rate: float | None = Field(default=None, ge=0, le=0.5)
    speed_factor: float | None = Field(default=None, ge=0.1, le=2)
    manual_stop: bool | None = None


class ScenarioChange(StationChange):
    station_id: str


class ScenarioRequest(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    horizon_minutes: int = Field(ge=15, le=480, strict=True)
    changes: list[ScenarioChange] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_changes(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Укажите название сценария")
        ids = [change.station_id for change in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("Участок может встречаться в сценарии только один раз")
        if any(len(change.model_dump(exclude_none=True)) <= 1 for change in self.changes):
            raise ValueError("В каждом изменении нужен хотя бы один параметр")
        return self


class Reset(StrictModel):
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Нужно явное подтверждение confirm: true")
        return value


class Source(StrictModel):
    source: Literal["simulation", "telemetry"]
    telemetry_kind: Literal['production', 'robots'] | None = None


SensorName = Literal['joint_temperature_c', 'vibration_mm_s', 'hydraulic_pressure_bar', 'pneumatic_pressure_bar', 'pneumatic_pressure', 'paint_volume_l', 'paint_capacity_l', 'electrode_count', 'welding_current_a', 'motor_current_a', 'speed_percent', 'cycle_progress_pct']


class SensorLimits(StrictModel):
    low_critical: float | None = Field(default=None, ge=-273.15, le=1e9)
    low_warning: float | None = Field(default=None, ge=-273.15, le=1e9)
    high_warning: float | None = Field(default=None, ge=-273.15, le=1e9)
    high_critical: float | None = Field(default=None, ge=-273.15, le=1e9)

    @model_validator(mode='after')
    def ordered(self):
        values = [v for v in (self.low_critical, self.low_warning, self.high_warning, self.high_critical) if v is not None]
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError('Границы должны возрастать: критическая нижняя < нижняя < верхняя < критическая верхняя')
        return self


class ErrorDefinition(StrictModel):
    description: str = Field(min_length=1, max_length=500)
    action: str = Field(default='', max_length=500)
    severity: Literal['warning', 'critical'] = 'warning'


class RobotConfig(StrictModel):
    expected_interval_seconds: int = Field(default=60, ge=1, le=86400)
    limits: dict[SensorName, SensorLimits] = Field(default_factory=dict)
    error_codes: dict[str, ErrorDefinition] = Field(default_factory=dict, max_length=100)

    @model_validator(mode='after')
    def valid_codes(self):
        for code, value in self.error_codes.items():
            if not code.strip() or len(code) > 128 or code.casefold() in ('0', 'none', 'null', 'ok'):
                raise ValueError('Укажите непустой код ошибки, отличный от кода нормального состояния')
            if not value.description.strip():
                raise ValueError('Описание ошибки не может быть пустым')
        for sensor, limits in self.limits.items():
            if sensor != 'joint_temperature_c' and any(v < 0 for v in limits.model_dump(exclude_none=True).values()):
                raise ValueError('Границы давления и вибрации не могут быть отрицательными')
        return self


class RobotMeasurement(StrictModel):
    timestamp: str = Field(min_length=1, max_length=64)
    robot_id: str = Field(min_length=1, max_length=128)
    cycle_status: str = Field(min_length=1, max_length=128)
    line_section: str = Field(default='', max_length=128)
    error_code: str = Field(default='', max_length=128)
    joint_temperature_c: float | None = None
    vibration_mm_s: float | None = None
    hydraulic_pressure_bar: float | None = None
    pneumatic_pressure_bar: float | None = None
    pneumatic_pressure: float | None = None
    operation: Literal['', 'welding', 'painting', 'assembly'] = ''
    controller_mode: Literal['', 'automatic', 'manual', 'offline', 'unknown'] = ''
    safety_state: Literal['', 'normal', 'protective_stop', 'emergency_stop', 'unknown'] = ''
    paint_volume_l: float | None = Field(default=None, ge=0, le=1e9)
    paint_capacity_l: float | None = Field(default=None, ge=0, le=1e9)
    electrode_count: int | None = Field(default=None, ge=0, le=1000000000)
    welding_current_a: float | None = Field(default=None, ge=0, le=1e9)
    motor_current_a: float | None = Field(default=None, ge=0, le=1e9)
    speed_percent: float | None = Field(default=None, ge=0, le=100)
    cycle_progress_pct: float | None = Field(default=None, ge=0, le=100)
    joint_1_deg: float | None = Field(default=None, ge=-3600, le=3600)
    joint_2_deg: float | None = Field(default=None, ge=-3600, le=3600)
    joint_3_deg: float | None = Field(default=None, ge=-3600, le=3600)
    joint_4_deg: float | None = Field(default=None, ge=-3600, le=3600)
    joint_5_deg: float | None = Field(default=None, ge=-3600, le=3600)
    joint_6_deg: float | None = Field(default=None, ge=-3600, le=3600)


class MeasurementBatch(StrictModel):
    measurements: list[RobotMeasurement] = Field(min_length=1, max_length=1000)
