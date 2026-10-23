from decimal import Decimal, localcontext
from fractions import Fraction
from dataclasses import replace

import pytest

from core.execution.account_risk_preflight import (
    ExecutionEvidence,
    GapObservation,
    GapP99,
    QuantileResult,
    RiskConfig,
    ScenarioEvidence,
    TierBand,
    TierProvenance,
    TierSchedule,
    build_envelopes,
    canonical_sha256,
    evaluate_preflight,
    gap_p99_from_observations,
    nearest_rank_p99,
    tier_schedule_snapshot_hash,
    validate_risk_config,
    ACCOUNT_LOSS_P99_SCENARIOS,
    REQUIRED_EXECUTION_EVIDENCE,
    REQUIRED_SCENARIO_EVIDENCE,
    _scenario_expected_stress,
)


AS_OF = "2026-10-23T00:00:00Z"
ACCOUNT_SCOPE = "synthetic-account-1"


def _config(
    *,
    caps=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("100"))),
    leverages=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("75"))),
    n_max=Decimal("200"),
    required_windows=100,
):
    schedules = tuple(
        _schedule(symbol, max_leverage=str(leverage))
        for symbol, leverage in leverages
    )
    return RiskConfig(
        initial_equity_usdt=Decimal("10000"),
        max_leverage=Decimal("100"),
        margin_mode="isolated",
        n_max_usdt=n_max,
        per_symbol_caps_usdt=tuple(caps),
        per_symbol_leverages=tuple(leverages),
        account_p99_limit_pct=Decimal("0.05"),
        required_n_gap=100,
        required_account_windows=required_windows,
        account_scope=ACCOUNT_SCOPE,
        account_loss_authority_scenario_id="A_long",
        tier_snapshot_hash=tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, schedules),
    )


def _schedule(symbol, *, max_leverage="100", fetched=AS_OF, kind="account_api_snapshot", scope=ACCOUNT_SCOPE):
    return TierSchedule(
        symbol=symbol,
        bands=(
            TierBand(
                initial_leverage=Decimal(max_leverage),
                notional_floor_usdt=Decimal("0"),
                notional_cap_usdt=Decimal("1000000"),
                maintenance_margin_ratio=Decimal("0.004"),
                cumulative_maintenance_usdt=Decimal("0"),
            ),
        ),
        source="exchange-api",
        effective_at_utc="2026-10-01T00:00:00Z",
        fetched_at_utc=fetched,
        account_scope=scope,
        evidence_sha256="a" * 64,
        verified=True,
        snapshot_kind=kind,
        notional_cap_inclusive=True,
        provenance=TierProvenance(
            endpoint="/fapi/v1/leverageBracket",
            http_method="GET",
            security_type="USER_DATA",
            request_id=f"req-{symbol}",
            signed_at_utc="2026-10-01T00:00:00Z",
            response_sha256="b" * 64,
            environment="testnet",
        ),
    )


def _p99(symbol, side, rate="0.001"):
    qr = QuantileResult("pass", Decimal(rate), Decimal(rate), 100, 100, rank=99)
    return GapP99(symbol, side, qr, qr)


def _gaps(symbols=("BTCUSDT", "ETHUSDT"), rates=None):
    rates = rates or {}
    return tuple(
        _p99(symbol, side, rates.get((symbol, side), "0.001"))
        for symbol in symbols
        for side in ("long", "short")
    )


def _hash(value):
    return canonical_sha256(value)


def _scenario(
    config,
    scenario_id,
    *,
    losses=None,
    deficits=None,
    config_hash=None,
    model_sha256=None,
    tier_snapshot_hash=None,
    assumption_hashes=None,
    status="pass",
    window_data_sha256="c" * 64,
):
    losses = tuple(losses if losses is not None else [Decimal("1")] * config.required_account_windows)
    deficits = tuple(deficits if deficits is not None else [Decimal("0")] * len(losses))
    path, slippage, delay_ms, ticks = _scenario_expected_stress(scenario_id)
    assumptions = assumption_hashes or tuple((name, "b" * 64) for name in REQUIRED_EXECUTION_EVIDENCE)
    model_hash = model_sha256 or "b" * 64
    tier_hash = tier_snapshot_hash or config.tier_snapshot_hash
    payload = {
        "scenario_id": scenario_id,
        "config_hash": config_hash or config.config_hash,
        "model_sha256": model_hash,
        "tier_snapshot_hash": tier_hash,
        "assumption_hashes": assumptions,
        "window_data_sha256": window_data_sha256,
        "account_window_losses_usdt": losses,
        "uncovered_deficits_usdt": deficits,
        "status": status,
        "execution_path": path,
        "slippage_multiple": slippage,
        "delay_ms": delay_ms,
        "liquidation_slippage_ticks": ticks,
    }
    return ScenarioEvidence(
        scenario_id=scenario_id,
        config_hash=config_hash or config.config_hash,
        model_sha256=model_hash,
        tier_snapshot_hash=tier_hash,
        assumption_hashes=assumptions,
        evidence_sha256=_hash(payload),
        window_data_sha256=window_data_sha256,
        account_window_losses_usdt=losses,
        uncovered_deficits_usdt=deficits,
        status=status,
        execution_path=path,
        slippage_multiple=slippage,
        delay_ms=delay_ms,
        liquidation_slippage_ticks=ticks,
    )


