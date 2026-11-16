# 研究产物本地内容核验

本模块把声明的本地 JSONL 蜡烛文件转换成可复算的质量证据。它实际读取文件字节、计算 SHA-256，并调用现有 `assess_candle_coverage`。它不启动研究 worker，不训练或选择策略，不连接交易所，不登记数据库，也不授予 Demo 或生产资格。

## 来源边界

文件哈希只绑定读取到的字节，不证明数据来自 Binance 或其他真实来源。当前没有独立内部不可变来源收据核验器，模块始终传入 `trusted_source=False`。即使一年数据无缺根、无重复且可用时间完整，质量结果仍为 `not_estimable`，保留 `source_untrusted`，产物中的 `acceptance_eligible` 始终为 `false`。

JSONL 不接受 `trusted_source` 等额外字段；冻结 spec 同样拒绝额外字段。HTTP 标志、文件 mtime、SHA、用户提供的 JSON 收据都不会提升来源信任。如果未来接入独立来源收据，应另行验证不可变性、实际 source SHA 绑定与身份，再增加受保护的内部调用路径；不能增加一个用户可控布尔开关。

## 输入与冻结条件

调用方显式提供一个 spec 字典，字段必须完全如下。时间为非负整数毫秒，窗口为 `[start_ms,end_ms)`；`as_of_ms` 是本次证据的冻结观察截止时间，不取当前墙上时钟。

~~~json
{
  "symbol": "BTC/USDT:USDT",
  "timeframe": "1h",
  "start_ms": 0,
  "end_ms": 31536000000,
  "as_of_ms": 31536000000,
  "source_ref": "archive:btc-hourly-v1"
}
~~~

支持协议现有 BTC、ETH、TRX USDT 永续，以及 1m、5m、15m、1h。非对齐窗口进入协议质量判断，不擅自移动边界。运行路径应使用真实目录的绝对路径；macOS 的 `/tmp`、`/var` 等可能是系统符号链接，应显式使用 `/private/tmp`、`/private/var` 下的真实路径。

JSONL 每行是一个对象，必须显式包含八个字段：

~~~json
{"timestamp_ms":0,"available_ms":3600000,"closed":true,"open":"10.00000000000000000000000000000001","high":"11","low":"9","close":"10","volume":"0"}
~~~

可选 `symbol`、`timeframe` 字段若提供，必须与冻结 spec 一致。金额使用精确十进制字符串或 JSON 整数；拒绝浮点 token、科学计数 JSON 数字、NaN/Infinity、重复键、非法 UTF-8、BOM、孤立代理字符、超深 JSON 和超过 96 位的 JSON 整数。十进制字符串由协议继续检查有限性、幅度、OHLC 关系及量的非负性。非法事实产生质量原因，不填补或修复。缺少 `available_ms` 属于结构错误，不能从闭盘时间或 mtime 推断。

空文件保留零行并报告缺根；空行属于无效 JSON。最后一行允许没有换行符。字节 SHA 包括所有原始空格与换行，因此格式不同的源文件具有不同来源 SHA。规范事实指纹由协议单独计算。

## Python 使用

~~~python
from research_artifact_verifier import (
    build_candle_snapshot,
    verify_local_artifact,
    write_snapshot,
)

result = build_candle_snapshot(
    "/private/tmp/research-input/candles.jsonl",
    frozen_spec,
    allowed_roots=["/private/tmp/research-input"],
)
output = write_snapshot(
    result,
    "/private/tmp/research-output",
    allowed_roots=["/private/tmp/research-output"],
)
metadata = verify_local_artifact(
    output,
    allowed_roots=["/private/tmp/research-output"],
    expected_sha256=result.sha256,
    media_type="application/json",
    reference="artifact:quality-v1",
).to_dict()
~~~

`build_candle_snapshot` 返回 `FrozenResearchEvidence`。规范 JSON 包含冻结 spec、实际来源 SHA/字节数、来源未认证标志、质量报告及其 SHA。相同冻结输入和原始字节产生相同 JSON 和 SHA，不包含机器绝对路径或读取时间。

