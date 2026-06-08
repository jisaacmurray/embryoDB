"""Tests for the LIF extractor + import flow.

`readlif`'s binary format is too esoteric to fake at the byte level, so we
mock :class:`readlif.reader.LifFile` with a fake that exposes the handful
of attributes the extractor reads (``xml_root``, ``get_iter_image``, and
per-image ``name`` / ``nt`` / ``nz`` / ``channels`` / ``bit_depth`` /
``dims`` / ``scale`` / ``get_frame``). The fake's ``xml_root`` is a real
LIF-shaped ElementTree so the metadata slice path is exercised for real.

Coverage:

- ``extract_position`` pixel loop: path_fn invocation, file writes,
  idempotent skip-on-exists, bit-depth ``pass`` / ``downcast`` / ``fail``.
- ``_position_number``: parses the Leica position number from a name and
  falls back to enumeration order when absent.
- ``import_lif`` end-to-end on a fake LIF whose positions come back out of
  numeric order — proves the ``_L<N>`` series suffix tracks the real Leica
  position number, not the iteration index (the bug fixed alongside).
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import numpy as np
import pytest
import tifffile

from embryodb.lif.extractor import LifExtractor
from embryodb.lif.import_flow import _position_number


# ---------------------------------------------------------------------------
# Fake readlif.reader.LifFile
# ---------------------------------------------------------------------------


class _FakeDims:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


class _FakeLifImage:
    """Minimal stand-in for ``readlif.reader.LifImage``.

    ``get_frame`` returns a constant-valued array encoding (t, z, c) so
    tests can assert which plane landed where if they want; the constant
    is clipped into the requested dtype.
    """

    def __init__(
        self,
        name: str,
        *,
        nt: int,
        nz: int,
        channels: int,
        bit_depth: tuple[int, ...],
        x: int = 4,
        y: int = 4,
        scale: tuple = (1 / 0.0865, 1 / 0.0865, 1 / 0.5002, 1 / 114.0),
        dtype=np.uint8,
        fill: int | None = None,
    ):
        self.name = name
        self.nt = nt
        self.nz = nz
        self.channels = channels
        self.bit_depth = bit_depth
        self.dims = _FakeDims(x, y)
        self.scale = scale
        self._dtype = dtype
        self._fill = fill

    def get_frame(self, *, z: int, t: int, c: int):
        val = self._fill if self._fill is not None else (t * 100 + z * 10 + c)
        return np.full((self.dims.y, self.dims.x), val, dtype=self._dtype)


def _build_lif_xml(series_specs, n_channels: int = 2) -> ET.Element:
    """LIF-shaped xml_root for the given ``[(series, [positions]), ...]``."""
    root = ET.Element("LMSDataContainerHeader")
    project = ET.SubElement(root, "Element", Name="Project")
    proj_children = ET.SubElement(project, "Children")
    for sname, positions in series_specs:
        series = ET.SubElement(proj_children, "Element", Name=sname)
        schildren = ET.SubElement(series, "Children")
        for pname in positions:
            pos = ET.SubElement(schildren, "Element", Name=pname)
            image = ET.SubElement(ET.SubElement(pos, "Data"), "Image")
            desc = ET.SubElement(image, "ImageDescription")
            dims = ET.SubElement(desc, "Dimensions")
            for dim_id, n, voxel_um in (
                ("1", 4, 0.0865),
                ("2", 4, 0.0865),
                ("3", 3, 0.5002),
                ("4", 2, 0.0),
            ):
                ET.SubElement(
                    dims, "DimensionDescription",
                    DimID=dim_id, NumberOfElements=str(n),
                    Length=repr((voxel_um * 1e-6) * n), Unit="m",
                )
            channels = ET.SubElement(desc, "Channels")
            for ci in range(n_channels):
                ET.SubElement(channels, "ChannelDescription", ChannelTag=str(ci))
    return root


class FakeLifFile:
    """Stand-in for ``readlif.reader.LifFile``.

    Built from a ``series_specs`` list of ``(series_name, [position_names])``.
    Images are yielded in the order positions appear in ``series_specs`` —
    callers pass scrambled position orders to exercise the numbering fix.
    """

    series_specs = [("TileScan 1", ["Position 1", "Position 2"])]
    image_dtype = np.uint8
    image_bit_depth = (8, 8)
    n_channels = 2

    def __init__(self, path):
        self.path = path
        self.xml_root = _build_lif_xml(self.series_specs, self.n_channels)
        self._images = [
            _FakeLifImage(
                f"{sname}/{pname}",
                nt=2, nz=3, channels=self.n_channels,
                bit_depth=self.image_bit_depth,
                dtype=self.image_dtype,
            )
            for sname, positions in self.series_specs
            for pname in positions
        ]

    def get_iter_image(self):
        return iter(self._images)


@pytest.fixture
def seeded_protocols(db_session, tmp_path):
    """Seed the Stellaris_* protocols (mirrors the test_pipeline fixture)."""
    from embryodb.pipeline.protocol_seed import seed_protocols

    params_dir = tmp_path / "params"
    params_dir.mkdir()
    body = "%test fixture\nslices=3;\nxyres=0.087;\nzres=0.5;\nfirsttimestepdiam=80;\n"
    (params_dir / "Stellaris_JIM113").write_text(body)
    (params_dir / "Stellaris_RW10029").write_text(body)
    return seed_protocols(db_session, parameters_dir=params_dir)


@pytest.fixture
def install_fake_lif(monkeypatch):
    """Patch readlif.reader.LifFile; return a configurator for the fake."""

    def _configure(*, series_specs=None, dtype=np.uint8, bit_depth=(8, 8), n_channels=2):
        class _Configured(FakeLifFile):
            pass

        if series_specs is not None:
            _Configured.series_specs = series_specs
        _Configured.image_dtype = dtype
        _Configured.image_bit_depth = bit_depth
        _Configured.n_channels = n_channels
        monkeypatch.setattr("readlif.reader.LifFile", _Configured)
        return _Configured

    return _configure


# ---------------------------------------------------------------------------
# _position_number helper (the numbering fix)
# ---------------------------------------------------------------------------


def test_position_number_parses_leica_name():
    assert _position_number("Position 4", 99) == 4
    assert _position_number("Position 12", 99) == 12
    assert _position_number("position 7", 99) == 7  # case-insensitive


def test_position_number_falls_back_to_enumeration():
    assert _position_number("Weird Name", 5) == 5
    assert _position_number("", 3) == 3


# ---------------------------------------------------------------------------
# extract_position pixel loop
# ---------------------------------------------------------------------------


def test_extract_position_writes_all_planes(install_fake_lif, tmp_path):
    install_fake_lif()
    ex = LifExtractor("ignored.lif")

    seen: list[tuple[int, int, int]] = []

    def path_fn(t, z, c):
        seen.append((t, z, c))
        return tmp_path / f"t{t}_z{z}_c{c}.tif"

    report = ex.extract_position(
        "TileScan 1", "Position 1", path_fn=path_fn, compress=True
    )
    assert report.error is None
    # 2 t × 3 z × 2 c = 12 planes
    assert report.planes_total == 12
    assert report.planes_written == 12
    assert report.planes_skipped == 0
    assert len(seen) == 12
    assert len(list(tmp_path.glob("*.tif"))) == 12
    # Written files are real LZW TIFFs of the right shape.
    arr = tifffile.imread(tmp_path / "t0_z0_c0.tif")
    assert arr.shape == (4, 4)


def test_extract_position_is_idempotent(install_fake_lif, tmp_path):
    install_fake_lif()
    ex = LifExtractor("ignored.lif")

    def path_fn(t, z, c):
        return tmp_path / f"t{t}_z{z}_c{c}.tif"

    first = ex.extract_position("TileScan 1", "Position 1", path_fn=path_fn)
    assert first.planes_written == 12
    # Second run: all destinations exist → skipped, nothing rewritten.
    second = ex.extract_position("TileScan 1", "Position 1", path_fn=path_fn)
    assert second.planes_written == 0
    assert second.planes_skipped == 12


def test_extract_position_overwrite_rewrites(install_fake_lif, tmp_path):
    install_fake_lif()
    ex = LifExtractor("ignored.lif")

    def path_fn(t, z, c):
        return tmp_path / f"t{t}_z{z}_c{c}.tif"

    ex.extract_position("TileScan 1", "Position 1", path_fn=path_fn)
    again = ex.extract_position(
        "TileScan 1", "Position 1", path_fn=path_fn, overwrite=True
    )
    assert again.planes_written == 12
    assert again.planes_skipped == 0


def test_extract_position_missing_returns_error(install_fake_lif, tmp_path):
    install_fake_lif()
    ex = LifExtractor("ignored.lif")
    report = ex.extract_position(
        "TileScan 1", "Position 99", path_fn=lambda t, z, c: tmp_path / "x.tif"
    )
    assert report.error is not None
    assert "not in LIF" in report.error


# ---------------------------------------------------------------------------
# Bit-depth policy
# ---------------------------------------------------------------------------


def test_downcast_shifts_uint16_to_uint8(install_fake_lif, tmp_path):
    # 12-bit data stored in uint16; downcast shifts right by (12 - 8) = 4.
    install_fake_lif(dtype=np.uint16, bit_depth=(12, 12))
    ex = LifExtractor("ignored.lif")

    paths: list = []

    def path_fn(t, z, c):
        p = tmp_path / f"t{t}_z{z}_c{c}.tif"
        paths.append(p)
        return p

    report = ex.extract_position(
        "TileScan 1", "Position 1", path_fn=path_fn,
        bit_depth_policy="downcast",
    )
    assert report.error is None
    assert report.bit_depth_warning is not None
    arr = tifffile.imread(paths[0])
    assert arr.dtype == np.uint8


def test_fail_policy_raises_on_uint16(install_fake_lif, tmp_path):
    install_fake_lif(dtype=np.uint16, bit_depth=(16, 16))
    ex = LifExtractor("ignored.lif")
    # bit_depth_policy='fail' is the cautious opt-in: it raises rather than
    # silently downcasting, aborting the extraction.
    with pytest.raises(ValueError, match="fail"):
        ex.extract_position(
            "TileScan 1", "Position 1",
            path_fn=lambda t, z, c: tmp_path / f"{t}_{z}_{c}.tif",
            bit_depth_policy="fail",
        )


def test_pass_policy_keeps_uint16(install_fake_lif, tmp_path):
    install_fake_lif(dtype=np.uint16, bit_depth=(16, 16))
    ex = LifExtractor("ignored.lif")
    p = tmp_path / "out.tif"
    report = ex.extract_position(
        "TileScan 1", "Position 1",
        path_fn=lambda t, z, c: p, overwrite=True,
        bit_depth_policy="pass",
    )
    assert report.error is None
    assert tifffile.imread(p).dtype == np.uint16


# ---------------------------------------------------------------------------
# list_series metadata walk
# ---------------------------------------------------------------------------


def test_list_series_reports_positions_in_document_order(install_fake_lif):
    install_fake_lif(
        series_specs=[("TileScan 1", ["Position 2", "Position 1", "Position 4"])]
    )
    ex = LifExtractor("ignored.lif")
    series = ex.list_series()
    assert len(series) == 1
    names = [p.name for p in series[0].positions]
    assert names == ["Position 2", "Position 1", "Position 4"]


# ---------------------------------------------------------------------------
# import_lif end-to-end (the numbering fix in context)
# ---------------------------------------------------------------------------


def test_import_lif_names_series_by_leica_position(
    install_fake_lif, db_session, seeded_protocols, tmp_path
):
    """Positions come back out of numeric order; series must still be named
    by the real Leica position number, matching a LAS X export."""
    from sqlalchemy import select

    from embryodb.lif.import_flow import import_lif
    from embryodb.models import Protocol, Series
    from embryodb.pipeline.orchestrate import ImportOptions

    # readlif yields Position 2 first, then Position 1.
    install_fake_lif(series_specs=[("TileScan 1", ["Position 2", "Position 1"])])

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()

    img_root = tmp_path / "images"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    opts = ImportOptions(
        image_loc_root=img_root,
        alias_root=None,
        user="testuser",
        run_through_step="write_matlab_params",
    )

    result = import_lif(
        db_session,
        lif_path=tmp_path / "JIM593.lif",
        series_name="TileScan 1",
        protocol=proto,
        options=opts,
        person="alice",
        strain_name="N2",
        legacy_xml_dir=legacy_dir,
    )

    # Series named by the Leica position number, NOT the iteration order.
    names = {s.series_name for s in result.acquisition.series_list}
    assert names == {"JIM593_L1", "JIM593_L2"}

    # The L2 dir holds the data from the first-iterated "Position 2".
    assert (img_root / "testuser" / "images" / "JIM593_L2" / "tif").is_dir()
    assert (img_root / "testuser" / "images" / "JIM593_L1" / "tif").is_dir()

    # JIM113 channel map is swapped (0=reporter→tifR, 1=histone→tif).
    l1 = img_root / "testuser" / "images" / "JIM593_L1"
    assert (l1 / "tif").is_dir() and (l1 / "tifR").is_dir()
    # 2 t × 3 z planes per channel dir.
    assert len(list((l1 / "tif").glob("*.tif"))) == 6
    # Z-inversion: deepest plane (z=0) → p03, z=2 → p01.
    assert (l1 / "tif" / "JIM593_L1-t001-p03.tif").exists()
    assert (l1 / "tif" / "JIM593_L1-t001-p01.tif").exists()

    # Properties.xml sidecar uses the Leica position number too.
    assert (l1 / "dats" / "JIM593_Position 1_Properties.xml").exists()
    l2 = img_root / "testuser" / "images" / "JIM593_L2"
    assert (l2 / "dats" / "JIM593_Position 2_Properties.xml").exists()

    # Metadata parsed off the sliced XML.
    s1 = db_session.execute(
        select(Series).where(Series.series_name == "JIM593_L1")
    ).scalar_one()
    assert s1.microscopy is not None
    assert s1.microscopy.voxel_xy_um == pytest.approx(0.0865, rel=1e-3)
    assert s1.microscopy.voxel_z_um == pytest.approx(0.5002, rel=1e-3)


def test_import_lif_single_channel_forces_histone(
    install_fake_lif, db_session, seeded_protocols, tmp_path
):
    """A single-channel acquisition is always histone (tif/), even under a
    protocol like JIM113 whose channel 0 default is reporter (tifR/)."""
    from sqlalchemy import select

    from embryodb.lif.import_flow import import_lif
    from embryodb.models import Protocol
    from embryodb.pipeline.orchestrate import ImportOptions

    # One channel only; bit_depth tuple length matches.
    install_fake_lif(
        series_specs=[("TileScan 1", ["Position 1"])],
        n_channels=1,
        bit_depth=(8,),
    )
    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()

    img_root = tmp_path / "images"
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    opts = ImportOptions(
        image_loc_root=img_root,
        alias_root=None,
        user="testuser",
        run_through_step="write_matlab_params",
    )

    import_lif(
        db_session,
        lif_path=tmp_path / "JIM593.lif",
        series_name="TileScan 1",
        protocol=proto,
        options=opts,
        legacy_xml_dir=legacy_dir,
    )

    l1 = img_root / "testuser" / "images" / "JIM593_L1"
    # Histone landed in tif/; reporter dir was never created.
    assert (l1 / "tif").is_dir()
    assert len(list((l1 / "tif").glob("*.tif"))) == 6  # 2 t × 3 z
    assert not (l1 / "tifR").exists()


def test_import_lif_single_channel_override_still_wins(
    install_fake_lif, db_session, seeded_protocols, tmp_path
):
    """An explicit channel_role_override beats the single-channel histone rule."""
    from sqlalchemy import select

    from embryodb.lif.import_flow import import_lif
    from embryodb.models import Protocol
    from embryodb.pipeline.orchestrate import ImportOptions

    install_fake_lif(
        series_specs=[("TileScan 1", ["Position 1"])],
        n_channels=1,
        bit_depth=(8,),
    )
    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()

    opts = ImportOptions(
        image_loc_root=tmp_path / "images",
        alias_root=None,
        user="testuser",
        run_through_step="write_matlab_params",
    )
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()

    import_lif(
        db_session,
        lif_path=tmp_path / "JIM593.lif",
        series_name="TileScan 1",
        protocol=proto,
        options=opts,
        legacy_xml_dir=legacy_dir,
        channel_role_override={0: "reporter"},
    )

    l1 = tmp_path / "images" / "testuser" / "images" / "JIM593_L1"
    assert (l1 / "tifR").is_dir()
    assert not (l1 / "tif").exists()
