from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from demo_risk import DemoRiskPolicy


SHANGHAI = ZoneInfo("Asia/Shanghai")


def stamp(day, hour=0):
    return int(datetime(2026, 11, day, hour, tzinfo=SHANGHAI).timestamp() * 1000)


def sample(policy, day, equity, *, hour=0, flow=0, complete=True):
    return policy.observe_equity(
        timestamp_ms=stamp(day, hour), net_equity_usdt=equity,
        cumulative_external_net_flow_usdt=flow, data_complete=complete,
    )


def test_daily_net_equity_uses_beijing_boundary_and_external_flow_adjustment():
    policy = DemoRiskPolicy()
    sample(policy, 17, "1000")
    state = sample(policy, 17, "1120", hour=1, flow="100")

    assert state["daily_pnl_usdt"] == "20"
    assert state["daily_loss_usdt"] == "0"
    assert state["daily_boundary_timezone"] == "Asia/Shanghai"
    assert state["hard_failure"] is None


@pytest.mark.parametrize(
    ("loss", "mode"),
    [("20", "cooldown"), ("30", "stop_until_next_day"),
     ("40", "lock_until_reconciled"), ("50.01", "terminated")],
)
def test_daily_loss_levels_escalate_monotonically(loss, mode):
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    state = sample(policy, 16, str(1000 - float(loss)), hour=1)

    assert state["entries_mode"] == mode
    if loss == "50.01":
        assert state["hard_failure"] == "daily_loss_exceeded"


def test_daily_loss_uses_lowest_verified_equity_and_includes_mark_to_market_costs():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 16, "970", hour=1)
    state = sample(policy, 16, "990", hour=2)

    assert state["daily_loss_usdt"] == "30"
    assert state["entries_mode"] == "stop_until_next_day"


def test_three_losing_round_trips_start_one_hour_cooldown():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    state = None
    for pnl in ("-1", "-2", "-3"):
        state = policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt=pnl)

    assert state["consecutive_losses"] == 0
    assert state["cooldown_until_ms"] == stamp(16, 2)
    assert state["entries_mode"] == "cooldown"


def test_zero_result_does_not_clear_loss_streak_and_profit_does():
    policy = DemoRiskPolicy()
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1")
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="0")
    assert policy.status(stamp(16, 1))["consecutive_losses"] == 1
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="0.01")
    assert policy.status(stamp(16, 1))["consecutive_losses"] == 0


def test_daily_lock_persists_across_day_and_recovers_only_after_flat_reconciliation():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 16, "960", hour=1)
    next_day = sample(policy, 17, "960")

    assert next_day["entries_mode"] == "lock_until_reconciled"
    assert policy.recover_next_day(timestamp_ms=stamp(17), flat=False,
                                   no_open_orders=True, reconciled=True, healthy=True) is False
    assert policy.recover_next_day(timestamp_ms=stamp(17), flat=True,
                                   no_open_orders=False, reconciled=True, healthy=True) is False
    assert policy.recover_next_day(timestamp_ms=stamp(17), flat=True,
                                   no_open_orders=True, reconciled=True, healthy=True) is True
    assert policy.status(stamp(17))["entries_mode"] == "enabled"


def test_hard_drawdown_failure_precedes_insufficient_evidence():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 16, "849", hour=1)
    state = sample(policy, 16, "900", hour=2, complete=False)

    assert state["hard_failure"] == "drawdown_limit_reached"
    assert state["verdict"] == "failed"


def test_incomplete_account_snapshot_fails_closed_and_does_not_invent_pnl():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    state = sample(policy, 16, "999", hour=1, complete=False)

    assert state["daily_pnl_usdt"] is None
    assert state["entries_mode"] == "blocked_evidence_incomplete"
    assert policy.status(stamp(16, 2))["entries_mode"] == "blocked_evidence_incomplete"


def test_incomplete_flow_snapshot_does_not_invent_peak_or_hard_drawdown_failure():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    state = policy.observe_equity(
        timestamp_ms=stamp(16, 1), net_equity_usdt="800",
        cumulative_external_net_flow_usdt=None, data_complete=False,
    )

    assert state["entries_mode"] == "blocked_evidence_incomplete"
    assert state["stage_drawdown_usdt"] == "0"
    assert state["daily_pnl_usdt"] is None
    assert state["hard_failure"] is None


def test_event_journal_replay_restores_loss_streak_cooldown_and_lock():
    original = DemoRiskPolicy()
    sample(original, 16, "1000")
    original.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1")
    original.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1")
    original.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1")
    original.drain_events()

    recovered = DemoRiskPolicy.from_events(original.journal)
    assert recovered.status(stamp(16, 1))["cooldown_until_ms"] == stamp(16, 2)

    sample(original, 16, "960", hour=2)
    recovered = DemoRiskPolicy.from_events(original.journal)
    assert recovered.status(stamp(17))["entries_mode"] == "lock_until_reconciled"


def test_triggered_three_loss_group_is_consumed_before_the_fourth_loss():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    for index in range(3):
        policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1",
                                 trade_id=f"loss-{index}")
    fourth = policy.record_round_trip(timestamp_ms=stamp(16, 3), net_pnl_usdt="-1",
                                      trade_id="loss-4")
    assert fourth["consecutive_losses"] == 1
    assert fourth["cooldown_until_ms"] == stamp(16, 2)
    assert fourth["entries_mode"] == "enabled"


