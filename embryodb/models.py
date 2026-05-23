"""SQLAlchemy ORM models for embryoDB v1 (safe mirror).

Schema mirrors the 16-field Java EmbryoXML record one-for-one, plus provenance
columns (xml_source_path, xml_hash, xml_mtime, raw_xml, imported_at, imported_by)
and an optimistic-locking trio (updated_at, updated_by, version).

The status enum keeps every value the Java app could emit (`new`, `archived`,
`deleted`, `arc1`, `arc2`, `arc3`, `del1`) so importing any legacy XML
round-trips. v1 only exposes the first three in the UI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    declared_attr,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ARCHIVED = "archived"
    DELETED = "deleted"
    # Legacy multi-stage values preserved for round-trip fidelity.
    ARC1 = "arc1"
    ARC2 = "arc2"
    ARC3 = "arc3"
    DEL1 = "del1"


class ImageLayout(StrEnum):
    """How a series' image data is laid out on disk."""

    # Today: per-plane TIFs under tif/ + tifR/ (+ optional DIC/ + tifC<n>/)
    SPLIT_TIF_PER_PLANE = "split_tif_per_plane"
    # Future: one multichannel TIF per (timepoint, plane)
    MULTICHANNEL_TIF_PER_PLANE = "multichannel_tif_per_plane"
    # Further future: one OME-TIFF per series; Bio-Formats native LIF/CZI
    OME_TIF = "ome_tif"
    NATIVE_LIF = "native_lif"
    NATIVE_CZI = "native_czi"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


dataset_series_table = Table(
    "dataset_series",
    Base.metadata,
    Column("dataset_id", ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True),
    Column("series_id", ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
)


class TimestampMixin:
    """Optimistic-locking + audit trio. `version` increments on every write."""

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )

    @declared_attr
    def updated_by(cls) -> Mapped[str]:
        return mapped_column(String(64), default="anonymous", nullable=False)

    @declared_attr
    def version(cls) -> Mapped[int]:
        return mapped_column(Integer, default=1, nullable=False)


