"""Tests for the ACD z plane-spacing resolution.

The CD `z` column is a plane index and is multiplied straight through to
microns, with none of the standard-embryo normalization that cancels the
movie's pixel size out of x and y. GetACD.pl hardcoded 0.504 um and ignored
the `zpixres` column it parsed, so a stack acquired at 1.007 um came out half
as deep as it really was.
"""

from __future__ import annotations

from embryodb.getacd import DEFAULT_Z_RES, resolve_z_res, transform_coordinates

AUX_HEADER = (
    "name,slope,intercept,xc,yc,maj,min,ang,zc,zslope,time,zpixres,axis\n"
)


def test_db_voxel_z_wins():
    z, src = resolve_z_res({"zpixres": "5.7471"}, voxel_z_um=1.007, voxel_xy_um=0.0864)
    assert z == 1.007
    assert "voxel_z_um" in src


def test_falls_back_to_zpixres_ratio():
    """zpixres is the z/xy ratio, so it needs the movie's xy size to be useful."""
    z, src = resolve_z_res({"zpixres": "5.7471"}, voxel_z_um=None, voxel_xy_um=0.0864)
    assert z == 5.7471 * 0.0864
    assert "zpixres" in src


def test_falls_back_to_constant_without_db_values():
    assert resolve_z_res({"zpixres": "5.7471"})[0] == DEFAULT_Z_RES
    assert resolve_z_res({})[0] == DEFAULT_Z_RES


def test_blank_or_garbage_zpixres_is_ignored():
    assert resolve_z_res({"zpixres": ""}, voxel_xy_um=0.0864)[0] == DEFAULT_Z_RES
    assert resolve_z_res({"zpixres": "n/a"}, voxel_xy_um=0.0864)[0] == DEFAULT_Z_RES


def test_transform_scales_z_by_the_resolved_spacing(tmp_path):
    """A coarse-step movie must come out proportionally deeper."""
    aux = tmp_path / "aux.csv"
    # ang=0 and xc/yc/zc=0 so only the micron scaling is exercised.
    aux.write_text(AUX_HEADER + "s,0,0,0,0,571,348,0,0,0,0,5.7471,ADL\n")
    rows = [{"cell": "P0", "time": "1", "x": "0", "y": "0", "z": "10"}]

    default = transform_coordinates(rows, aux)
    assert float(default[0]["z"]) == 10 * DEFAULT_Z_RES

    coarse = transform_coordinates(rows, aux, voxel_z_um=1.007)
    assert float(coarse[0]["z"]) == 10 * 1.007
    # The bug this fixes: the legacy constant halved a 1.007 um stack.
    assert float(coarse[0]["z"]) / float(default[0]["z"]) > 1.9


def test_transform_leaves_xy_on_the_reference_scale(tmp_path):
    """x/y are normalized onto the reference embryo, so voxel size can't move them."""
    aux = tmp_path / "aux.csv"
    aux.write_text(AUX_HEADER + "s,0,0,0,0,571,348,0,0,0,0,5.7471,ADL\n")
    rows = [{"cell": "P0", "time": "1", "x": "100", "y": "50", "z": "10"}]

    base = transform_coordinates(rows, aux)
    other = transform_coordinates(rows, aux, voxel_z_um=1.007, voxel_xy_um=0.12)
    assert other[0]["x"] == base[0]["x"]
    assert other[0]["y"] == base[0]["y"]
