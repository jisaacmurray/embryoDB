"""Parser/serializer round-trip and edge cases."""

from embryodb.parsers.xml import (
    XmlFormatError,
    looks_like_legacy_format,
    parse,
    round_trip_ok,
    serialize,
)


def test_parse_canonical(sample_xml: str) -> None:
    rec = parse(sample_xml)
    assert rec["series_name"] == "20240805_JIM763_L5"
    assert rec["date_acquired"] == "20240820"
    assert rec["status"] == "new"
    assert rec["partial_editing_code"] == "120,ABala:180,ABprp:180,MSpapp:180"


def test_round_trip_canonical(sample_xml: str) -> None:
    assert round_trip_ok(sample_xml)


def test_parses_unescaped_lt(sample_xml_unescaped: str) -> None:
    # The lenient parser handles unescaped '<' in attribute values that the
    # legacy Java writer sometimes emits. Strict ET would reject this.
    rec = parse(sample_xml_unescaped)
    assert "E-<EMS" in rec["comments"]


def test_unescaped_lt_does_not_round_trip_via_serializer(
    sample_xml_unescaped: str,
) -> None:
    # The canonical serializer escapes '<' (correct XML), so it does NOT
    # produce byte-identical output. The exporter handles this by preserving
    # raw_xml verbatim for unmodified rows; the test_round_trip suite covers
    # the end-to-end case.
    assert not round_trip_ok(sample_xml_unescaped)


def test_missing_element_parses_as_empty(sample_xml_missing_status: str) -> None:
    rec = parse(sample_xml_missing_status)
    assert rec["series_name"] == "20141220_JIM113_L3"
    assert rec["status"] == ""  # missing -> empty string


def test_missing_element_does_not_round_trip(sample_xml_missing_status: str) -> None:
    # Round-trip diverges (serializer always writes 16 fields). This is by
    # design: missing-element files must be preserved verbatim via raw_xml,
    # not regenerated. The exporter has that branch.
    assert not round_trip_ok(sample_xml_missing_status)


def test_unknown_element_raises() -> None:
    bad = (
        "<?xml version='1.0' encoding='utf-8'?>\n\n"
        "<experiment>\n<bogus name=\"x\"/>\n</experiment>\n"
    )
    try:
        parse(bad)
    except XmlFormatError:
        return
    raise AssertionError("expected XmlFormatError")


def test_serialize_escapes_ampersand_and_lt() -> None:
    rec = {
        "series_name": "x",
        "date_acquired": "20240101",
        "person": "p",
        "strain_name": "",
        "treatments": "",
        "reporter_gene": "",
        "image_loc": "",
        "timepts": "",
        "annot_loc": "",
        "acetree_config": "",
        "edited_by": "",
        "edited_timepts": "",
        "edited_cells": "",
        "partial_editing_code": "",
        "comments": "A & B < C",
        "status": "new",
    }
    out = serialize(rec)
    assert 'text="A &amp; B &lt; C"' in out
    # > is NOT escaped (matches legacy Java writer)
    assert "&gt;" not in serialize({**rec, "comments": "A > B"})


def test_looks_like_legacy_format(sample_xml: str) -> None:
    assert looks_like_legacy_format(sample_xml)
    assert not looks_like_legacy_format("<?xml version=\"1.0\"?><x/>")
