# K 线运行检查

数据库检查默认读取每个数据集最近 750 根时间槽，使用完整身份（交易所、市场、交易对、周期和 candle_type）与时间范围查主键索引。每个数据集使用独立只读事务和 10 秒语句超时；某个数据集超时会保留失败记录并继续检查其余数据集。报告注明这不是跨币种同时刻的研究快照。

示例：

    .venv/bin/python tools/check_candle_integrity.py \
      --pair BTC/USDT:USDT --pair ETH/USDT:USDT --timeframe 5m \
      --window-bars 750 --query-timeout 10 --max-age-bars 3 \
      --output /absolute/path/candle_report.json

--full-history 显式读取全部历史。运行窗口通过只代表该窗口的检查通过，不能替代历史缺口审计；旧历史报告应另存。原有离线 --input 快照不自动截短，可用于复核完整冻结证据。

只有 extras.closed=true 且 timestamp + interval <= as_of 的行算最终收盘。报告同时提供最后观测时间和最后最终收盘时间；未收盘、缺少 closed 或未来时间的行不能掩盖收盘数据超期。价格型 mark/index 数据不要求交易量与订单流字段；普通成交数据仍按原字段要求检查。

--dataset-policy /absolute/path/policies.json 指定每个完整数据身份的用途。例如：

    {
      "datasets": [
        {
          "exchange": "binance",
          "market": "futures",
          "pair": "BTC/USDT:USDT",
          "timeframe": "15m",
          "candle_type": "mark",
          "policy": {
            "mode": "frozen",
            "source": "native",
            "end_ms": 1793491200000
          }
        }
      ]
    }

mode 为 active 或 frozen，默认 active。冻结仅免除实时新鲜度要求，仍检查缺口、重复和适用字段。冻结 end_ms 是固定的半开区间上界；有界运行检查必须提供该上界，没有该上界时只允许显式 --full-history，不能用可变摘要的最后时间悄悄移动冻结范围。source 为 native、resampled 或 unspecified，标签不改变数据身份，也不会把两个来源混算。策略可按数据集指定 required_fields 与 max_age_bars。

运行状态分别记录检查开始、结束和最后健康完成时间。查询异常仅记录异常类型，不写连接地址或数据库错误文本。完整报告与状态通过同目录临时文件、fsync 和原子替换发布；失败轮也发布失败报告，旧成功不会因未写新报告而被当作当前结果。

这些改动不回填行情、不修改研究样本、不运行数据库初始化。加载到常驻服务需要正常切换该只读检查进程；24 小时连续运行和七天验收另行记录。
