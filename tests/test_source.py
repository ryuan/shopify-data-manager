from app.source import CellState, parse_cell


def test_blank_is_unchanged_and_explicit_empty_is_clear():
    assert parse_cell("").state == CellState.UNCHANGED
    assert parse_cell("   ").state == CellState.UNCHANGED
    for value in ("null", "NULL", "[]", "[ ]", "{}", "{ }", '""'):
        assert parse_cell(value).state == CellState.CLEAR


def test_falsey_values_are_real_values():
    assert parse_cell("0").state == CellState.VALUE
    assert parse_cell("false").state == CellState.VALUE
