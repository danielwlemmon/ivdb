from datetime import date

from ivdb.occ import parse_occ


def test_parse_equity():
    c = parse_occ("SPY261130C00689000")
    assert c.root == "SPY"
    assert c.expiry == date(2026, 11, 30)
    assert c.right == "C"
    assert c.strike == 689.0


def test_parse_weekly_index_root_and_fractional_strike():
    c = parse_occ("SPXW270630P09600500")
    assert c.root == "SPXW"
    assert c.expiry == date(2027, 6, 30)
    assert c.right == "P"
    assert c.strike == 9600.5


def test_parse_padded_root_with_spaces():
    c = parse_occ("MU    260618C00620000")
    assert c.root == "MU"
    assert c.strike == 620.0


def test_invalid_returns_none():
    assert parse_occ("") is None
    assert parse_occ("SPY") is None
    assert parse_occ("SPY261399C00689000") is None  # month 13
