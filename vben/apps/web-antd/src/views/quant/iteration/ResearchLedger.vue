<script lang="ts" setup>
import { onMounted, ref } from 'vue';

import { Alert, Button, Card, Empty, Table, Tag } from 'ant-design-vue';

import {
  getResearchLedger,
  type ResearchLedger,
  type ResearchLedgerVersion,
} from '#/api/freqtrade';

const ledger = ref<ResearchLedger | null>(null);
const loading = ref(false);
const error = ref('');

const versionColumns = [
  { dataIndex: 'id', title: '版本 / 轮次', width: 190 },
  { dataIndex: 'direction', title: '迭代方向', width: 150 },
  { key: 'change', title: '相对基线的变化' },
  { key: 'reported', title: '原报指标', width: 170 },
  { key: 'deployment', title: '部署状态', width: 130 },
  { key: 'evaluation', title: '评估有效性', width: 150 },
  { key: 'effect', title: '实际订单 / 平仓', width: 150 },
  { key: 'availability', title: '可用性依据' },
];
const tradeColumns = [
  { dataIndex: 'trade_id', title: '交易', width: 80 },
  { dataIndex: 'pair', title: '币对' },
  { dataIndex: 'side', title: '方向' },
  { dataIndex: 'leverage', title: '杠杆' },
  { dataIndex: 'open_date', title: '开仓' },
  { dataIndex: 'close_date', title: '平仓' },
  { key: 'profit', title: '已实现收益' },
  { dataIndex: 'model_id', title: '模型归属' },
  { dataIndex: 'attribution_status', title: '归因状态' },
];
const orderColumns = [
  { dataIndex: 'order_id', title: '订单', width: 90 },
  { dataIndex: 'trade_id', title: '交易' },
  { dataIndex: 'pair', title: '币对' },
  { dataIndex: 'side', title: '方向' },
  { dataIndex: 'status', title: '状态' },
  { key: 'fill', title: '数量 / 成交' },
  { key: 'price', title: '价格' },
  { dataIndex: 'order_date', title: '时间' },
  { dataIndex: 'model_id', title: '模型归属' },
];

