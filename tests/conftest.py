import json
from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_TRADE_DATE = date(2026, 9, 4)  # last_trade_time in every fixture is 2026-09-04


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def spy_payload() -> dict:
    return json.loads((FIXTURES / "SPY.json").read_text())


@pytest.fixture
def vix_payload() -> dict:
    return json.loads((FIXTURES / "_VIX.json").read_text())


@pytest.fixture
def mu_payload() -> dict:
    return json.loads((FIXTURES / "MU.json").read_text())
