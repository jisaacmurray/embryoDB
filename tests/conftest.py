"""pytest fixtures: each test gets a fresh in-memory SQLite DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import database


@pytest.fixture
def db_session():
    """In-memory SQLite session for one test."""
    database.reset_for_tests("sqlite:///:memory:")
    database.create_all()
    with database.session_scope() as s:
        yield s


SAMPLE_XML = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="20240805_JIM763_L5"/>
<date date="20240820"/>
<person name="azach"/>
<strain name="n/a"/>
<treatments desc="n/a"/>
<redsig value="n/a"/>
<imageloc loc="/gpfs/fs0/u/azach/images/20240805_JIM763_L5"/>
<timepts num="240"/>
<annots loc="/gpfs/fs0/u/azach/images/20240805_JIM763_L5"/>
<acetree config="20240805_JIM763_L5.xml"/>
<editedby name="BW"/>
<editedtimepts num="190"/>
<editedcells num="220"/>
<checkedby name="120,ABala:180,ABprp:180,MSpapp:180"/>
<comments text="Pretty severe developmental delay"/>
<status case="new"/>
</experiment>
"""

# Legacy file with the lab's unescaped '<' in comments (real example).
SAMPLE_XML_UNESCAPED_LT = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="20110509_JIM65_L3"/>
<date date="20110508"/>
<person name="twal"/>
<strain name="JIM65 cdk-1(ne2257)"/>
<treatments desc="25*C overnight"/>
<redsig value="ref-2.a"/>
<imageloc loc="/gpfs/fs0/u/twal/images/20110509_JIM65_L3"/>
<timepts num="240"/>
<annots loc="/gpfs/fs0/u/twal/images/20110509_JIM65_L3"/>
<acetree config="20110509_JIM65_L3.xml"/>
<editedby name="TW"/>
<editedtimepts num="120"/>
<editedcells num="136"/>
<checkedby name="n/a"/>
<comments text="MS->EMS,E-<EMS,C->EMS but broad within MSa"/>
<status case="new"/>
</experiment>
"""

# Legacy file missing one of the 16 elements (real example).
SAMPLE_XML_MISSING_STATUS = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="20141220_JIM113_L3"/>
<date date="20141221"/>
<person name="jmurr"/>
<strain name="n/a"/>
<treatments desc="n/a"/>
<redsig value="n/a"/>
<imageloc loc="/x"/>
<timepts num="240"/>
<annots loc="/x"/>
<acetree config="20141220_JIM113_L3.xml"/>
<editedby name="n/a"/>
<editedtimepts num="n/a"/>
<editedcells num="n/a"/>
<checkedby name="n/a"/>
<comments text="n/a"/>
</experiment>
"""


@pytest.fixture
def sample_xml() -> str:
    return SAMPLE_XML


@pytest.fixture
def sample_xml_unescaped() -> str:
    return SAMPLE_XML_UNESCAPED_LT


@pytest.fixture
def sample_xml_missing_status() -> str:
    return SAMPLE_XML_MISSING_STATUS


@pytest.fixture
def make_source_dir(tmp_path: Path):
    """Returns a builder: write {name: content} to a tmp source dir."""

    def _build(files: dict[str, str]) -> Path:
        src = tmp_path / "source"
        src.mkdir()
        for name, content in files.items():
            (src / name).write_text(content, encoding="utf-8")
        return src

    return _build
