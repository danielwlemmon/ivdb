import pytest

from ivdb.universe import ConfigError, load_tickers

GOOD = "symbol,cboe_symbol,kind,active\nSPY,SPY,etf,1\nMU,MU,equity,1\nSPX,_SPX,index,1\nOFF,OFF,equity,0\n"


def write(tmp_path, text):
    p = tmp_path / "tickers.csv"
    p.write_text(text)
    return p


def test_active_only_and_cboe_symbols(tmp_path):
    tickers = load_tickers(write(tmp_path, GOOD))
    assert [t.symbol for t in tickers] == ["SPY", "MU", "SPX"]  # OFF is inactive
    assert {t.symbol: t.cboe_symbol for t in tickers}["SPX"] == "_SPX"


def test_duplicate_symbol_is_a_hard_error(tmp_path):
    p = write(tmp_path, GOOD + "SPY,SPY,etf,1\n")
    with pytest.raises(ConfigError, match="duplicate symbol SPY"):
        load_tickers(p)
    # case and whitespace differences are still duplicates
    with pytest.raises(ConfigError, match="duplicate symbol MU"):
        load_tickers(write(tmp_path, GOOD + " mu ,MU,equity,1\n"))


def test_only_list_is_deduplicated_and_ordered(tmp_path):
    p = write(tmp_path, GOOD)
    tickers = load_tickers(p, ["SPY", "mu", "SPY", " spy ", ""])
    assert [t.symbol for t in tickers] == ["SPY", "MU"]
    # an inactive symbol can still be requested explicitly
    assert [t.symbol for t in load_tickers(p, ["OFF"])] == ["OFF"]
    # a symbol absent from the file is fetched ad hoc, using itself as the CBOE symbol
    ad_hoc = load_tickers(p, ["NEWCO"])[0]
    assert ad_hoc.symbol == "NEWCO" and ad_hoc.cboe_symbol == "NEWCO"


def test_bad_kind_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="kind must be one of"):
        load_tickers(write(tmp_path, "symbol,cboe_symbol,kind,active\nSPY,SPY,stock,1\n"))
    with pytest.raises(ConfigError, match="header must include"):
        load_tickers(write(tmp_path, "symbol,kind\nSPY,etf\n"))
    with pytest.raises(ConfigError, match="not found"):
        load_tickers(tmp_path / "nope.csv")
