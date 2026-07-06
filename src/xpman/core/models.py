"""SQLAlchemy ORM models for the xpman core data layer.

Hierarchy (mirrors the legacy XP Man vocabulary):

    Profile -> Subject
    Profile -> Program -> Experiment -> Condition
                                      -> Block -> Trial (-> Condition)

    Program -> Instance (frozen snapshot of the Program's full tree)
    Instance -> Run -> Result -> Event
    Subject -> Run

No PsychoPy/Qt/hardware imports here deliberately -- this module must stay importable
and testable headless (see docs/architecture.md).

Cascade policy (documented once here; per-relationship comments note any deviation):
Deleting a "container" row (Profile, Program, Experiment, Block, Instance, Run, Result)
cascades to delete everything that only makes sense in its context (its children), using
``cascade="all, delete-orphan"`` on the parent-side relationship together with
``ondelete="CASCADE"`` on the child's FK so the same behavior holds even for rows deleted
via raw SQL. Purely "informational" cross-references (Instance.program_id,
Result.condition_id) use ``ondelete="SET NULL"`` (nullable FK) instead, since deleting the
referenced row should not destroy historical Instance/Result records that must remain
reproducible/inspectable.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp used as the default for all created_at/updated_at columns."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base shared by every xpman ORM model.

    ``migrations/env.py`` imports ``Base.metadata`` from this module for Alembic
    autogenerate support -- keep every model attached to this Base.
    """


class RunStatus(enum.StrEnum):
    """Terminal status of a Run."""

    COMPLETED = "completed"
    ABORTED = "aborted"
    CRASHED = "crashed"


class Profile(Base):
    """Top-level owner of Subjects and Programs (roughly: one experimenter/lab member)."""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    subjects: Mapped[list["Subject"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", passive_deletes=True
    )
    programs: Mapped[list["Program"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Profile(id={self.id!r}, name={self.name!r})"


class Subject(Base):
    """A participant belonging to a Profile."""

    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    first_name: Mapped[str] = mapped_column(String(200), nullable=False)
    last_name: Mapped[str] = mapped_column(String(200), nullable=False)
    info_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    visible_to_others: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    profile: Mapped[Profile] = relationship(back_populates="subjects")
    runs: Mapped[list["Run"]] = relationship(back_populates="subject", passive_deletes=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Subject(id={self.id!r}, first_name={self.first_name!r}, last_name={self.last_name!r})"


class Program(Base):
    """A named, reusable experiment definition tree owned by a Profile."""

    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_main_directory: Mapped[str] = mapped_column(Text, nullable=False)
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    task_schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    visible_to_others: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    profile: Mapped[Profile] = relationship(back_populates="programs")
    experiments: Mapped[list["Experiment"]] = relationship(
        back_populates="program", cascade="all, delete-orphan", passive_deletes=True
    )
    # Informational only (per spec) -- an Instance survives its Program being deleted.
    instances: Mapped[list["Instance"]] = relationship(back_populates="program", passive_deletes=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Program(id={self.id!r}, name={self.name!r}, task_name={self.task_name!r})"


class Experiment(Base):
    """One experiment within a Program, holding Conditions and Blocks."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("programs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    program: Mapped[Program] = relationship(back_populates="experiments")
    conditions: Mapped[list["Condition"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True
    )
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Experiment(id={self.id!r}, name={self.name!r})"


class Condition(Base):
    """A named parameter set within an Experiment; referenced by Trials."""

    __tablename__ = "conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parameters_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    experiment: Mapped[Experiment] = relationship(back_populates="conditions")
    trials: Mapped[list["Trial"]] = relationship(back_populates="condition", passive_deletes=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Condition(id={self.id!r}, name={self.name!r})"


class Block(Base):
    """An ordered group of Trials within an Experiment."""

    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    randomize_trials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    randomize_per_subject: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    experiment: Mapped[Experiment] = relationship(back_populates="blocks")
    trials: Mapped[list["Trial"]] = relationship(
        back_populates="block", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Trial.order_index",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Block(id={self.id!r}, name={self.name!r}, order_index={self.order_index!r})"


class Trial(Base):
    """A single ordered slot within a Block, pointing at the Condition it should run."""

    __tablename__ = "trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False)
    # SET NULL: a Condition can in principle be retired without destroying the Block's Trial
    # slots; the Trial row itself is owned by the Block, not the Condition.
    condition_id: Mapped[int | None] = mapped_column(
        ForeignKey("conditions.id", ondelete="SET NULL"), nullable=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    block: Mapped[Block] = relationship(back_populates="trials")
    condition: Mapped[Condition | None] = relationship(back_populates="trials")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Trial(id={self.id!r}, block_id={self.block_id!r}, order_index={self.order_index!r})"


class Instance(Base):
    """An immutable, fully-resolved snapshot of a Program's tree at a point in time.

    ``frozen_json`` is write-once: nothing in ``core`` exposes an update path for it after
    creation (see ``core/instance.py``). ``checksum`` lets callers verify on load that the
    stored payload hasn't been tampered with/corrupted.
    """

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Informational only: deleting the Program must not delete/invalidate past Instances.
    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    frozen_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)

    program: Mapped[Program | None] = relationship(back_populates="instances")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Instance(id={self.id!r}, name={self.name!r}, checksum={self.checksum!r})"


class Run(Base):
    """One launch of an Instance against a Subject."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL: a Subject may be removed/anonymized without deleting historical Run records.
    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    xpman_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), nullable=False, default=RunStatus.ABORTED)
    # Provenance captured for reproducibility (all nullable + additive, so old Runs stay valid).
    # Versions are recorded at Run creation; the refresh fields are filled in once the task has
    # measured the monitor (see runtime.engine + a task's run_metadata()). measured_refresh_hz is
    # the *achieved* rate the frame math actually used -- not a requested/nominal value.
    psychopy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    numpy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    pyserial_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    measured_refresh_hz: Mapped[float | None] = mapped_column(Float, nullable=True)
    refresh_measured_successfully: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Which trigger backend drove this run and (where meaningful) the port/address it used --
    # captured from TriggerSender.describe() at Run creation. Nullable + additive: old Runs (and
    # any run whose backend records no port) leave these NULL. See runtime/session.launch_run.
    trigger_backend: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_port: Mapped[str | None] = mapped_column(Text, nullable=True)

    instance: Mapped[Instance] = relationship(back_populates="runs")
    subject: Mapped[Subject | None] = relationship(back_populates="runs")
    results: Mapped[list["Result"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Result.trial_index",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Run(id={self.id!r}, instance_id={self.instance_id!r}, status={self.status!r})"


class Result(Base):
    """One executed Trial's outcome summary within a Run; raw events live in Parquet on disk."""

    __tablename__ = "results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    trial_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Informational only: retiring a Condition must not delete historical Results.
    condition_id: Mapped[int | None] = mapped_column(
        ForeignKey("conditions.id", ondelete="SET NULL"), nullable=True
    )
    outcome_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    events_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="results")
    events: Mapped[list["Event"]] = relationship(
        back_populates="result", cascade="all, delete-orphan", passive_deletes=True,
        order_by="Event.timestamp",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Result(id={self.id!r}, run_id={self.run_id!r}, trial_index={self.trial_index!r})"


class Event(Base):
    """A lightweight DB mirror of a high-value event (block start, abort, ...).

    The full per-flip event stream lives in the Run's Parquet file, not here -- see
    ``core/export.py`` and ``docs/architecture.md``.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(
        ForeignKey("results.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    result: Mapped[Result] = relationship(back_populates="events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Event(id={self.id!r}, result_id={self.result_id!r}, event_type={self.event_type!r})"