class Series(Base, TimestampMixin):
    """One row per embryo imaging experiment.

    The 16 XML fields are split into typed columns where useful; integer-valued
    fields (timepts/edited_timepts/edited_cells) stay as TEXT here because the
    legacy data uses 'n/a' as a sentinel in many rows. The exporter writes them
    back verbatim.
    """

    __tablename__ = "series"
    __mapper_args__ = {"version_id_col": None}  # we manage version manually

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- 16 XML fields (raw text; preserved exactly as imported) -----------
    # All free-text fields are Text rather than VARCHAR(N) because the legacy
    # XML format imposes no length limit and real-world corpus data has rows
    # that exceed even generous caps (e.g. an old row with the partial-editing
    # code accidentally pasted into <editedby>, ~165 chars). SQLite silently
    # tolerates over-long values; PostgreSQL strictly enforces VARCHAR limits
    # and would reject the row at import time.
    series_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    date_acquired: Mapped[str] = mapped_column(Text, default="")
    person: Mapped[str] = mapped_column(Text, default="")
    strain_name: Mapped[str] = mapped_column(Text, default="")
    treatments: Mapped[str] = mapped_column(Text, default="")
    reporter_gene: Mapped[str] = mapped_column(Text, default="")  # redsig
    image_loc: Mapped[str] = mapped_column(Text, default="")
    timepts: Mapped[str] = mapped_column(Text, default="")
    annot_loc: Mapped[str] = mapped_column(Text, default="")
    acetree_config: Mapped[str] = mapped_column(Text, default="")
    edited_by: Mapped[str] = mapped_column(Text, default="")
    edited_timepts: Mapped[str] = mapped_column(Text, default="")
    edited_cells: Mapped[str] = mapped_column(Text, default="")
    partial_editing_code: Mapped[str] = mapped_column(Text, default="")  # checkedby
    comments: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[Status] = mapped_column(
        SAEnum(Status, name="status_enum", native_enum=False, length=16),
        default=Status.NEW,
        nullable=False,
    )

    # --- Provenance (set at import time) -----------------------------------
    xml_source_path: Mapped[str | None] = mapped_column(Text)
    xml_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 hex
    xml_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_xml: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    imported_by: Mapped[str | None] = mapped_column(String(64))

    # --- Pipeline-import additions (v2) ------------------------------------
    acquisition_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisitions.id", ondelete="SET NULL")
    )
    position: Mapped[int | None] = mapped_column(Integer)  # 1-indexed within Acquisition
    image_layout: Mapped[ImageLayout] = mapped_column(
        SAEnum(ImageLayout, name="image_layout_enum", native_enum=False, length=32),
        default=ImageLayout.SPLIT_TIF_PER_PLANE,
        nullable=False,
    )

    # --- Deletion lifecycle (v2) -------------------------------------------
    # Soft-delete: a user marks the series for deletion via the detail panel;
    # the row stays in the DB and metadata files (dats/, AuxInfo, edit.zip)
    # stay on disk. After deleted_at + grace period (default 30d), the CLI
    # `embryodb gc-deleted` permanently removes the bulky image_loc/tif*
    # subdirectories. Restoring before that just clears these two columns.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(64))

    datasets: Mapped[list[Dataset]] = relationship(
        secondary=dataset_series_table, back_populates="series"
    )
    acquisition: Mapped[Acquisition | None] = relationship(back_populates="series_list")
    microscopy: Mapped[MicroscopyMetadata | None] = relationship(
        back_populates="series", uselist=False, cascade="all, delete-orphan"
    )
    runs: Mapped[list[PipelineStepRun]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Series {self.series_name!r} status={self.status} v={self.version}>"


class Dataset(Base, TimestampMixin):
    """Named collection of Series. Replaces flat list files; supports overlap.

    `tags` is a comma-separated string of tokens used to group/filter datasets
    in the GUI (e.g. "ALZ,ceh-13" or "nob-1,enh554"). The list importer
    populates this heuristically from the source filename; users can also
    edit it.

    `source_path` records where a dataset was imported from, so we can
    show provenance and re-sync if the source file changes.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    series: Mapped[list[Series]] = relationship(
        secondary=dataset_series_table, back_populates="datasets"
    )

    def __repr__(self) -> str:
        return f"<Dataset {self.name!r} ({len(self.series)} series)>"


# -----------------------------------------------------------------------------
# v2: Pipeline-import schema
# -----------------------------------------------------------------------------


class Protocol(Base, TimestampMixin):
    """A named acquisition / processing protocol.

    Seeded from `/gpfs/fs0/l/murr/parameters/Stellaris_*` (one row per
    parameters file). Captures the channel-role mapping and the StarryNite
    parameter defaults. Picking a protocol in the import wizard is what
    distinguishes the legacy `Stellaris_tif_pipeline*.pl` variants.
    """

    __tablename__ = "protocols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    parameters_file_path: Mapped[str | None] = mapped_column(Text)
    # {raw_channel_index: role} — role in {histone, reporter, DIC, reporter2, ...}
    channel_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    default_timepts: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    default_layout: Mapped[ImageLayout] = mapped_column(
        SAEnum(ImageLayout, name="image_layout_enum_protocol", native_enum=False, length=32),
        default=ImageLayout.SPLIT_TIF_PER_PLANE,
        nullable=False,
    )
    # Cached defaults extracted from the parameters file; full file content
    # remains the canonical source. Surface a few brightness knobs here so the
    # GUI can show + edit them quickly.
    defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    acquisitions: Mapped[list[Acquisition]] = relationship(back_populates="protocol")

    def __repr__(self) -> str:
        return f"<Protocol {self.name!r} channels={self.channel_map}>"


class Acquisition(Base, TimestampMixin):
    """One microscope run. Parent of many Series (TileScan positions)."""

    __tablename__ = "acquisitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source_dir: Mapped[str] = mapped_column(Text, nullable=False)
    parser_name: Mapped[str] = mapped_column(String(64), default="leica_tilescan", nullable=False)

    protocol_id: Mapped[int | None] = mapped_column(ForeignKey("protocols.id"))
    protocol: Mapped[Protocol | None] = relationship(back_populates="acquisitions")

    raw_timepts: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staged_by: Mapped[str | None] = mapped_column(String(64))

    # Per-acquisition tweaks to the protocol's parameter defaults
    parameter_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Per-acquisition metadata shared across positions (per user clarification)
    person: Mapped[str] = mapped_column(String(128), default="")
    strain_name: Mapped[str] = mapped_column(String(128), default="")
    treatments: Mapped[str] = mapped_column(Text, default="")
    reporter_gene: Mapped[str] = mapped_column(String(128), default="")
    comments: Mapped[str] = mapped_column(Text, default="")

    series_list: Mapped[list[Series]] = relationship(back_populates="acquisition")

    def __repr__(self) -> str:
        return f"<Acquisition {self.name!r} ({len(self.series_list)} positions)>"


class MicroscopyMetadata(Base):
    """Per-series Leica/Zeiss metadata, parsed at staging time.

    One-to-one with Series. Populated from the per-position Properties.xml.
    """

    __tablename__ = "microscopy_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    series: Mapped[Series] = relationship(back_populates="microscopy")

    vendor: Mapped[str] = mapped_column(String(64), default="leica_stellaris")
    instrument: Mapped[str] = mapped_column(String(128), default="")
    objective: Mapped[str] = mapped_column(String(128), default="")
    objective_NA: Mapped[float | None] = mapped_column(Float)
    magnification: Mapped[float | None] = mapped_column(Float)
    immersion: Mapped[str] = mapped_column(String(64), default="")
    refractive_index: Mapped[float | None] = mapped_column(Float)

    voxel_xy_um: Mapped[float | None] = mapped_column(Float)
    voxel_z_um: Mapped[float | None] = mapped_column(Float)
    planes_per_volume: Mapped[int | None] = mapped_column(Integer)
    channels_per_plane: Mapped[int | None] = mapped_column(Integer)
    x_pixels: Mapped[int | None] = mapped_column(Integer)
    y_pixels: Mapped[int | None] = mapped_column(Integer)
    n_timepoints: Mapped[int | None] = mapped_column(Integer)
    cycle_time_s: Mapped[float | None] = mapped_column(Float)

    pinhole_um: Mapped[float | None] = mapped_column(Float)
    pinhole_airy: Mapped[float | None] = mapped_column(Float)
    scan_speed_hz: Mapped[float | None] = mapped_column(Float)
    line_averaging: Mapped[int | None] = mapped_column(Integer)
    frame_averaging: Mapped[int | None] = mapped_column(Integer)

    # Free-form per-channel info (name, wavelength, LUT, fluorophore, gain…)
    channels: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    stage_x_um: Mapped[float | None] = mapped_column(Float)
    stage_y_um: Mapped[float | None] = mapped_column(Float)
    stage_z_um: Mapped[float | None] = mapped_column(Float)

    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_metadata_path: Mapped[str | None] = mapped_column(Text)


class PipelineStepRun(Base):
    """Audit row for one pipeline step on one series.

    Steps the worker executes (in order): stage_images, stage_metadata,
    write_acetree_config, write_embryodb_xml, create_alias_symlink,
    write_matlab_params, run_starrynite, run_red_extract, run_measure.
    Each step writes one row; the worker re-checks on resume.
    """

    __tablename__ = "pipeline_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    series: Mapped[Series] = relationship(back_populates="runs")
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, name="run_status_enum", native_enum=False, length=16),
        default=RunStatus.PENDING,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Off-hours scheduling: worker skips this row until `not_before` elapses.
    # Used by the wizard's "Delay (hours)" knob to push heavy I/O to nights.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    log_path: Mapped[str | None] = mapped_column(Text)
    error_excerpt: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<Run {self.step} series={self.series_id} status={self.status}>"


__all__ = [
    "Base",
    "Series",
    "Dataset",
    "Status",
    "ImageLayout",
    "RunStatus",
    "Acquisition",
    "Protocol",
    "MicroscopyMetadata",
    "PipelineStepRun",
    "dataset_series_table",
]
