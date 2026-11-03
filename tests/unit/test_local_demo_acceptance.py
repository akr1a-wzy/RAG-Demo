import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "local_acceptance", ROOT / "scripts/accept_local_demo.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_malformed_wrong_company_and_silent_replay_fail_market_contract(runner):
    case = {"expected_mode": "market_data", "market_codes": ["600519"], "market_days": 20}
    for answer in [
        {"mode": "market_error"},
        {"mode": "market_data", "market": {}},
        {"mode": "market_data", "market": {"data_mode": "replay", "datasets": []}},
        {"mode": "market_data", "market": {"data_mode": "live", "datasets": [{}]}},
        {"mode": "market_data", "market": {"data_mode": "live", "datasets": [
            {"quote": {"code": "300750"}}]}},
    ]:
        assert not runner.market_check(case, answer)


def test_existing_once_state_cannot_be_reset(runner, tmp_path):
    path = tmp_path / "state.json"
    runner.reserve_attempt(path, {"attempt_count": 1})
    with pytest.raises(FileExistsError):
        runner.reserve_attempt(path, {"attempt_count": 0})
