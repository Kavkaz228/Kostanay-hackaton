from __future__ import annotations

from sqlalchemy import JSON, Integer, String, Float, Boolean, Index, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ScadaRecord(Base):
    """Durable equipment, maintenance and stock journal; separate from simulation."""
    __tablename__ = 'scada_records'
    kind: Mapped[str] = mapped_column(String(24), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), index=True, default='')
    created_at: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index('ix_scada_kind_owner_time', 'kind', 'owner', 'created_at'),)


class Snapshot(Base):
    __tablename__ = "snapshots"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class History(Base):
    __tablename__ = "history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class Observation(Base):
    __tablename__ = "robot_observations"
    robot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index('ix_observations_timestamp', 'timestamp'),)


class Receipt(Base):
    __tablename__ = 'ingestion_receipts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, index=True)


class CaseReport(Base):
    __tablename__ = 'case_reports'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    imported_at: Mapped[str] = mapped_column(String(40), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class QualityRecord(Base):
    __tablename__ = 'quality_records'
    record_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(40), index=True)
    brand: Mapped[str] = mapped_column(String(80), index=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    color: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer)


class RobotCommand(Base):
    __tablename__ = 'robot_commands'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    data: Mapped[dict] = mapped_column(JSON)


class GatewayKey(Base):
    __tablename__ = 'gateway_keys'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(128), index=True)
    expires: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(Base):
    __tablename__ = 'login_sessions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float] = mapped_column(Float, index=True)


class AccessToken(Base):
    __tablename__ = 'access_tokens'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    expires: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Audit(Base):
    __tablename__ = 'audit_log'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Float, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(256))
    detail: Mapped[str] = mapped_column(String(500))


def make_database(url: str):
    options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True, "pool_size": 20, "max_overflow": 10, "pool_timeout": 5, "connect_args": {"connect_timeout": 5, "options": "-c statement_timeout=30000"}}
    engine = create_engine(url, **options)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
