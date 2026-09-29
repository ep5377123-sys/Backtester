"""Parquet files written atomically, optionally stamped with a JSON fingerprint in the schema metadata.

A file is written to a temporary sibling, flushed to disk, then moved over the final path (os.replace), so an
interrupted write never leaves a partial file under the final name, and processes writing the same file at once
(optimizer workers building a missing cache) each replace it whole.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

KEY = b"guban.fingerprint"


def write_parquet(df: pd.DataFrame, path: str | Path, fingerprint: dict | None = None) -> Path:
    """Write `df` to `path` atomically; `fingerprint` (JSON-able) is stored in the file's schema metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df)
    if fingerprint is not None:
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), KEY: json.dumps(fingerprint, sort_keys=True).encode()})
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            pq.write_table(table, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def read_fingerprint(path: str | Path) -> dict | None:
    """The fingerprint stored by write_parquet, or None (no file, no fingerprint, or not a readable Parquet file)."""
    try:
        meta = pq.read_schema(path).metadata or {}
        raw = meta.get(KEY)
        return json.loads(raw) if raw else None
    except (OSError, ValueError, pa.ArrowException):
        return None