def _execution(config, *, scenarios=None, losses=None, deficits=None, tier_snapshot_hash=None):
    digest = "b" * 64
    assumptions = tuple((name, digest) for name in REQUIRED_EXECUTION_EVIDENCE)
    losses = tuple(losses if losses is not None else [Decimal("1")] * config.required_account_windows)
    deficits = tuple(deficits if deficits is not None else [Decimal("0")] * len(losses))
    return ExecutionEvidence(
        model_sha256=digest,
        window_data_sha256="c" * 64,
        assumption_hashes=assumptions,
        account_window_losses_usdt=losses,
        uncovered_deficits_usdt=deficits,
        tier_snapshot_hash=tier_snapshot_hash or config.tier_snapshot_hash,
        scenario_evidence=tuple(scenarios if scenarios is not None else (_scenario(config, item) for item in REQUIRED_SCENARIO_EVIDENCE)),
    )


class TestCanonicalAndQuantiles:
    def test_decimal_canonical_hash_is_stable_and_float_is_rejected(self):
        assert canonical_sha256({"x": Decimal("1.00")}) == canonical_sha256({"x": Decimal("1")})
        with pytest.raises(TypeError):
            canonical_sha256({"x": 1.0})

    def test_nearest_rank_uses_ceil_rank(self):
        result = nearest_rank_p99(tuple(Decimal(i) for i in range(1, 101)), 100)
        assert result.status == "pass"
        assert result.rank == 99
        assert result.p99 == Decimal("99")

    def test_nearest_rank_and_gap_require_samples_and_adjacency(self):
        assert nearest_rank_p99((Decimal("0.1"),), 100).status == "not_estimable"
        observations = (
            GapObservation(Decimal("-0.01"), 0, 900_000),
            GapObservation(Decimal("-0.02"), 900_000, 1_800_000, has_valid_next_bar=False),
            GapObservation(Decimal("-0.03"), 1_800_000, 3_600_000),
        )
        result = gap_p99_from_observations(observations, "long", required_sample_size=2)
        assert result.status == "not_estimable"
        assert result.sample_size == 1


