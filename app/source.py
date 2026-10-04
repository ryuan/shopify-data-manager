from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

from app.utils import choose


class CellState(str, Enum):
    UNCHANGED = "unchanged"
    CLEAR = "clear"
    VALUE = "value"


@dataclass(frozen=True)
class ParsedCell:
    state: CellState
    value: Any = None


@dataclass(frozen=True)
class SourceRow:
    row_number: int
    key: str
    values: dict[str, Any]


@dataclass(frozen=True)
class SourceData:
    path: Path
    key_column: str
    columns: list[str]
    rows: list[SourceRow]

    @property
    def keys(self) -> list[str]:
        return [row.key for row in self.rows]


def parse_cell(value: Any) -> ParsedCell:
    if value is None:
        return ParsedCell(CellState.UNCHANGED)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return ParsedCell(CellState.UNCHANGED)
        if stripped.casefold() == "null":
            return ParsedCell(CellState.CLEAR)
        if stripped[:1] in {"[", "{", '"'}:
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = object()
            if parsed in ([], {}, ""):
                return ParsedCell(CellState.CLEAR)
        return ParsedCell(CellState.VALUE, stripped)
    if pd.isna(value):
        return ParsedCell(CellState.UNCHANGED)
    return ParsedCell(CellState.VALUE, value)


def _read_dataframe(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xlsx":
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported source file type: {path.suffix}")
    df.columns = [str(col).strip() for col in df.columns]
    if any(not col for col in df.columns):
        raise ValueError("Source contains a blank column header.")
    duplicates = sorted({col for col in df.columns if list(df.columns).count(col) > 1})
    if duplicates:
        raise ValueError(f"Source contains duplicate column headers: {duplicates}")
    return df


def load_source(source_dir: Path, expected_key: str) -> SourceData:
    files = sorted(
        [p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in {".xlsx", ".csv"}],
        key=lambda p: p.name.lower(),
    )
    if len(files) != 1:
        raise RuntimeError(
            f"Exactly one .xlsx or .csv source file must exist in 'source/'. Found {len(files)}."
        )
    path = files[0]
    df = _read_dataframe(path)
    if expected_key in df.columns:
        key_column = expected_key
    else:
        print(f"Column '{expected_key}' was not found. Select the source column to use as '{expected_key}'.")
        key_column = str(df.columns[choose("Select key column", list(df.columns))])

    rows: list[SourceRow] = []
    seen: dict[str, int] = {}
    for idx, record in df.iterrows():
        row_number = int(idx) + 2
        key_cell = parse_cell(record[key_column])
        other_has_value = any(
            parse_cell(value).state != CellState.UNCHANGED
            for col, value in record.items()
            if col != key_column
        )
        if key_cell.state == CellState.UNCHANGED:
            if other_has_value:
                raise ValueError(f"Source row {row_number} has data but no '{expected_key}' value.")
            continue
        if key_cell.state == CellState.CLEAR:
            raise ValueError(f"Source row {row_number} cannot clear its key column '{expected_key}'.")
        key = str(key_cell.value).strip()
        if key in seen:
            raise ValueError(
                f"Duplicate '{expected_key}' value '{key}' on source rows {seen[key]} and {row_number}."
            )
        seen[key] = row_number
        rows.append(
            SourceRow(
                row_number=row_number,
                key=key,
                values={str(col): record[col] for col in df.columns if col != key_column},
            )
        )
    if not rows:
        raise ValueError("Source contains no usable data rows.")
    return SourceData(
        path=path,
        key_column=key_column,
        columns=[str(col) for col in df.columns if col != key_column],
        rows=rows,
    )
