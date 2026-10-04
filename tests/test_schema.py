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
