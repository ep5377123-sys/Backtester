from datetime import time

import pytest

from guban.config import get_contract, get_session


def test_contract_specs():
    assert (get_contract("NQ").point_value, get_contract("NQ").tick_size) == (20.0, 0.25)
    assert get_contract("mnq").point_value == 2.0
    assert get_contract("ES").tick_value == 12.5
    assert get_contract("MES").tick_value == 1.25
    with pytest.raises(KeyError):
        get_contract("CL")


def test_pnl_points_times_point_value_times_contracts_minus_costs():
    nq, mnq = get_contract("NQ"), get_contract("MNQ")
    assert nq.pnl(15000, 15050, 1) == 1000.0  # "$1,000 = 50 NQ pts"
    assert mnq.pnl(15000, 15050, 5) == 500.0
    assert mnq.pnl(15000, 15050, 5, direction=-1) == -500.0
    assert nq.pnl(15000, 14990, 2, direction=-1, commission=2.25, slippage=5.0) == 400.0 - 2 * 7.25
    with pytest.raises(ValueError):
        nq.pnl(1, 2, 1, direction=0)


def test_round_to_tick():
    assert get_contract("NQ").round_to_tick(15000.13) == 15000.25
    assert get_contract("NQ").round_to_tick(15000.12) == 15000.0


def test_sessions():
    rth, eth = get_session("rth"), get_session("ETH")
    assert (rth.start, rth.end, rth.overnight, rth.length_min) == (time(9, 30), time(16, 0), False, 390)
    assert (eth.overnight, eth.length_min) == (True, 1380)


@pytest.mark.parametrize("field, bad", [("point_value", 0.0), ("point_value", -2.0), ("tick_size", float("nan")), ("tick_size", float("inf")), ("tick_size", True)])
def test_contract_spec_rejects_invalid_point_value_and_tick(field, bad):
    from guban.config import ContractSpec

    kw = dict(symbol="X", name="x", point_value=2.0, tick_size=0.25) | {field: bad}
    with pytest.raises(ValueError, match=field):
        ContractSpec(**kw)
