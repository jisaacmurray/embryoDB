"""Bulk-import flat list files (e.g. /gpfs/fs0/l/murr/lists/) into Datasets.

Each text file in the source directory becomes one Dataset. The filename
(stripped of `.txt`) is used as the dataset name. Lines are taken as series
names; blank lines and `#`-comment lines are ignored. Members that don't
exist in the database are skipped with a warning and recorded in the report.

`tags` are auto-populated from the filename via small heuristics so the GUI
can group/filter the 500+ legacy lists by some organizing principle without
forcing a manual taxonomy.

Re-running on the same directory is safe:
- Datasets whose source-file content is unchanged are skipped.
- Datasets that already exist with the same name are refreshed (members
  re-resolved) only if the source-file content has changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dataset, Series


@dataclass
class ListImportReport:
    inserted: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)
    missing_series: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        missing_total = sum(len(v) for v in self.missing_series.values())
        return (
            f"inserted: {len(self.inserted)}, "
            f"refreshed: {len(self.refreshed)}, "
            f"unchanged: {len(self.unchanged)}, "
            f"skipped: {len(self.skipped)}, "
            f"missing series refs: {missing_total} across "
            f"{len(self.missing_series)} datasets"
        )


_DATE_RE = re.compile(r"^(\d{8})_(.+)$")
_ALZ_RE = re.compile(r"^ALZ_(.+)$")
_GENE_HINT_RE = re.compile(r"([a-z]+-\d+(?:\.[a-z]+)?|enh\d+)", re.IGNORECASE)
_STRAIN_RE = re.compile(r"(JIM\d+|RW\d+|OU\d+|CHB\d+|CHBKM\d+|FUC\d+)", re.IGNORECASE)


def _stem(path: Path) -> str:
    """List files use `.txt` inconsistently — many have no extension at all."""
    name = path.name
    if name.endswith(".txt"):
        name = name[: -len(".txt")]
    return name


def derive_tags(filename: str) -> list[str]:
    """Heuristic tags from a list file name.

    Examples:
      `20180402_nob-1_e76_vector_JIM497_L2` ->
          ["2018", "nob-1", "JIM497"]
      `ALZ_20160627_ceh-13_GFP` ->
          ["ALZ", "2016", "ceh-13"]
      `20220329_irx-1_pop-1i_list` ->
          ["2022", "irx-1", "pop-1"]
    """
    tags: list[str] = []
    name = filename
    m = _ALZ_RE.match(name)
    if m:
        tags.append("ALZ")
        name = m.group(1)
    m = _DATE_RE.match(name)
    if m:
        tags.append(m.group(1)[:4])  # year
    for gene in _GENE_HINT_RE.findall(name):
        if gene.lower() not in (t.lower() for t in tags):
            tags.append(gene)
    for strain in _STRAIN_RE.findall(name):
        upper = strain.upper()
        if upper not in tags:
            tags.append(upper)
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _parse_list_file(path: Path) -> list[str]:
    members: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Some legacy lists have whitespace-separated annotations after the
        # series name; take the first token.
        members.append(line.split()[0])
    return members


def import_lists(
    session: Session,
    source_dir: Path,
    glob: str = "*",
) -> ListImportReport:
    """Import every file under `source_dir` matching `glob` as a Dataset."""
    src = Path(source_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"list source-dir does not exist: {src}")

    # Build the set of valid series names once for fast membership checks.
    all_series = {
        n for (n,) in session.execute(select(Series.series_name)).all()
    }

    report = ListImportReport()
    for path in sorted(src.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        # Skip editor / OS cruft that lab members occasionally leave behind:
        # - emacs autosaves       (#name#)
        # - emacs backups         (name~)
        # - macOS metadata        (.DS_Store, ._*)
        # - any dotfile           (hidden by convention)
        if name.startswith(".") or name.startswith("#") or name.endswith("~"):
            report.skipped.append((name, "editor/OS cruft"))
            continue
        # Skip obvious non-list files
        if path.suffix in {".jpg", ".png", ".pdf", ".zip", ".log"}:
            report.skipped.append((name, f"unsupported suffix {path.suffix}"))
            continue
        try:
            members = _parse_list_file(path)
        except UnicodeDecodeError:
            report.skipped.append((name, "binary file"))
            continue
        if not members:
            report.skipped.append((name, "empty"))
            continue

        ds_name = _stem(path)
        tags_csv = ",".join(derive_tags(ds_name))

        # Deduplicate but preserve order: list files occasionally repeat
        # the same series, and dataset membership is a set.
        seen_members: set[str] = set()
        unique_members: list[str] = []
        for m in members:
            if m in seen_members:
                continue
            seen_members.add(m)
            unique_members.append(m)
        resolved = [m for m in unique_members if m in all_series]
        missing = [m for m in unique_members if m not in all_series]

        existing = session.execute(
            select(Dataset).where(Dataset.name == ds_name)
        ).scalar_one_or_none()

        if existing is None:
            ds = Dataset(
                name=ds_name,
                description="",
                tags=tags_csv,
                source_path=str(path),
                series=[
                    session.execute(
                        select(Series).where(Series.series_name == m)
                    ).scalar_one()
                    for m in resolved
                ],
            )
            session.add(ds)
            report.inserted.append(ds_name)
        else:
            # Check whether the existing dataset matches the on-disk content.
            current_members = {s.series_name for s in existing.series}
            if (
                current_members == set(resolved)
                and existing.tags == tags_csv
                and existing.source_path == str(path)
            ):
                report.unchanged.append(ds_name)
            else:
                existing.tags = tags_csv
                existing.source_path = str(path)
                existing.series = [
                    session.execute(
                        select(Series).where(Series.series_name == m)
                    ).scalar_one()
                    for m in resolved
                ]
                report.refreshed.append(ds_name)

        if missing:
            report.missing_series[ds_name] = missing

    return report
