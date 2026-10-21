from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import Any

from fastapi.responses import StreamingResponse


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def csv_response(rows: Iterable[dict[str, Any]], filename: str) -> StreamingResponse:
    rows = list(rows)
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(v) for k, v in row.items()})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})
