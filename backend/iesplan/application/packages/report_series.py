"""Stream sealed column-oriented series into a row-oriented Excel worksheet."""

from array import array
from dataclasses import dataclass
from itertools import islice
from tempfile import TemporaryFile

from iesplan.core.jsonstream import ArrayReader, ObjectFields, project_json
from iesplan.storage import get_object_content

PAGE_ROWS = 4096


@dataclass(frozen=True)
class Column:
    offset: int
    rows: int


def write_hourly_sheets(db, wb, refs) -> None:
    """Disk-backed transpose; RAM is one row page, not all values or Excel cells."""
    directory = wb.create_sheet("步序列目录")
    values_sheet = wb.create_sheet("步序列值")
    headings = ("series_id", "kind", "instance_id", "variable_id", "block", "variable", "unit", "rows")
    directory.append(headings)
    columns = {}
    with TemporaryFile() as spool:

        def column(items):
            offset = spool.tell()
            rows = 0
            while values := array("d", islice(items, PAGE_ROWS)):
                spool.write(values.tobytes())
                rows += len(values)
            return Column(offset, rows)

        for ref in refs:
            doc = project_json(
                get_object_content(db, int(ref["object_id"])),
                ObjectFields(
                    {
                        "data": ObjectFields(other=ArrayReader(column)),
                        "meta": True,
                    }
                ),
            )
            for series_id in sorted(doc["data"]):
                col = doc["data"][series_id]
                meta = doc["meta"]["series"][series_id]
                directory.append(tuple(col.rows if key == "rows" else meta.get(key) for key in headings))
                columns[series_id] = col
        ordered = sorted(columns)
        values_sheet.append(("step", *ordered))
        if not ordered:
            return
        for start in range(0, columns[ordered[0]].rows, PAGE_ROWS):
            count = min(PAGE_ROWS, columns[ordered[0]].rows - start)
            page = []
            for key in ordered:
                spool.seek(columns[key].offset + start * 8)
                values = array("d")
                values.frombytes(spool.read(count * 8))
                page.append(values)
            for index, values in enumerate(zip(*page, strict=True), start):
                values_sheet.append((index, *values))
