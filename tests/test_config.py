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


def test_every_contract_names_a_calendar_with_its_early_closes():
    from datetime import date

    from guban.config import _contracts, get_early_closes

    for spec in _contracts(None).values():
        closes = get_early_closes(spec.calendar)
        if spec.calendar == "cme_crypto":  # BTC trades through most US holidays since 2022
            assert closes[date(2024, 12, 24)] == time(13, 45) and date(2024, 7, 4) not in closes
            assert closes[date(2021, 4, 2)] == time(9, 15)
        else:
            assert closes[date(2024, 12, 24)] == time(13, 15) and closes[date(2024, 7, 4)] == time(13, 0)
            assert closes[date(2019, 11, 29)] == time(13, 15) and closes[date(2022, 6, 20)] == time(13, 0)
    assert {s.calendar for s in _contracts(None).values()} == {"cme_equity", "cme_crypto"}
    assert get_early_closes(None) == {}
    with pytest.raises(KeyError, match="calendar.toml"):
        get_early_closes("nyse")


def test_calendar_rejects_bad_entries(tmp_path):
    from guban.config import get_early_closes

    (tmp_path / "calendar.toml").write_text('[x.early_close]\n"2024-12-24" = "1:15 pm"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        get_early_closes("x", tmp_path)


def test_default_costs_per_contract():
    # PLAN.md "Phase 15 plan": commission RT, slippage RT = 1 tick (half a tick per side)
    costs = {s: (get_contract(s).commission, get_contract(s).slippage) for s in ("NQ", "MNQ", "ES", "MES", "BTC")}
    assert costs == {"NQ": (2.50, 5.00), "MNQ": (2.00, 0.50), "ES": (3.78, 12.50), "MES": (1.22, 1.25), "BTC": (16.04, 25.00)}
    for s in costs:
        assert get_contract(s).slippage == get_contract(s).tick_value


@pytest.mark.parametrize("field", ["commission", "slippage"])
def test_contract_spec_rejects_negative_default_costs(field):
    from guban.config import ContractSpec

    with pytest.raises(ValueError, match=field):
        ContractSpec(symbol="X", name="x", point_value=2.0, tick_size=0.25, **{field: -1.0})