`verify_local_artifact` 重新读取稳定文件、比较预期 SHA，并校验可登记的媒体类型声明。它不会进行完整文件格式识别；允许声明为 application/json、text/csv、application/octet-stream、image/png、application/pdf。引用限定为 `artifact:`、`archive:`、`dataset:`、`quality:` 后接安全逻辑名字，不接受网络 URL、`@`、查询串、片段或任意 scheme。该规则是登记 URI 规则的安全子集。

返回字典的 `uri`、`sha256`、`media_type` 可供调用方构造 `register_artifact` 参数；还必须单独提供合法 `artifact_id`、`attempt_id`、`created_ms`，并满足已完成尝试及事务规则。不要把核验器额外的 `size_bytes`、`source_authenticated` 字段直接传入要求精确字段集合的登记接口。核验成功也不证明 registry 已提交或来源可信。

## 离线命令

项目 Python 环境示例：

~~~sh
.venv/bin/python -m research_artifact_verifier snapshot \
  --source /private/tmp/research-input/candles.jsonl \
  --spec /private/tmp/research-input/spec.json \
  --output-dir /private/tmp/research-output \
  --allow-root /private/tmp/research-input \
  --allow-root /private/tmp/research-output

.venv/bin/python -m research_artifact_verifier verify \
  --source /private/tmp/research-output/实际SHA.json \
  --expected-sha256 实际SHA \
  --media-type application/json \
  --reference artifact:quality-v1 \
  --allow-root /private/tmp/research-output
~~~

输出目录必须已存在。命令成功退出 0；输入、路径、哈希、内容或写入失败退出 2，仅输出有界中文摘要与稳定错误码。快照成功表示证据文件写入成功；摘要携带实际质量状态和原因，通常包括 `source_untrusted`，不表示质量通过。未知参数错误也不回显参数值。

## 文件安全与资源限制

只读取显式允许根中的路径，拒绝 `..`、NUL、任意层符号链接与非普通文件。逐级通过目录描述符和 `O_NOFOLLOW` 打开，读取结束比较文件描述符与当前路径的设备、inode、大小、mtime/ctime 纳秒值，检测读取期间修改及路径替换。SHA 通过有界字节块读取计算；没有把整个文件一次性读入内存。

默认总源字节上限 256 MiB、单行 64 KiB、600000 根。Python API 允许收紧三个限制，CLI 允许通过 `--max-bytes` 收紧总字节。不得提高到超过内置上限。协议要求真实行列表，故内存仍随行数增长；一年小时线 8760 根可离线验证，一年分钟线 525600 根可能受总字节或内存限制，应预先规划资源和可信分片协议，不能静默截断。

输出以 `SHA.json` 命名，字节严格等于规范 JSON 的 UTF-8，不额外追加换行。写入使用 `O_EXCL` 创建新文件，已有文件或链接均拒绝覆盖。写后重读验证 SHA，并核对文件名及目录身份。失败时只清理本次仍拥有的 inode，保留并发替换的文件。输出权限默认只读 0444；权限不是不可变真实性证明，有写权限的外部进程仍可在返回之后改变目录或文件，消费前应再次核验。

典型错误码包括 `path_unsafe`、`file_not_regular`、`source_changed`、`limit_exceeded`、`json_duplicate_key`、`json_number_inexact`、`candle_schema_invalid`、`sha256_mismatch`、`output_exists`、`output_changed`。错误日志不包含原始蜡烛行、绝对源路径或任意异常文本。

## 本次验证记录

2026-11-17 离线测试先确认缺少核验器时失败，再完成实现。新增测试使用真实临时文件，动态生成一年 8760 根小时线，不提交数据文件；覆盖字节篡改、读取期间原地修改和路径替换、显式可用时间、精确小数、重复/缺根/真实零量、非法 JSON/编码、路径链接与 FIFO、资源限制、输出写入失败与并发替换、命令行有界错误及规范产物复核。

本模块是 P2 内容证据生产器的一部分。独立来源真实性、完整研究尝试、统计比较、一次性留出、审批及生产门槛仍由各自协议与持久化证据负责，本次实现不证明 P2 完成。
