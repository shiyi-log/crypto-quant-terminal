# 验证阶段完整实验族统计

`research_statistics.py` 是纯离线近似统计计算器。它计算全部冻结验证配置的净损益、时间块 bootstrap 单侧 p 值、完整族校正和同时下界，不读取文件/数据库，不运行策略、训练模型或访问交易所，也不领取最终留出票。

即使 `calculation_status=computed`，`status` 仍是 `not_estimable`：输入来源和 inference 事前登记未由此模块认证；`source_authenticated`、`inference_registration_verified`、`acceptance_eligible`、`production_authorized` 固定为 False。SHA 证明数值输入绑定，不能证明收益真实、资金费完整、无特征前视或保护/批准有效。

运行日志使用标准logger的INFO级别：成功输入校验输出B、M及索引抽样次数，成功计算输出固定完成摘要；拒绝只输出固定不可估计原因码。一次计算最多两条此模块日志，不记录账户/尝试/配置ID、哈希、种子、金额、原始输入或异常堆栈，也不逐次记录99,999个重采样。日志不参与冻结证据或统计决策。

## 接口与冻结政策

调用 `freeze_validation_inference(family, spec)`，family 必须是现有 `freeze_experiment_family` 产生且可规范重建的 `FrozenResearchEvidence`。新政策绑定 family SHA、五身份、全部原始fold及完整 M，不修改已有 family。再调用 `calculate_validation_family(family, inference, series)`；输入不齐或任一配置无效时返回固定错误码、空 `results`，不删除配置后重新计算。政策冻结本身不产生服务端登记证明。

family和inference必须是精确的内置 `FrozenResearchEvidence` 类型，canonical_json及sha256必须是精确内置str；拒绝重写解析、编码或比较行为的子类。受信校验从规范字符串直接解析，避免在原SHA下用自定义to_dict替换fold窗口。

spec 必须恰好包含下表全部字段，不接受省略默认值、额外认证字段或隐藏的测试重采样次数。

| 字段 | 支持的显式值 |
| --- | --- |
| stage / metric | `validation` / `fixed_capital_equity_delta_usdt` |
| period_ms | 正整数，共同评价网格；块长必须为其整数倍 |
| block_ms | 与原 family comparison 一致，且不小于 label_span_ms |
| block_anchor / scheme | `fold_start` / `nonoverlapping_stratified` |
| initial_capital | 正的精确普通十进制字符串，如 `1000` |
| capital_policy | `fixed_no_compounding_no_transfers` |
| boundary_policy | `flat_before_and_after_fold_costs_included` |
| evaluation_as_of_ms | 不早于冻结 dataset.end_ms 的整数时间戳 |
| method / replicates | `time_block_bootstrap` / `99999` |
| seed / rng | 独立于策略seed的64位小写SHA形式种子 / `sha256-counter-rejection-v1` |
| money_scale | `24` |
| null / tail | `aggregate_mean_le_zero` / `greater_equal` |
| multiple_testing / alpha | 与family一致的 `holm` 或 `bonferroni` / 精确十进制alpha |
| confidence_bound | `bonferroni_basic_rank` |
| min_tail_count / min_blocks_per_fold | `20` / `30` |
| degeneracy | `any_config_fold_constant_blocks_reject_family` |
| max_input_rows | 正整数，全部配置总行数预算，上限2,000,000 |
| max_index_draws | 正整数，上限30,000,000 |
| max_weighted_additions | 正整数，上限60,000,000 |
| max_distribution_cells | 正整数，上限2,000,000 |

每fold的有效验证窗口长度必须整除block_ms，且至少30块，不静默丢尾、补零、跨fold或改变现有purge/embargo。窗口不是整块时需要在看结果前重新冻结适配的协议，不能事后调块长重试。方法 `hac` 明确拒绝，不能替换为 bootstrap。

保护性硬限制还包括：family规范JSON不超过1MB，inference不超过100KB，dataset跨度不超过3660日、推导fold不超过128。这些检查在调用旧协议重建前执行，避免损坏小输入触发巨大fold循环。

## 输入序列

series是列表，恰好包含全部family配置，每个配置一个包，包括全部fold。包必须恰好含：

`family_sha256`、`inference_sha256`、`config_id`、`attempt_id`、`producer_id`、`identities`、`stage=validation`、`state=completed`、`execution_sha256`、`cost_sha256`、`equity_sha256`、`rows`。

五身份必须与family一致，attempt_id全族唯一；三个来源SHA只能作为引用。`rows`严格按fold顺序和共同时间网格列出，行字段恰好为 `fold_id`（从0起）、`start_ms`、`end_ms`、`available_ms`、`complete=True`、`net_pnl`。期间为 `[start_ms,end_ms)`，available_ms不得早于period结束或晚于冻结evaluation_as_of_ms。