def test_first_midday_equity_does_not_masquerade_as_the_daily_opening():
    policy = DemoRiskPolicy()
    state = sample(policy, 16, "970", hour=12)
    assert state["entries_mode"] == "blocked_evidence_incomplete"
    assert state["daily_pnl_usdt"] is None
    assert state["daily_loss_usdt"] is None


def test_new_day_requires_its_own_verified_opening_and_equity_snapshot():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    state = policy.status(stamp(17))
    assert state["entries_mode"] == "blocked_evidence_incomplete"
    assert state["evidence_complete"] is False


def test_explicit_midnight_opening_makes_intraday_loss_verifiable():
    policy = DemoRiskPolicy()
    policy.record_day_opening(timestamp_ms=stamp(16), net_equity_usdt="1000",
                              cumulative_external_net_flow_usdt="0")
    state = sample(policy, 16, "970", hour=12)
    assert state["daily_loss_usdt"] == "30"
    assert state["entries_mode"] == "stop_until_next_day"
    with pytest.raises(ValueError, match="午夜"):
        policy.record_day_opening(timestamp_ms=stamp(17, 12), net_equity_usdt="970")


def test_same_event_id_with_conflicting_equity_is_rejected_without_mutation():
    policy = DemoRiskPolicy()
    policy.observe_equity(timestamp_ms=stamp(16), net_equity_usdt="1000", event_id="mark")
    original = policy.journal
    with pytest.raises(ValueError, match="冲突"):
        policy.observe_equity(timestamp_ms=stamp(16), net_equity_usdt="800", event_id="mark")
    assert policy.journal == original
    assert policy.status(stamp(16))["hard_failure"] is None
    assert DemoRiskPolicy.from_events(original + original).journal == original


def test_same_trade_id_with_conflicting_pnl_is_rejected():
    policy = DemoRiskPolicy()
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1", trade_id="trip")
    with pytest.raises(ValueError, match="冲突"):
        policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="1", trade_id="trip")
    assert policy.status(stamp(16, 1))["consecutive_losses"] == 1


def test_replay_cannot_forge_a_same_day_or_unproven_lock_recovery():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 16, "960", hour=1)
    assert policy.recover_next_day(timestamp_ms=stamp(16, 2), flat=True,
        no_open_orders=True, reconciled=True, healthy=True) is False
    forged = {"event_id": "forged", "event_type": "lock_recovered",
              "timestamp_ms": stamp(16, 2), "payload": {"lock_day": "2026-11-16"}}
    with pytest.raises(ValueError):
        DemoRiskPolicy.from_events(policy.journal + [forged])
    forged["timestamp_ms"] = stamp(17)
    with pytest.raises(ValueError):
        DemoRiskPolicy.from_events(policy.journal + [forged])


def test_same_millisecond_replay_keeps_original_profit_loss_order():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="1",
                             trade_id="win", event_id="z")
    policy.record_round_trip(timestamp_ms=stamp(16, 1), net_pnl_usdt="-1",
                             trade_id="loss", event_id="a")
    restored = DemoRiskPolicy.from_events(policy.journal)
    assert restored.status(stamp(16, 1))["consecutive_losses"] == 1


def test_old_complete_snapshot_cannot_clear_a_newer_missing_evidence_fact():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 17, "1000")
    sample(policy, 17, "990", hour=2, complete=False)
    state = sample(policy, 16, "1000", hour=1)
    assert policy.status(stamp(17, 2))["entries_mode"] == "blocked_evidence_incomplete"
    assert state["evidence_complete"] is True


def test_late_prior_day_loss_cannot_clear_the_current_daily_stop():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 17, "1000")
    sample(policy, 17, "970", hour=1)
    sample(policy, 16, "970", hour=1)
    assert policy.status(stamp(17, 2))["entries_mode"] == "stop_until_next_day"


def test_late_midnight_baseline_preserves_the_latest_intraday_equity():
    policy = DemoRiskPolicy()
    sample(policy, 16, "970", hour=12)
    policy.record_day_opening(timestamp_ms=stamp(16), net_equity_usdt="1000")
    state = policy.status(stamp(16, 12))
    assert state["daily_pnl_usdt"] == "-30"
    assert state["daily_loss_usdt"] == "30"
    assert state["entries_mode"] == "stop_until_next_day"


def test_late_historical_equity_does_not_create_a_backwards_stage_drawdown():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 17, "1200")
    sample(policy, 16, "1010", hour=1)
    state = policy.status(stamp(17))
    assert state["stage_drawdown_usdt"] == "0"
    assert state["hard_failure"] is None


def test_late_historical_peak_reveals_real_subsequent_stage_drawdown():
    policy = DemoRiskPolicy()
    sample(policy, 16, "1000")
    sample(policy, 17, "1000")
    sample(policy, 16, "1200", hour=1)
    state = policy.status(stamp(17))
    assert state["stage_drawdown_usdt"] == "200"
    assert state["hard_failure"] == "drawdown_limit_reached"