function value(value: unknown, suffix = '') {
  return value === null || value === undefined || value === ''
    ? '暂无'
    : `${value}${suffix}`;
}
function number(value: unknown, digits = 4) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toFixed(digits)
    : '暂无';
}
function config(value: unknown) {
  if (value === null || value === undefined || value === '') return '暂无';
  return typeof value === 'string' ? value : JSON.stringify(value);
}
function statusColor(status: string) {
  if (status === 'verified') return 'green';
  if (status === 'invalidated') return 'red';
  if (status === 'not_deployed') return 'orange';
  return 'blue';
}
function statusText(status: string) {
  const names: Record<string, string> = {
    verified: '已验证',
    unverified: '未验证',
    not_deployed: '未部署',
    invalidated: '已作废',
    forward_observation: '事前观察中',
    planned: '计划中',
  };
  return names[status] || status || '暂无';
}
function modelAttribution(modelId: string | null) {
  return modelId || '未知（未归因）';
}
function join(items?: string[]) {
  return items?.length ? items.join('；') : '暂无记录';
}
function changeHistory(items?: ResearchLedgerVersion['changes']) {
  if (!items?.length) return '暂无记录';
  return items
    .map((item) => {
      if (typeof item === 'string') return item;
      return [item.t, item.event, item.note].filter(Boolean).join('：');
    })
    .filter(Boolean)
    .join('；');
}
function configChanges(
  items?: ResearchLedger['comparison'] extends infer Comparison
    ? Comparison extends { config_changes?: infer Changes }
      ? Changes
      : never
    : never,
) {
  if (!items) return '暂无记录';
  if (!items.length) return '配置一致（无参数差异）';
  return items
    .map((item) => {
      if (typeof item === 'string') return item;
      const before = config(item.before);
      const after = config(item.after);
      return `${item.key}: ${before} → ${after}`;
    })
    .join('；');
}
function repeatRule(value: string | boolean | null | undefined) {
  if (value === true) return '避免再次使用未经验证口径';
  if (value === false || value == null || value === '') return '暂无记录';
  return value;
}
function versionRow(item: ResearchLedgerVersion) {
  const metrics = item.reported_metrics || {};
  return {
    ...item,
    key: item.id,
    roundText:
      item.round == null ? item.id : `${item.id} · 第 ${item.round} 轮`,
    changeText: `${changeHistory(item.changes)}${item.config ? ` | 配置: ${config(item.config)}` : ''}`,
    reportedText: `IC ${number(metrics.ic)} · t ${number(metrics.t, 2)} · q ${number(metrics.q)}`,
    effectText:
      item.effect?.actual_orders == null && item.effect?.closed_trades == null
        ? '未归因'
        : `${value(item.effect?.actual_orders)} / ${value(item.effect?.closed_trades)} · 收益 ${value(item.effect?.realized_profit_abs)}`,
    availabilityText: `证据：${join(item.evidence)}；限制：${join(item.limitations)}`,
  };
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    ledger.value = await getResearchLedger();
  } catch (cause: any) {
    ledger.value = null;
    error.value = cause?.message || '证据账读取失败';
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <Card :bordered="false" class="mb-4 shadow-sm" title="模型与交易证据账">
    <template #extra>
      <Button :loading="loading" size="small" @click="load">手动刷新</Button>
    </template>

    <Alert
      v-if="error"
      class="mb-3"
      type="error"
      show-icon
      :message="error"
      description="请检查认证服务和账本接口后重试。"
    >
      <template #action
        ><Button size="small" @click="load">重试</Button></template
      >
    </Alert>

    <Empty v-if="!ledger && !loading && !error" description="暂无证据账数据" />
    <template v-if="ledger">
      <Tag v-if="ledger.source_id?.includes('dryrun')" color="red" class="mb-2">
        模拟盘 dry-run · 模拟订单与收益
      </Tag>
      <div class="mb-3 text-xs text-muted-foreground">
        来源 {{ value(ledger.source_id) }} · 接口快照
        {{ value(ledger.generated_at) }} · 研究账本事件同步
        {{ value(ledger.last_synced_at) }}
      </div>

      <div class="grid grid-cols-2 gap-3 lg:grid-cols-7">
        <div
          v-for="item in [
            ['登记版本', ledger.summary.version_count],
            ['实际交易', ledger.summary.actual_trade_count],
            ['实际订单', ledger.summary.actual_order_count],
            ['已平仓', ledger.summary.closed_trade_count],
            ['已部署 ML', ledger.summary.deployed_ml_count],
            ['未归因交易', ledger.summary.unattributed_trade_count],
            ['已实现收益', ledger.summary.realized_profit_abs],
          ]"
          :key="item[0]"
          class="rounded-lg border border-gray-100 p-3 dark:border-gray-800"
        >
          <div class="text-xs text-muted-foreground">{{ item[0] }}</div>
          <div class="mt-1 font-mono text-base font-semibold">
            {{ value(item[1]) }}
          </div>
        </div>
      </div>
      <div
        v-if="ledger.summary.closed_trade_count === 0"
        class="mt-3 rounded-lg bg-orange-50 p-3 text-xs text-orange-700 dark:bg-orange-950/20"
      >
        暂无成熟收益：当前没有已平仓交易，收益、胜率和期望值不作推断。
      </div>
      <div
        v-if="ledger.summary.deployed_ml_count === 0"
        class="mt-2 rounded-lg bg-blue-50 p-3 text-xs text-blue-700 dark:bg-blue-950/20"
      >
        ML 未部署：研究版本没有参与实际订单，登记 champion 不代表运行中的模型。
      </div>

      <div class="mt-5 mb-2 text-sm font-medium">每代模型与策略版本</div>
      <Table
        :columns="versionColumns"
        :data-source="ledger.versions.map(versionRow)"
        :pagination="{ pageSize: 8, hideOnSinglePage: true }"
        :scroll="{ x: 1100 }"
        size="small"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'id'"
            ><span class="font-mono text-xs">{{
              record.roundText
            }}</span></template
          >
          <template v-else-if="column.key === 'change'"
            ><span class="text-xs">{{ record.changeText }}</span></template
          >
          <template v-else-if="column.key === 'reported'"
            ><span class="font-mono text-xs">{{
              record.reportedText
            }}</span></template
          >
          <template v-else-if="column.key === 'deployment'"
            ><Tag :color="statusColor(record.deployment_status)">{{
              statusText(record.deployment_status)
            }}</Tag></template
          >
          <template v-else-if="column.key === 'evaluation'"
            ><Tag :color="statusColor(record.evaluation_status)">{{
              statusText(record.evaluation_status)
            }}</Tag></template
          >
          <template v-else-if="column.key === 'effect'"
            ><span class="font-mono text-xs">{{
              record.effectText
            }}</span></template
          >
          <template v-else-if="column.key === 'availability'"
            ><span class="text-xs">{{
              record.availabilityText
            }}</span></template
          >
        </template>
      </Table>
      <div
        v-if="ledger.versions.length"
        class="mt-2 text-xs text-muted-foreground"
      >
        版本指标仅作原始记录；已作废或未验证的历史结果不会被当作收益证据。
      </div>

      <div
        v-if="ledger.comparison"
        class="mt-5 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
      >
        <div class="mb-1 font-medium">基线与候选对比</div>
        <div>
          基线 {{ value(ledger.comparison.baseline_id) }} → 候选
          {{ value(ledger.comparison.candidate_id) }}
        </div>
        <div class="mt-1">
          参数变化：{{ configChanges(ledger.comparison.config_changes) }}
        </div>
        <div class="mt-1 text-muted-foreground">
          {{ value(ledger.comparison.note) }}
        </div>
      </div>

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div>
          <div class="mb-2 text-sm font-medium">实际交易（与订单分开）</div>
          <Table
            :columns="tradeColumns"
            :data-source="ledger.actual_trades"
            :pagination="{ pageSize: 6, hideOnSinglePage: true }"
            :scroll="{ x: 1000 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'profit'"
                ><span class="font-mono text-xs">{{
                  value(record.realized_profit_abs)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'model_id'"
                ><span class="text-xs">{{
                  modelAttribution(record.model_id)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'close_date'"
                ><span class="text-xs">{{
                  value(record.close_date)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'attribution_status'"
                ><Tag color="blue">{{
                  value(record.attribution_status)
                }}</Tag></template
              >
            </template>
          </Table>
        </div>
        <div>
          <div class="mb-2 text-sm font-medium">实际订单</div>
          <Table
            :columns="orderColumns"
            :data-source="ledger.actual_orders"
            :pagination="{ pageSize: 6, hideOnSinglePage: true }"
            :scroll="{ x: 950 }"
            size="small"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'fill'"
                ><span class="font-mono text-xs"
                  >{{ value(record.amount) }} / {{ value(record.filled) }}</span
                ></template
              >
              <template v-else-if="column.key === 'price'"
                ><span class="font-mono text-xs">{{
                  value(record.price)
                }}</span></template
              >
              <template v-else-if="column.dataIndex === 'model_id'"
                ><span class="text-xs">{{
                  modelAttribution(record.model_id)
                }}</span></template
              >
            </template>
          </Table>
        </div>
      </div>

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div>
          <div class="mb-2 text-sm font-medium">已确认的教训</div>
          <div
            v-for="lesson in ledger.lessons"
            :key="lesson.id"
            class="mb-2 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
          >
            <div class="flex items-center gap-2">
              <b>{{ lesson.title }}</b
              ><Tag :color="statusColor(lesson.status)">{{
                statusText(lesson.status)
              }}</Tag>
            </div>
            <div class="mt-1">原因：{{ lesson.reason }}</div>
            <div class="mt-1 text-muted-foreground">
              证据：{{ join(lesson.evidence) }}
            </div>
            <div class="mt-1 text-orange-600">
              避免重犯：{{ repeatRule(lesson.do_not_repeat) }}
            </div>
          </div>
          <Empty
            v-if="!ledger.lessons.length"
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            description="暂无教训记录"
          />
        </div>
        <div>
          <div class="mb-2 text-sm font-medium">研究方向</div>
          <div
            v-for="direction in ledger.directions"
            :key="direction.id"
            class="mb-2 rounded-lg border border-gray-100 p-3 text-xs dark:border-gray-800"
          >
            <div class="flex items-center gap-2">
              <b>{{ direction.title }}</b
              ><Tag :color="statusColor(direction.status)">{{
                statusText(direction.status)
              }}</Tag>
            </div>
            <div class="mt-1">假设：{{ direction.hypothesis }}</div>
            <div class="mt-1 text-muted-foreground">
              下一检查点：{{ direction.next_check }}
            </div>
          </div>
          <Empty
            v-if="!ledger.directions.length"
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            description="暂无研究方向"
          />
        </div>
      </div>
      <Alert
        v-if="ledger.errors.length"
        class="mt-4"
        type="warning"
        show-icon
        message="账本存在数据错误"
        :description="ledger.errors.join('；')"
      />
    </template>
  </Card>
</template>
