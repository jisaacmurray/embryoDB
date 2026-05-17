"""Seed Protocol rows from `/gpfs/fs0/l/murr/parameters/Stellaris_*`.

Each parameters file becomes one Protocol with:
- channel_map inferred from the protocol name (the three legacy variants
  encode the histone/reporter assignment in their filename suffix)
- defaults pulled from the tunable subset of the parameter file
- default_timepts from `end_time` if present, else 240

The user can edit `channel_map` afterward via the GUI; this seeder gets us
to the right starting state without manual data entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ImageLayout, Protocol
from ..parsers.matlab_params import extract_defaults, load


# Heuristic: derive channel_map from the protocol filename.
#
# Sequential acquisition (RW10029): ch00=histone (GFP), ch01=reporter (red).
# Simultaneous (JIM113-style): ch00=reporter, ch01=histone.
# Other strains we treat conservatively as "looks like JIM113" if not RW10029.
def _channel_map_for_name(name: str) -> dict[str, str]:
    lowered = name.lower()
    if "rw10029" in lowered:
        return {"0": "histone", "1": "reporter"}
    if "dic" in lowered:
        return {"0": "reporter", "1": "histone", "2": "DIC"}
    # Default to JIM113-style swap
    return {"0": "reporter", "1": "histone"}


@dataclass
class SeedReport:
    inserted: list[str]
    updated: list[str]
    skipped: list[tuple[str, str]]

    def summary(self) -> str:
        return (
            f"inserted: {len(self.inserted)}, "
            f"updated: {len(self.updated)}, "
            f"skipped: {len(self.skipped)}"
        )


def seed_protocols(
    session: Session,
    parameters_dir: Path | str | None = None,
    name_prefix: str = "Stellaris_",
) -> SeedReport:
    """Scan `parameters_dir` for files starting with `name_prefix` and create
    a Protocol per file. Idempotent — re-running updates defaults from disk
    without clobbering manually-edited channel maps.
    """
    pdir = Path(parameters_dir or "/gpfs/fs0/l/murr/parameters")
    inserted: list[str] = []
    updated: list[str] = []
    skipped: list[tuple[str, str]] = []

    for entry in sorted(pdir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        # Skip backup files (`~` suffix) and very-variant suffixes that just
        # tweak brightness; users pick the base protocol and apply overrides.
        if not name.startswith(name_prefix):
            continue
        if name.endswith("~"):
            skipped.append((name, "backup file"))
            continue

        try:
            params = load(entry)
        except Exception as exc:
            skipped.append((name, f"parse error: {exc}"))
            continue

        defaults = extract_defaults(params)
        default_timepts = params.get_int("end_time") or 240

        existing = session.execute(
            select(Protocol).where(Protocol.name == name)
        ).scalar_one_or_none()

        if existing is None:
            row = Protocol(
                name=name,
                parameters_file_path=str(entry),
                channel_map=_channel_map_for_name(name),
                default_timepts=default_timepts,
                default_layout=ImageLayout.SPLIT_TIF_PER_PLANE,
                defaults=defaults,
            )
            session.add(row)
            inserted.append(name)
        else:
            existing.parameters_file_path = str(entry)
            existing.default_timepts = default_timepts
            existing.defaults = defaults
            # Don't overwrite channel_map — user may have customized it.
            updated.append(name)

    return SeedReport(inserted=inserted, updated=updated, skipped=skipped)
