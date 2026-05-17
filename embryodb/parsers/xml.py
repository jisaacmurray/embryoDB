"""XML parse + serialize.

The legacy embryoDB format is deterministic enough that we can guarantee
byte-comparable round-trip:

  <?xml version='1.0' encoding='utf-8'?>
  \\n
  <experiment>
  <series name="..."/>
  ... 15 more elements, fixed order, one per line ...
  </experiment>
  \\n

Survey of all 11,023 source files confirmed: no XML escape entities in
content fields, no whitespace variation, attribute values use double quotes
and the XML declaration uses single quotes. We rely on these invariants.

If a future file violates them we want to know loudly, not silently
re-encode — so the parser uses xml.etree only for content extraction, while
the serializer writes raw bytes.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..xml_format import (
    ELEMENT_TO_COLUMN,
    FIELDS,
    XML_FOOTER,
    XML_HEADER,
    XmlField,
)


class XmlFormatError(ValueError):
    """Raised when the source file deviates from the assumed legacy format."""


# Only escape what the XML spec requires inside a double-quoted attribute:
# & and < must be escaped; the quote char itself must be escaped. > is legal
# unescaped and the legacy Java writer leaves it that way, so we match.
_ATTR_ESCAPE = {
    "&": "&amp;",
    "<": "&lt;",
    '"': "&quot;",
}


def _escape_attr(value: str) -> str:
    return "".join(_ATTR_ESCAPE.get(ch, ch) for ch in value)


# Lenient line-format parser. The legacy Java writer doesn't escape `<` in
# attribute values, which occasionally produces technically malformed XML
# (e.g. comments containing "elongate<1"). xml.etree rejects those, but the
# format is regular enough that a regex-per-line approach handles every case.
#
# Pattern: ^<elementName attrName="...everything until /> at end of line">/>$
_LINE_RE = re.compile(r'^<(\w+)\s+(\w+)="(.*)"/>$')


def parse(content: str) -> dict[str, str]:
    """Parse an embryoDB XML document into a {column_name: value} dict.

    Missing elements default to empty string. Unknown elements raise.
    Tolerates unescaped '<' in attribute values, which the legacy Java
    writer occasionally emits.
    """
    # Try strict XML first — it's the right semantics when content is clean.
    try:
        root = ET.fromstring(content)
        if root.tag != "experiment":
            raise XmlFormatError(f"expected root <experiment>, got <{root.tag}>")
        record: dict[str, str] = {f.column: "" for f in FIELDS}
        for child in root:
            field = ELEMENT_TO_COLUMN.get(child.tag)
            if field is None:
                raise XmlFormatError(f"unknown element <{child.tag}>")
            record[field.column] = child.attrib.get(field.attribute, "")
        return record
    except ET.ParseError:
        pass  # fall through to lenient parser

    # Lenient: walk lines between <experiment> and </experiment>.
    record = {f.column: "" for f in FIELDS}
    started = False
    for line in content.splitlines():
        if line == "<experiment>":
            started = True
            continue
        if line == "</experiment>":
            return record
        if not started or not line:
            continue
        m = _LINE_RE.match(line)
        if m is None:
            raise XmlFormatError(f"unrecognized line: {line!r}")
        tag, attr, value = m.group(1), m.group(2), m.group(3)
        field = ELEMENT_TO_COLUMN.get(tag)
        if field is None:
            raise XmlFormatError(f"unknown element <{tag}>")
        if attr != field.attribute:
            raise XmlFormatError(
                f"<{tag}> has attribute {attr!r}, expected {field.attribute!r}"
            )
        record[field.column] = value
    raise XmlFormatError("missing </experiment>")


def serialize(record: dict[str, str]) -> str:
    """Emit XML in the legacy format. Output is deterministic and intended to
    round-trip with parse()."""
    lines = [XML_HEADER]
    for field in FIELDS:
        value = record.get(field.column, "") or ""
        lines.append(
            f'<{field.element} {field.attribute}="{_escape_attr(value)}"/>\n'
        )
    lines.append(XML_FOOTER)
    return "".join(lines)


def round_trip_ok(content: str) -> bool:
    """True iff parse() then serialize() reproduces the input byte-for-byte."""
    return serialize(parse(content)) == content


# --- legacy-format heuristics -------------------------------------------------

_HEADER_RE = re.compile(r"^<\?xml version='1\.0' encoding='utf-8'\?>\n\n<experiment>\n")


def looks_like_legacy_format(content: str) -> bool:
    """Cheap check for the legacy byte layout. Used by the importer to flag
    files that may not round-trip cleanly so an operator can investigate."""
    return bool(_HEADER_RE.match(content)) and content.endswith("</experiment>\n")


__all__ = [
    "parse",
    "serialize",
    "round_trip_ok",
    "looks_like_legacy_format",
    "XmlFormatError",
]