class TestConfigAndEnvelopes:
    def test_mixed_actual_leverages_are_valid_under_user_cap(self):
        config = _config(
            caps=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("100")), ("TRXUSDT", Decimal("100"))),
            leverages=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("75")), ("TRXUSDT", Decimal("25"))),
            n_max=Decimal("300"),
        )
        validation = validate_risk_config(config, [_schedule("BTCUSDT", max_leverage="100"), _schedule("ETHUSDT", max_leverage="75"), _schedule("TRXUSDT", max_leverage="25")], as_of_utc=AS_OF)
        assert validation.status == "pass"

    @pytest.mark.parametrize("precision", [28, 80])
    @pytest.mark.parametrize("p99_text,expected,delta", [
        ("0.333333333333333333333333333398", "pass", "-1e-30"),
        ("0.333333333333333333333333333399", "pass", "0"),
        ("0.333333333333333333333333333400", "fail", "1e-30"),
    ])
    def test_account_p99_limit_comparison_is_context_independent(
        self, precision, p99_text, expected, delta,
    ):
        limit_pct = Decimal("0.333333333333333333333333333399")
        p99_loss = Decimal(p99_text)
        exact_limit = Fraction(1) * Fraction(limit_pct)
        assert Fraction(p99_loss) - exact_limit == Fraction(Decimal(delta))

        config = _config(
            caps=(("BTCUSDT", Decimal("1")), ("ETHUSDT", Decimal("1"))),
            leverages=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("75"))),
            n_max=Decimal("2"),
        )
        config = replace(
            config,
            initial_equity_usdt=Decimal("1"),
            account_p99_limit_pct=limit_pct,
        )
        losses = (p99_loss,) * config.required_account_windows
        scenarios = tuple(
            _scenario(config, scenario_id, losses=losses)
            for scenario_id in REQUIRED_SCENARIO_EVIDENCE
        )
        execution = _execution(config, losses=losses, scenarios=scenarios)

        with localcontext() as context:
            context.prec = precision
            report = evaluate_preflight(
                config,
                (_schedule("BTCUSDT"), _schedule("ETHUSDT", max_leverage="75")),
                _gaps(),
                execution,
                as_of_utc=AS_OF,
            )
        assert report.status == expected
        assert report.missing_assumptions == ()
        assert report.account_window_p99_loss_usdt == p99_loss
        top_level_reason = "single-window account-loss P99 exceeds the frozen equity percentage limit"
        scenario_reasons = {
            f"scenario {scenario_id} account-loss P99 exceeds the frozen equity limit"
            for scenario_id in ACCOUNT_LOSS_P99_SCENARIOS
        }
        if expected == "fail":
            assert top_level_reason in report.reasons
            assert set(report.reasons) == scenario_reasons | {top_level_reason}
        else:
            assert top_level_reason not in report.reasons
            assert not scenario_reasons.intersection(report.reasons)
            assert report.reasons == ()

    @pytest.mark.parametrize(
        "schedule_kwargs",
        [
            {"fetched": "2026-10-20T00:00:00Z"},
            {"kind": "static_diagnostic"},
            {"scope": "other-account"},
        ],
    )
    def test_stale_static_or_scope_mismatch_is_not_estimable(self, schedule_kwargs):
        config = _config()
        validation = validate_risk_config(config, [_schedule("BTCUSDT", **schedule_kwargs), _schedule("ETHUSDT", **schedule_kwargs)], as_of_utc=AS_OF)
        assert validation.status == "not_estimable"

    def test_missing_as_of_or_bracket_is_not_estimable(self):
        config = _config()
        validation = validate_risk_config(config, [_schedule("BTCUSDT"), _schedule("ETHUSDT")])
        assert validation.status == "not_estimable"
        assert "UTC as-of" in " ".join(validation.missing_assumptions)

    def test_leverage_and_cap_conflicts_are_hard_failures(self):
        config = _config(leverages=(("BTCUSDT", Decimal("101")), ("ETHUSDT", Decimal("75"))))
        validation = validate_risk_config(config, [_schedule("BTCUSDT"), _schedule("ETHUSDT")], as_of_utc=AS_OF)
        assert validation.status == "fail"

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("required_n_gap", 99, "required_n_gap"),
            ("required_account_windows", 99, "required_account_windows"),
        ],
    )
    def test_required_sample_hard_floors_cannot_be_lowered(self, field, value, reason):
        config = replace(_config(), **{field: value})
        validation = validate_risk_config(
            config,
            [_schedule("BTCUSDT"), _schedule("ETHUSDT", max_leverage="75")],
            as_of_utc=AS_OF,
        )
        assert validation.status == "fail"
        assert any(reason in item for item in validation.reasons)

    def test_reversed_immutable_tier_bands_fail_closed_even_when_hash_matches(self):
        ascending = _schedule("BTCUSDT")
        two_bands = replace(
            ascending,
            bands=(
                TierBand(
                    initial_leverage=Decimal("100"),
                    notional_floor_usdt=Decimal("0"),
                    notional_cap_usdt=Decimal("100"),
                    maintenance_margin_ratio=Decimal("0.004"),
                    cumulative_maintenance_usdt=Decimal("0"),
                ),
                TierBand(
                    initial_leverage=Decimal("100"),
                    notional_floor_usdt=Decimal("100"),
                    notional_cap_usdt=Decimal("1000000"),
                    maintenance_margin_ratio=Decimal("0.01"),
                    cumulative_maintenance_usdt=Decimal("0.6"),
                ),
            ),
        )
        reversed_bands = replace(two_bands, bands=tuple(reversed(two_bands.bands)))
        eth = _schedule("ETHUSDT", max_leverage="75")
        config = replace(
            _config(),
            tier_snapshot_hash=tier_schedule_snapshot_hash(
                ACCOUNT_SCOPE,
                AS_OF,
                (reversed_bands, eth),
            ),
        )
        validation = validate_risk_config(
            config,
            (reversed_bands, eth),
            as_of_utc=AS_OF,
        )
        assert validation.status == "fail"
        assert any("ascending notional-floor order" in item for item in validation.reasons)

    def test_exclusive_cap_boundary_uses_the_next_tier_leverage(self):
        btc = replace(
            _schedule("BTCUSDT"),
            notional_cap_inclusive=False,
            bands=(
                TierBand(Decimal("100"), Decimal("0"), Decimal("100"), Decimal("0.004"), Decimal("0")),
                TierBand(Decimal("75"), Decimal("100"), Decimal("1000000"), Decimal("0.01"), Decimal("0.6")),
            ),
        )
        eth = _schedule("ETHUSDT", max_leverage="75")
        config = replace(
            _config(),
            tier_snapshot_hash=tier_schedule_snapshot_hash(ACCOUNT_SCOPE, AS_OF, (btc, eth)),
        )

        validation = validate_risk_config(config, (btc, eth), as_of_utc=AS_OF)
        assert validation.status == "fail"
        assert any("BTCUSDT actual leverage 100x exceeds tier maximum 75x" in item
                   for item in validation.reasons)

        within_tier = replace(
            config,
            per_symbol_leverages=(("BTCUSDT", Decimal("75")), ("ETHUSDT", Decimal("75"))),
        )
        assert validate_risk_config(within_tier, (btc, eth), as_of_utc=AS_OF).status == "pass"

    def test_frozen_tier_snapshot_hash_must_bind_complete_schedule_set(self):
        config = _config()
        schedules = (_schedule("BTCUSDT"), _schedule("ETHUSDT", max_leverage="75"))
        wrong_config = replace(config, tier_snapshot_hash="d" * 64)
        validation = validate_risk_config(wrong_config, schedules, as_of_utc=AS_OF)
        assert validation.status == "not_estimable"
        assert any("do not match" in item for item in validation.missing_assumptions)

    def test_binance_cumulative_maintenance_amount_is_subtracted(self):
        config = _config(
            caps=(("BTCUSDT", Decimal("200")), ("ETHUSDT", Decimal("100"))),
            # Keep this fixture within the user's confirmed 100x cap while
            # still exercising the subtraction of Binance's cumulative
            # maintenance amount.
            leverages=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("75"))),
            n_max=Decimal("300"),
        )
        btc = TierSchedule(
            symbol="BTCUSDT",
            bands=(
                TierBand(Decimal("100"), Decimal("0"), Decimal("100"), Decimal("0.004"), Decimal("0")),
                # At the 100 USDT boundary: 100*.01 - .6 == .4,
                # continuous with the preceding bracket. At cap 200 the
                # maintenance requirement is 1.4, below 200/100 = 2.0.
                TierBand(Decimal("100"), Decimal("100"), Decimal("1000"), Decimal("0.01"), Decimal("0.6")),
            ),
            source="exchange-api",
            effective_at_utc="2026-10-01T00:00:00Z",
            fetched_at_utc=AS_OF,
            account_scope=ACCOUNT_SCOPE,
            evidence_sha256="a" * 64,
            verified=True,
            snapshot_kind="account_api_snapshot",
            notional_cap_inclusive=True,
            provenance=TierProvenance(
                endpoint="/fapi/v1/leverageBracket",
                http_method="GET",
                security_type="USER_DATA",
                request_id="req-BTCUSDT",
                signed_at_utc="2026-10-01T00:00:00Z",
                response_sha256="b" * 64,
                environment="testnet",
            ),
        )
        eth = _schedule("ETHUSDT", max_leverage="75")
        # This fixture intentionally replaces BTC's default one-band schedule
        # with a two-band maintenance curve, so the frozen aggregate snapshot
        # hash must describe the exact schedules supplied to validation.
        config = replace(
            config,
            tier_snapshot_hash=tier_schedule_snapshot_hash(
                ACCOUNT_SCOPE,
                AS_OF,
                (btc, eth),
            ),
        )
        validation = validate_risk_config(config, [btc, eth], as_of_utc=AS_OF)
        assert validation.status == "pass"

    def test_discontinuous_multi_tier_maintenance_is_not_accepted(self):
        config = _config(
            caps=(("BTCUSDT", Decimal("200")), ("ETHUSDT", Decimal("100"))),
            leverages=(("BTCUSDT", Decimal("125")), ("ETHUSDT", Decimal("75"))),
            n_max=Decimal("300"),
        )
        schedule = _schedule("BTCUSDT", max_leverage="125")
        bad = replace(
            schedule,
            bands=(
                TierBand(Decimal("125"), Decimal("0"), Decimal("100"), Decimal("0.004"), Decimal("0")),
                TierBand(Decimal("125"), Decimal("100"), Decimal("1000"), Decimal("0.01"), Decimal("0.5")),
            ),
        )
        validation = validate_risk_config(config, [bad, _schedule("ETHUSDT", max_leverage="75")], as_of_utc=AS_OF)
        assert validation.status == "fail"
        assert "discontinuous" in " ".join(validation.reasons)
        config = _config(n_max=Decimal("250"), caps=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("100"))))
        validation = validate_risk_config(config, [_schedule("BTCUSDT"), _schedule("ETHUSDT")], as_of_utc=AS_OF)
        assert validation.status == "fail"

    def test_waterfill_and_c_concentrated_tie_are_deterministic(self):
        config = _config(
            caps=(("BTCUSDT", Decimal("60")), ("ETHUSDT", Decimal("100")), ("TRXUSDT", Decimal("200"))),
            leverages=(("BTCUSDT", Decimal("100")), ("ETHUSDT", Decimal("75")), ("TRXUSDT", Decimal("25"))),
            n_max=Decimal("200"),
        )
        rates = {("BTCUSDT", "long"): "0.020", ("BTCUSDT", "short"): "0.010", ("ETHUSDT", "long"): "0.020", ("ETHUSDT", "short"): "0.020", ("TRXUSDT", "long"): "0.001", ("TRXUSDT", "short"): "0.001"}
        envelopes = build_envelopes(config, _gaps(("BTCUSDT", "ETHUSDT", "TRXUSDT"), rates))
        assert [item.notional_usdt for item in envelopes[0].exposures] == [Decimal("60"), Decimal("70"), Decimal("70")]
        c = envelopes[-1]
        assert [(item.symbol, item.side, item.notional_usdt) for item in c.exposures] == [("BTCUSDT", "long", Decimal("60")), ("ETHUSDT", "long", Decimal("100")), ("TRXUSDT", "long", Decimal("40"))]
        assert envelopes == build_envelopes(config, _gaps(("BTCUSDT", "ETHUSDT", "TRXUSDT"), rates))

    def test_b_not_estimable_if_each_side_cannot_reach_half(self):
        config = _config(caps=(("BTCUSDT", Decimal("150")), ("ETHUSDT", Decimal("50"))), n_max=Decimal("200"))
        envelopes = build_envelopes(config, _gaps())
        assert envelopes[2].envelope_id == "B"
        assert envelopes[2].status == "not_estimable"

    def test_hand_built_pass_p99_with_insufficient_sample_cannot_build_envelopes(self):
        config = _config()
        bad = _gaps()
        bad = tuple(
            _p99(item.symbol, item.side, "0.001") if item.symbol != "BTCUSDT" else GapP99(
                item.symbol,
                item.side,
                QuantileResult("pass", Decimal("0.001"), Decimal("0.001"), 1, 100, rank=1),
                QuantileResult("pass", Decimal("0.001"), Decimal("0.001"), 1, 100, rank=1),
            )
            for item in bad
        )
        assert build_envelopes(config, bad)[0].status == "not_estimable"


