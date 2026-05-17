"""Authoritative element/attribute schema for the legacy embryoDB XML format.

The Java EmbryoXML.java enumerates 16 fields in a fixed order, each serialized
as a self-closing element with one named attribute. This module is the single
source of truth so the importer, exporter, and any round-trip test agree.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class XmlField:
    column: str  # SQLAlchemy column name on Series
    element: str  # XML element tag
    attribute: str  # XML attribute name


# Order matters — the serializer emits fields in this exact sequence to match
# the legacy format byte-for-byte.
FIELDS: tuple[XmlField, ...] = (
    XmlField("series_name", "series", "name"),
    XmlField("date_acquired", "date", "date"),
    XmlField("person", "person", "name"),
    XmlField("strain_name", "strain", "name"),
    XmlField("treatments", "treatments", "desc"),
    XmlField("reporter_gene", "redsig", "value"),
    XmlField("image_loc", "imageloc", "loc"),
    XmlField("timepts", "timepts", "num"),
    XmlField("annot_loc", "annots", "loc"),
    XmlField("acetree_config", "acetree", "config"),
    XmlField("edited_by", "editedby", "name"),
    XmlField("edited_timepts", "editedtimepts", "num"),
    XmlField("edited_cells", "editedcells", "num"),
    XmlField("partial_editing_code", "checkedby", "name"),
    XmlField("comments", "comments", "text"),
    XmlField("status", "status", "case"),
)

ELEMENT_TO_COLUMN: dict[str, XmlField] = {f.element: f for f in FIELDS}
COLUMN_TO_FIELD: dict[str, XmlField] = {f.column: f for f in FIELDS}

XML_HEADER = "<?xml version='1.0' encoding='utf-8'?>\n\n<experiment>\n"
XML_FOOTER = "</experiment>\n"
