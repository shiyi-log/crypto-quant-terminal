"""Walk-forward momentum research strategy with volatility and funding filters."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas
from pandas import DataFrame

from freqtrade.strategy import IStrategy


RESEARCH_FEATURE_COLUMNS = (
    "%%-momentum_1d",
    "%%-momentum_7d",
    "%%-momentum_30d",
    "%%-rv_7d",
    "%%-rv_30d",
    "%%-rv_30d_percentile",
    "%%-funding_z_30d",
)
TARGET_COLUMN = "&-s_next_open_return_1d"
FUNDING_MAX_AGE = pd.Timedelta(hours=12)
FUNDING_INTERVAL = pd.Timedelta(hours=8)


def build_research_features(candles: DataFrame, funding: DataFrame) -> DataFrame:
    """Build features at t using only candles/funding known by the prior UTC day."""
    required_candle_columns = {"date", "close"}
    if not required_candle_columns.issubset(candles.columns):
        raise ValueError("candles must contain date and close")
    if funding is None or funding.empty or not {"date", "funding_rate"}.issubset(funding.columns):
        raise ValueError("historical funding_rate data is required")

    frame = candles.copy().reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    funding_frame = funding[["date", "funding_rate"]].dropna().copy()
    funding_frame["date"] = pd.to_datetime(funding_frame["date"], utc=True)
    funding_frame["funding_rate"] = pd.to_numeric(funding_frame["funding_rate"], errors="coerce")
    funding_frame = funding_frame.dropna().sort_values("date").drop_duplicates("date", keep="last")
    if funding_frame.empty:
        raise ValueError("historical funding_rate data is required")
    if (funding_frame["date"].diff().dropna() > FUNDING_INTERVAL).any():
        raise ValueError("historical funding_rate contains missing 8-hour events")
    funding_frame = funding_frame.rename(columns={"date": "funding_date"})

    joined = pd.merge_asof(
        frame[["date"]].sort_values("date"),
        funding_frame,
        left_on="date",
        right_on="funding_date",
        direction="backward",
        allow_exact_matches=True,
    )
    funding_age = joined["date"] - joined["funding_date"]
    if funding_age.isna().any() or (funding_age > FUNDING_MAX_AGE).any():
        raise ValueError("historical funding_rate contains a gap greater than 12 hours")

    close = pd.to_numeric(frame["close"], errors="coerce")
    returns = close.pct_change()
    prior_returns = returns.shift(1)
    frame["%%-momentum_1d"] = prior_returns
    frame["%%-momentum_7d"] = close.pct_change(7).shift(1)
    frame["%%-momentum_30d"] = close.pct_change(30).shift(1)
    rv7 = prior_returns.rolling(7, min_periods=7).std(ddof=0)
    rv30 = prior_returns.rolling(30, min_periods=30).std(ddof=0)
    frame["%%-rv_7d"] = rv7
    frame["%%-rv_30d"] = rv30
    frame["%%-rv_30d_percentile"] = rv30.rolling(365, min_periods=90).apply(
        lambda window: pd.Series(window).rank(pct=True).iloc[-1], raw=False
    )

    prior_funding = joined["funding_rate"].shift(1).reset_index(drop=True)
    funding_mean = prior_funding.rolling(30, min_periods=20).mean()
    funding_std = prior_funding.rolling(30, min_periods=20).std(ddof=0).replace(0, np.nan)
    frame["%%-funding_z_30d"] = (prior_funding - funding_mean) / funding_std
    return frame


def build_next_open_target(candles: DataFrame) -> pd.Series:
    """Label the one-day holding interval entered at the next candle open."""
    if "open" not in candles.columns:
        raise ValueError("candles must contain open")
    open_price = pd.to_numeric(candles["open"], errors="coerce")
    return open_price.shift(-2).div(open_price.shift(-1)).sub(1.0)


class MomentumRegimeResearch(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 500
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    minimal_roi = {"0": 100.0}
    stoploss = -0.99
    trailing_stop = False

    entry_threshold = 0.0016
    max_volatility_percentile = 0.95
    funding_extreme_z = 2.0

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        # Direct file load: Freqtrade's funding resampling introduces artificial
        # 1h gaps for this research dataset.  The archive file has exact 8h spacing.
        import json
        from pathlib import Path
        pair = str(metadata["pair"]).replace("/", "_").replace(":", "_")
        funding_path = Path(__file__).resolve().parents[1] / "data" / "binance" / "futures" / f"{pair}-1h-funding_rate.json"
        raw = json.loads(funding_path.read_text(encoding="utf-8"))
        funding = pandas.DataFrame(raw, columns=["date", "funding_rate"])
        funding["date"] = pandas.to_datetime(funding["date"], unit="ms", utc=True)
        features = build_research_features(dataframe, funding)
        for column in RESEARCH_FEATURE_COLUMNS:
            dataframe[column] = features[column].to_numpy()
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        dataframe[TARGET_COLUMN] = build_next_open_target(dataframe).to_numpy()
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return self.freqai.start(dataframe, metadata, self)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        prediction = dataframe[TARGET_COLUMN]
        momentum = dataframe["%%-momentum_7d"]
        volatility_ok = dataframe["%%-rv_30d_percentile"] <= self.max_volatility_percentile
        funding_z = dataframe["%%-funding_z_30d"]
        predictable = dataframe["do_predict"] == 1

        long_condition = (
            predictable
            & volatility_ok
            & (prediction > self.entry_threshold)
            & (momentum > 0)
            & (funding_z < self.funding_extreme_z)
        )
        short_condition = (
            predictable
            & volatility_ok
            & (prediction < -self.entry_threshold)
            & (momentum < 0)
            & (funding_z > -self.funding_extreme_z)
        )
        dataframe.loc[long_condition, ["enter_long", "enter_tag"]] = (1, "momentum_regime_long")
        dataframe.loc[short_condition, ["enter_short", "enter_tag"]] = (1, "momentum_regime_short")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        prediction = dataframe[TARGET_COLUMN]
        momentum = dataframe["%%-momentum_7d"]
        dataframe.loc[(prediction <= 0) | (momentum <= 0), ["exit_long", "exit_tag"]] = (
            1,
            "momentum_regime_exit",
        )
        dataframe.loc[(prediction >= 0) | (momentum >= 0), ["exit_short", "exit_tag"]] = (
            1,
            "momentum_regime_exit",
        )
        return dataframe