输入只能包含原family各validation_start/end窗口，不能混入训练、purge、embargo或最终留出行。多行、漏行、重复、错顺序及越界全部拒绝，不读全部后悄悄筛选。统计模块只有聚合期间PnL，不能独立证明策略特征在决策时可得；未来worker必须物理限定源数据，并核验决策引用的available_ms。

净损益应由真实执行会计生产器提供，为固定资本下期间权益增量，涵盖期间已实现及未实现损益、手续费、滑点、资金费和边界成本；不能只记录平仓交易。初始和最终持仓必须按冻结政策平掉并记成本。base/stress都必须真实独立复算，计算器不把base损益乘系数伪造stress。已完整观察的无仓期间可显式为0，未知成本/缺行不能补0。

金额仅普通十进制字符串，整数部分最多18位、小数最多24位；拒绝float/bool、科学计数、NaN/Inf、空白、正号和负零。金额无损转scale=24整数；聚合、差值和排序不经过默认Decimal上下文。均值和p输出约分后的 numerator/denominator，展示端应单独注明舍入，禁止用展示值作门槛。

## 数学政策

fold f有K_f块，每块L个period，配置j块和为X_jfk。N=LΣK_f，净损益T_j=ΣX_jfk。对每个重采样b，在每fold内等概率有放回抽K_f块，同一组索引用于全部配置，不跨fold。D_jb=Σ_f(Σ_draw X_jf,index−Σ_k X_jfk)，条件期望为0；固定K_f时等价于对每fold样本均值居中，不要求每fold真实均值为0。长短fold按实际period数加权。

H0为时间加权总体期望净损益<=0。c_j=count(D_jb>=T_j)，单侧raw_p=(1+c_j)/(B+1)，相等计入尾部，禁止p=0。完整族M包含全部base/stress和seeds。Bonferroni为min(1,Mp)；Holm按(p,config_id)排序并累计最大 `(M−i+1)p_i`。不要求配置间独立。

同时下界无论p选哪种校正都用Bonferroni基本区间。a=alpha/M，R=floor(a(B+1))，要求R>=20。D升序第k=B−R+1个为q，下界=T−q；不插值，严格下界>0。该决策与plus-one raw_p<=a精确对应；Holm显著而同时下界不为正可能发生，不能改写结果。

bootstrap仍是近似推断，要求每fold内足够平稳、矩存在、弱依赖及合适块长，跨fold残余依赖可忽略。标签跨度不保证统计独立，块数不叫独立样本数；重叠训练、长记忆、市场结构变化会违反假设。30块/20尾部rank是工程门槛，不是覆盖率证明。每配置每fold块和恒定时整族不可估计，不因恒定正收益报告确定盈利。增加B不能修复低有效样本；不得换seed、调块长或增B试到通过。

## 确定性抽样和预算

domain为ASCII `sha256-counter-rejection-v1` + NUL + familySHA原始32字节 + inferenceSHA原始32字节 + seed原始32字节。每个索引digest为SHA256(domain || uint64be(b) || uint32be(fold) || uint32be(draw) || uint32be(retry))，序号从0起。u为big-endian整数；K为该fold块数，u>=floor(2^256/K)*K则重试，否则索引=u%K。retry最多64次，耗尽报固定诊断。config_id不进入抽样，分片和配置输入顺序不改变索引。共享流按(b,fold,draw)将uint32be(index)写入SHA，输出resample_digest。

预检 `BΣK_f` 索引、`M BΣK_f` 加法、`M B` 分布单元及输入总行数。预算不够整族拒绝，不降低B、不缩M。当前硬CPU预算对最小365日、5fold、每fold30块时最多支持M=4；常见12/24/120配置族会被完整拒绝。这个首版证明可实际复算的小切片，尚不能声称完成最大120族实际研究；扩大能力需要保持整数及共同抽样语义的另行实现和验证。

## 归档和阶段集成

返回FrozenResearchEvidence，包含policy/family/input SHA、全部配置与attempt及来源SHA manifest、block/grid、精确统计结果、RNG及共享流摘要。相同冻结输入复算字节一致。API只计算，不自行写文件；之后通过现有安全归档工具write-new内容寻址落盘，核验真实文件SHA。

当前Registry最终留出只允许所选一个base/stress配置对，现有candidate仅检查统计声明并要求family_size=M。验证族p不能标成最终留出p，也不能为满足该字段运行全M留出。未来需服务端在结果前持久冻结inference/attempt、真实权益会计生产器、来源attestation、事前选择规则和独立最终pair检验政策，最后升级候选协议同时检查两阶段证据。此模块不改这些现有边界，也不认证保护或批准。