class TestEvidenceGate:
    def _valid_inputs(self):
        config = _config()
        tiers = [_schedule("BTCUSDT"), _schedule("ETHUSDT", max_leverage="75")]
        return config, tiers

    def test_gap_only_or_partial_scenarios_never_pass(self):
        config, tiers = self._valid_inputs()
        report = evaluate_preflight(config, tiers, _gaps(), None, as_of_utc=AS_OF)
        assert report.status == "not_estimable"
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=()), as_of_utc=AS_OF)
        assert report.status == "not_estimable"

    @pytest.mark.parametrize("malformed_config", [None, object()])
    def test_malformed_config_fails_closed_without_throwing(self, malformed_config):
        report = evaluate_preflight(
            malformed_config,
            [],
            _gaps(),
            None,
            as_of_utc=AS_OF,
        )
        assert report.status == "not_estimable"
        assert "typed RiskConfig" in " ".join(report.missing_assumptions)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("initial_equity_usdt", None),
            ("initial_equity_usdt", "bad"),
            ("account_p99_limit_pct", None),
            ("account_loss_authority_scenario_id", []),
            ("required_account_windows", None),
        ],
    )
    def test_malformed_risk_config_fields_fail_closed_without_throwing(self, field, value):
        valid_config, tiers = self._valid_inputs()
        config = replace(valid_config, **{field: value})
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(valid_config),
            as_of_utc=AS_OF,
        )
        assert report.status == "fail"
        assert report.reasons or report.missing_assumptions

    def test_complete_scenario_evidence_can_pass(self):
        config, tiers = self._valid_inputs()
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config), as_of_utc=AS_OF)
        assert report.status == "pass"

    def test_exact_five_percent_loss_boundary_can_pass(self):
        config, tiers = self._valid_inputs()
        loss_limit = Decimal("500")
        assert Fraction(loss_limit) == (
            Fraction(config.initial_equity_usdt) * Fraction(config.account_p99_limit_pct)
        )
        losses = (loss_limit,) * config.required_account_windows
        scenarios = tuple(
            _scenario(config, scenario_id, losses=losses)
            for scenario_id in REQUIRED_SCENARIO_EVIDENCE
        )
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, scenarios=scenarios, losses=losses),
            as_of_utc=AS_OF,
        )
        assert report.status == "pass"
        assert report.account_window_p99_loss_usdt == Decimal("500")

    def test_negative_or_mismatched_evidence_cannot_pass(self):
        config, tiers = self._valid_inputs()
        evidence = _execution(config, losses=[Decimal("1")] * 100, deficits=[Decimal("0")] * 99 + [Decimal("1")])
        report = evaluate_preflight(config, tiers, _gaps(), evidence, as_of_utc=AS_OF)
        assert report.status == "fail"
        bad = list(_execution(config).scenario_evidence)
        bad[0] = _scenario(config, bad[0].scenario_id, losses=[Decimal("-1")] * 100)
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=bad), as_of_utc=AS_OF)
        assert report.status == "fail"
    def test_scenario_config_hash_mismatch_and_p99_over_limit_fail(self):
        config, tiers = self._valid_inputs()
        bad_hash = list(_execution(config).scenario_evidence)
        bad_hash[0] = _scenario(config, bad_hash[0].scenario_id, config_hash="d" * 64)
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=bad_hash), as_of_utc=AS_OF)
        assert report.status == "fail"
        too_large = list(_execution(config).scenario_evidence)
        too_large[0] = _scenario(config, too_large[0].scenario_id, losses=[Decimal("600")] * 100)
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=too_large), as_of_utc=AS_OF)
        assert report.status == "fail"

    @pytest.mark.parametrize("field", ["model", "tier", "assumptions"])
    def test_scenario_execution_binding_mismatches_fail(self, field):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        original = scenarios[0]
        kwargs = {}
        if field == "model":
            kwargs["model_sha256"] = "d" * 64
        elif field == "tier":
            kwargs["tier_snapshot_hash"] = "d" * 64
        else:
            changed = list(original.assumption_hashes)
            changed[0] = (changed[0][0], "d" * 64)
            kwargs["assumption_hashes"] = tuple(changed)
        scenarios[0] = _scenario(config, original.scenario_id, **kwargs)
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, scenarios=scenarios),
            as_of_utc=AS_OF,
        )
        assert report.status == "fail"
        if field == "assumptions":
            assert any("different execution assumptions" in reason for reason in report.reasons)
        else:
            assert any("bound to a different" in reason for reason in report.reasons)

    def test_execution_tier_snapshot_mismatch_fails_closed(self):
        config, tiers = self._valid_inputs()
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, tier_snapshot_hash="d" * 64),
            as_of_utc=AS_OF,
        )
        assert report.status == "fail"
        assert any("tier snapshot" in reason for reason in report.reasons)

    def test_scenario_payload_hash_and_status_are_not_trusted(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        original = scenarios[0]
        scenarios[0] = replace(original, status="fail")
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=scenarios), as_of_utc=AS_OF)
        assert report.status == "fail"
        scenarios[0] = replace(original, execution_path="mark_only")
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=scenarios), as_of_utc=AS_OF)
        assert report.status == "fail"

    def test_unregistered_scenario_suffix_cannot_enter_pressure_set(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        scenarios.append(
            replace(
                _scenario(config, "C_liquidation_1tick"),
                scenario_id="unregistered_liquidation_1tick",
            )
        )
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, scenarios=scenarios),
            as_of_utc=AS_OF,
        )
        assert report.status == "fail"
        assert any("not in the frozen pressure set" in reason for reason in report.reasons)

    def test_scenario_window_hash_must_match_execution_window(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        original = scenarios[0]
        scenarios[0] = _scenario(
            config,
            original.scenario_id,
            window_data_sha256="d" * 64,
        )
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, scenarios=scenarios),
            as_of_utc=AS_OF,
        )
        assert report.status == "not_estimable"

    def test_malformed_evidence_containers_fail_closed_without_throwing(self):
        config, tiers = self._valid_inputs()
        execution = _execution(config)
        malformed_scenarios = replace(execution, scenario_evidence=[*execution.scenario_evidence])
        report = evaluate_preflight(config, tiers, _gaps(), malformed_scenarios, as_of_utc=AS_OF)
        assert report.status == "not_estimable"
        malformed_scenarios = replace(execution, scenario_evidence=None)
        report = evaluate_preflight(config, tiers, _gaps(), malformed_scenarios, as_of_utc=AS_OF)
        assert report.status == "not_estimable"
        malformed_assumptions = replace(execution, assumption_hashes=None)
        report = evaluate_preflight(config, tiers, _gaps(), malformed_assumptions, as_of_utc=AS_OF)
        assert report.status == "not_estimable"

    @pytest.mark.parametrize("malformed_tiers", [None, [None], ["bad"]])
    def test_malformed_tier_schedules_fail_closed_without_throwing(self, malformed_tiers):
        config, tiers = self._valid_inputs()
        report = evaluate_preflight(config, malformed_tiers, _gaps(), _execution(config), as_of_utc=AS_OF)
        assert report.status == "not_estimable"
        assert report.tier_evidence_hashes == ()

    @pytest.mark.parametrize("boundary", [None, "false", 1])
    def test_malformed_notional_cap_boundary_is_not_estimable(self, boundary):
        config, tiers = self._valid_inputs()
        malformed = replace(tiers[0], notional_cap_inclusive=boundary)
        validation = validate_risk_config(
            config,
            (malformed, tiers[1]),
            as_of_utc=AS_OF,
        )
        assert validation.status == "not_estimable"
        assert any("notional cap boundary" in item for item in validation.missing_assumptions)

    def test_mutable_tier_bands_fail_closed(self):
        config, tiers = self._valid_inputs()
        mutable_schedule = replace(tiers[0], bands=list(tiers[0].bands))
        report = evaluate_preflight(
            config,
            (mutable_schedule, *tiers[1:]),
            _gaps(),
            _execution(config),
            as_of_utc=AS_OF,
        )
        assert report.status == "not_estimable"
        assert any("complete typed maintenance-margin bands" in item
                   for item in report.missing_assumptions)

    def test_gap_quantile_cannot_override_config_required_sample(self):
        config, tiers = self._valid_inputs()
        tiny = QuantileResult("pass", Decimal("0.001"), Decimal("0.001"), 1, 1, rank=1)
        gap_p99s = tuple(GapP99(item.symbol, item.side, tiny, tiny) for item in _gaps())
        report = evaluate_preflight(config, tiers, gap_p99s, _execution(config), as_of_utc=AS_OF)
        assert report.status == "not_estimable"

    def test_non_account_pressure_p99_is_bounded_but_not_subject_to_5_percent_gate(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        synthetic = next(i for i, item in enumerate(scenarios) if item.scenario_id == "synthetic_max_envelope")
        scenarios[synthetic] = _scenario(config, "synthetic_max_envelope", losses=[Decimal("600")] * 100)
        report = evaluate_preflight(config, tiers, _gaps(), _execution(config, scenarios=scenarios), as_of_utc=AS_OF)
        assert report.status == "pass"

    def test_liquidation_pressure_p99_is_subject_to_five_percent_gate(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        liquidation = next(
            i
            for i, item in enumerate(scenarios)
            if item.scenario_id == "C_liquidation_1tick"
        )
        scenarios[liquidation] = _scenario(
            config,
            "C_liquidation_1tick",
            losses=[Decimal("600")] * 100,
        )
        report = evaluate_preflight(
            config,
            tiers,
            _gaps(),
            _execution(config, scenarios=scenarios),
            as_of_utc=AS_OF,
        )
        assert report.status == "fail"
        assert any("C_liquidation_1tick" in reason for reason in report.reasons)

    def test_future_snapshot_is_a_hard_failure(self):
        config = _config()
        validation = validate_risk_config(
            config,
            [_schedule("BTCUSDT", fetched="2026-10-24T00:00:00Z"), _schedule("ETHUSDT", fetched="2026-10-24T00:00:00Z")],
            as_of_utc=AS_OF,
        )
        assert validation.status == "fail"

    def test_api_provenance_is_required_and_signed_time_cannot_follow_fetch(self):
        config = _config()
        missing = replace(_schedule("BTCUSDT"), provenance=None)
        validation = validate_risk_config(
            config,
            [missing, _schedule("ETHUSDT")],
            as_of_utc=AS_OF,
        )
        assert validation.status == "not_estimable"
        invalid = replace(
            _schedule("BTCUSDT"),
            provenance=TierProvenance(
                endpoint="/fapi/v1/leverageBracket",
                http_method="GET",
                security_type="USER_DATA",
                request_id="req-BTCUSDT",
                signed_at_utc="2026-10-24T00:00:00Z",
                response_sha256="b" * 64,
                environment="testnet",
            ),
        )
        validation = validate_risk_config(
            config,
            [invalid, _schedule("ETHUSDT")],
            as_of_utc=AS_OF,
        )
        assert validation.status == "fail"

    def test_authority_is_deterministic_worst_p99_and_scenarios_remain_distinct(self):
        config = replace(_config(), account_loss_authority_scenario_id="C")
        tiers = [_schedule("BTCUSDT"), _schedule("ETHUSDT", max_leverage="75")]
        scenarios = list(_execution(config).scenario_evidence)
        for index, scenario_id in enumerate(("A_long", "A_short", "B", "C")):
            loss = Decimal(str(index + 1))
            scenarios[
                next(i for i, item in enumerate(scenarios) if item.scenario_id == scenario_id)
            ] = _scenario(config, scenario_id, losses=[loss] * 100)
        evidence = _execution(
            config,
            scenarios=scenarios,
            losses=[Decimal("4")] * 100,
            deficits=[Decimal("0")] * 100,
        )
        report = evaluate_preflight(config, tiers, _gaps(), evidence, as_of_utc=AS_OF)
        assert report.status == "pass"

    def test_non_worst_declared_authority_fails_and_top_level_must_match_selected(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        scenarios[
            next(i for i, item in enumerate(scenarios) if item.scenario_id == "A_short")
        ] = _scenario(config, "A_short", losses=[Decimal("2")] * 100)
        evidence = _execution(config, scenarios=scenarios)
        report = evaluate_preflight(config, tiers, _gaps(), evidence, as_of_utc=AS_OF)
        assert report.status == "fail"
        assert any("deterministic worst-P99" in reason for reason in report.reasons)

        scenarios = list(_execution(config).scenario_evidence)
        scenarios[
            next(i for i, item in enumerate(scenarios) if item.scenario_id == "A_short")
        ] = _scenario(config, "A_short", losses=[Decimal("2")] * 100)
        evidence = _execution(
            config,
            scenarios=scenarios,
            losses=[Decimal("2")] * 100,
            deficits=[Decimal("1")] * 100,
        )
        report = evaluate_preflight(config, tiers, _gaps(), evidence, as_of_utc=AS_OF)
        assert report.status == "fail"
        assert any("deficit series differs" in reason for reason in report.reasons)

    def test_authority_ties_use_fixed_a_long_first_order(self):
        config, tiers = self._valid_inputs()
        scenarios = list(_execution(config).scenario_evidence)
        for scenario_id in ("A_long", "A_short"):
            scenarios[
                next(i for i, item in enumerate(scenarios) if item.scenario_id == scenario_id)
            ] = _scenario(config, scenario_id, losses=[Decimal("2")] * 100)
        evidence = _execution(
            config,
            scenarios=scenarios,
            losses=[Decimal("2")] * 100,
            deficits=[Decimal("0")] * 100,
        )
        report = evaluate_preflight(config, tiers, _gaps(), evidence, as_of_utc=AS_OF)
        assert report.status == "pass"

        wrong = replace(config, account_loss_authority_scenario_id="A_short")
        wrong_evidence = _execution(
            wrong,
            scenarios=[
                _scenario(wrong, item.scenario_id, losses=item.account_window_losses_usdt)
                for item in scenarios
            ],
            losses=[Decimal("2")] * 100,
            deficits=[Decimal("0")] * 100,
        )
        report = evaluate_preflight(wrong, tiers, _gaps(), wrong_evidence, as_of_utc=AS_OF)
        assert report.status == "fail"
