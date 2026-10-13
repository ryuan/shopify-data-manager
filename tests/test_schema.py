import pytest

from app.source import CellState, ParsedCell
from app.shopify.schema import coerce_cell


def test_explicit_clear_uses_empty_list_for_list_field():
    ref = {"kind": "LIST", "name": None, "ofType": {"kind": "SCALAR", "name": "String", "ofType": None}}
    assert coerce_cell(ParsedCell(CellState.CLEAR), ref, "tags") == []


def test_explicit_clear_uses_null_for_nullable_scalar():
    ref = {"kind": "SCALAR", "name": "String", "ofType": None}
    assert coerce_cell(ParsedCell(CellState.CLEAR), ref, "vendor") is None


def test_explicit_clear_rejects_non_null_scalar():
    ref = {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "String", "ofType": None}}
    with pytest.raises(ValueError):
        coerce_cell(ParsedCell(CellState.CLEAR), ref, "required")


def test_unsigned64_schema_coercion_is_json_string():
    type_ref = {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "UnsignedInt64", "ofType": None}}
    for raw, expected in [(0, "0"), ("1", "1"), ("0002", "2"), (str(2**64 - 1), str(2**64 - 1))]:
        assert coerce_cell(ParsedCell(CellState.VALUE, raw), type_ref, "newPosition") == expected
    for invalid in ("-1", "1.5", "abc", str(2**64)):
        with pytest.raises(ValueError):
            coerce_cell(ParsedCell(CellState.VALUE, invalid), type_ref, "newPosition")
