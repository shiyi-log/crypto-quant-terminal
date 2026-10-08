"""
FreqAI 基线策略 —— 币安 USDT-M 永续合约

设计要点：
1. 用 LightGBM 回归预测未来 N 根 K 线的收益率（滚动重训练，天然 walk-forward）
2. 特征分三层：
   - expand_all   : 多周期指标（RSI/MFI/ADX/布林/ROC/相对成交量）
   - expand_basic : 原始价量 + 波动率（ATR）
   - standard     : 时间特征 + **资金费率（加密独有特征）**
3. 只在 do_predict == 1（样本未越界，DI 检验通过）时交易
4. 多空双向，带止损/追踪止损/最小 ROI

关于资金费率特征：
  资金费率反映多空拥挤度与杠杆成本，是加密市场特有的、股票因子库里没有的信息。
  通过 dp.get_pair_dataframe(..., candle_type="funding_rate") 取真实历史值。
  使用 merge_asof(direction="backward") 保证只用「当时已知」的费率，不引入前视偏差。
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import DecimalParameter, IStrategy, IntParameter

logger = logging.getLogger(__name__)


class FreqaiFundingStrategy(IStrategy):
    """FreqAI + 资金费率特征的合约基线策略。"""

    INTERFACE_VERSION = 3

    timeframe = "5m"
    can_short = True

    process_only_new_candles = True
    startup_candle_count: int = 200

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ---------------- 风控 ----------------
    stoploss = -0.035
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.018
    trailing_only_offset_is_reached = True
    minimal_roi = {"0": 0.05}

    # ---------------- 可优化参数 ----------------
    entry_threshold = DecimalParameter(
        0.001, 0.020, default=0.005, decimals=3, space="buy", optimize=True
    )
    exit_threshold = DecimalParameter(
        -0.020, -0.001, default=-0.003, decimals=3, space="sell", optimize=True
    )

    plot_config = {
        "main_plot": {},
        "subplots": {
            "prediction": {"&-s_close": {"color": "blue"}},
            "funding": {
                "%-funding_rate": {"color": "orange"},
                "%-funding_rate_cum24h": {"color": "red"},
            },
            "do_predict": {"do_predict": {"color": "green"}},
        },
    }

    # ==================================================================
    # 特征工程
    # ==================================================================
    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        """按 indicator_periods_candles 自动扩展的指标类特征。"""
        dataframe[f"%-rsi-period_{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-mfi-period_{period}"] = ta.MFI(dataframe, timeperiod=period)
        dataframe[f"%-adx-period_{period}"] = ta.ADX(dataframe, timeperiod=period)
        dataframe[f"%-sma-period_{period}"] = ta.SMA(dataframe, timeperiod=period)
        dataframe[f"%-ema-period_{period}"] = ta.EMA(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.2
        )
        dataframe[f"%-bb_lower-period_{period}"] = bollinger["lower"]
        dataframe[f"%-bb_mid-period_{period}"] = bollinger["mid"]
        dataframe[f"%-bb_upper-period_{period}"] = bollinger["upper"]
        dataframe[f"%-bb_width-period_{period}"] = (
            bollinger["upper"] - bollinger["lower"]
        ) / bollinger["mid"]
        dataframe[f"%-close-bb_lower-period_{period}"] = (
            dataframe["close"] / bollinger["lower"]
        )

        dataframe[f"%-roc-period_{period}"] = ta.ROC(dataframe, timeperiod=period)
        dataframe[f"%-relative_volume-period_{period}"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )

        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """按 timeframe / 相关性币对 / 平移自动扩展的原始量价特征。"""
        dataframe["%-pct-change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        dataframe["%-hl-range"] = (dataframe["high"] - dataframe["low"]) / dataframe["close"]
        dataframe["%-oc-change"] = (dataframe["close"] - dataframe["open"]) / dataframe["open"]
        dataframe["%-atr_norm"] = ta.ATR(dataframe, timeperiod=14) / dataframe["close"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """不扩展的特征：时间 + 资金费率。"""
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        dataframe = self._add_funding_features(dataframe, metadata)
        return dataframe

    def _add_funding_features(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """把真实历史资金费率对齐到基础 K 线上（严格只用过去信息）。"""
        funding_cols = ["%-funding_rate", "%-funding_rate_ma3", "%-funding_rate_cum24h"]

        try:
            funding = self.dp.get_pair_dataframe(
                pair=metadata["pair"], timeframe="1h", candle_type="funding_rate"
            )
        except Exception as exc:  # 数据缺失时降级为 0，不让训练整体失败
            logger.warning("资金费率数据获取失败 (%s): %s", metadata["pair"], exc)
            funding = None

        if funding is None or funding.empty or "funding_rate" not in funding.columns:
            logger.warning("资金费率数据为空，%s 使用 0 填充", metadata["pair"])
            for col in funding_cols:
                dataframe[col] = 0.0
            return dataframe

        fr = (
            funding[["date", "funding_rate"]]
            .dropna()
            .sort_values("date")
            .reset_index(drop=True)
        )

        # direction="backward"：只取当前时间或更早的费率，杜绝前视偏差
        aligned = pd.merge_asof(
            dataframe[["date"]].sort_values("date").reset_index(drop=True),
            fr,
            on="date",
            direction="backward",
        )["funding_rate"].to_numpy()

        dataframe["%-funding_rate"] = aligned
        series = pd.Series(aligned)
        dataframe["%-funding_rate_ma3"] = series.rolling(3, min_periods=1).mean().to_numpy()
        dataframe["%-funding_rate_cum24h"] = series.rolling(24, min_periods=1).sum().to_numpy()

        for col in funding_cols:
            dataframe[col] = dataframe[col].ffill().fillna(0.0)

        return dataframe

    # ==================================================================
    # 预测目标
    # ==================================================================
    def set_freqai_targets(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """标签：未来 label_period_candles 根 K 线的平均收益率。"""
        label_period = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-s_close"] = (
            dataframe["close"]
            .shift(-label_period)
            .rolling(label_period)
            .mean()
            / dataframe["close"]
            - 1
        )
        return dataframe

    # ==================================================================
    # 交易逻辑
    # ==================================================================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        long_cond = (df["do_predict"] == 1) & (df["&-s_close"] > self.entry_threshold.value)
        short_cond = (df["do_predict"] == 1) & (df["&-s_close"] < -self.entry_threshold.value)

        df.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "ai_long")
        df.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "ai_short")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        exit_long_cond = (df["do_predict"] == 1) & (df["&-s_close"] < self.exit_threshold.value)
        exit_short_cond = (df["do_predict"] == 1) & (df["&-s_close"] > -self.exit_threshold.value)

        df.loc[exit_long_cond, ["exit_long", "exit_tag"]] = (1, "ai_exit_long")
        df.loc[exit_short_cond, ["exit_short", "exit_tag"]] = (1, "ai_exit_short")
        return df

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """基线先用 1x，隔离杠杆影响，便于评估策略本身质量。"""
        return 1.0
