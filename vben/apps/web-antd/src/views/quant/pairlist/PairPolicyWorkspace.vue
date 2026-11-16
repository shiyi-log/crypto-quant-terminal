<script lang="ts" setup>
import type { PairLock } from './pairPolicy';
import type { createPairPolicy } from './pairPolicy';

import { computed, nextTick, ref, watch } from 'vue';

import { lockTime, splitPairs } from './pairPolicy';

const props = defineProps<{ policy: ReturnType<typeof createPairPolicy> }>();
const state = computed(() => props.policy.state);
const view = ref<'blacklist' | 'locks' | 'whitelist'>('whitelist');
const newPairs = ref('');
const selected = ref<string[]>([]);
const query = ref('');
const dialog = ref<HTMLElement>();
const pending = ref<null | {
  kind: 'add' | 'remove' | 'unlock';
  pairs: string[];
  lock?: PairLock;
}>(null);
let opener: HTMLElement | null = null;
const views = [
  { key: 'whitelist' as const, name: '允许入场名单' },
  { key: 'blacklist' as const, name: '临时黑名单' },
  { key: 'locks' as const, name: '保护锁' },
];
const source = computed(() => state.value[view.value]);
const sourceLabel = computed(() =>
  source.value.error
    ? source.value.data === null
      ? '读取失败，事实未知'
      : '读取失败，保留上次快照'
    : source.value.data === null
      ? '尚未读取'
      : '已读取',
);
const freshBlacklist = computed(
  () => state.value.blacklist.data !== null && !state.value.blacklist.error,
);
const freshLocks = computed(
  () => state.value.locks.data !== null && !state.value.locks.error,
);
const filteredWhitelist = computed(() =>
  (state.value.whitelist.data ?? []).filter((pair) =>
    pair.toLowerCase().includes(query.value.toLowerCase()),
  ),
);
const filteredBlacklist = computed(() =>
  (state.value.blacklist.data?.pairs ?? []).filter((pair) =>
    pair.toLowerCase().includes(query.value.toLowerCase()),
  ),
);
const filteredLocks = computed(() =>
  (state.value.locks.data?.rows ?? []).filter((row) =>
    (row.pair + ' ' + row.reason)
      .toLowerCase()
      .includes(query.value.toLowerCase()),
  ),
);
const pendingTitle = computed(() =>
  pending.value?.kind === 'add'
    ? '加入旧引擎黑名单'
    : pending.value?.kind === 'remove'
      ? '移出旧引擎黑名单'
      : '解除旧引擎保护锁',
);
watch(
  () => state.value.blacklist.data?.pairs,
  (pairs) => {
    selected.value = selected.value.filter((pair) => pairs?.includes(pair));
  },
);
function choose(key: typeof view.value) {
  view.value = key;
  query.value = '';
}
async function confirm(kind: 'add' | 'remove' | 'unlock', lock?: PairLock) {
  if (state.value.busy) return;
  const pairs =
    kind === 'add'
      ? splitPairs(newPairs.value).filter(
          (pair) => !state.value.blacklist.data?.pairs.includes(pair),
        )
      : [...selected.value];
  if (kind !== 'unlock' && !pairs.length) return;
  opener =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  pending.value = { kind, pairs, lock };
  await nextTick();
  dialog.value
    ?.querySelector<HTMLButtonElement>('[data-action=cancel]')
    ?.focus();
}
async function close() {
  pending.value = null;
  await nextTick();
  opener?.focus();
}
async function execute() {
  const request = pending.value;
  if (!request || state.value.busy) return;
  const ok =
    request.kind === 'add'
      ? await props.policy.add(request.pairs)
      : request.kind === 'remove'
        ? await props.policy.remove(request.pairs)
        : await props.policy.unlock(request.lock!.id);
  if (ok && request.kind === 'add') newPairs.value = '';
  if (ok && request.kind === 'remove') selected.value = [];
  await close();
}
function dialogKeys(event: KeyboardEvent) {
  if (event.key === 'Escape' && !state.value.busy) {
    event.preventDefault();
    void close();
  }
  if (event.key !== 'Tab') return;
  const buttons = [
    ...(dialog.value?.querySelectorAll<HTMLButtonElement>(
      'button:not(:disabled)',
    ) ?? []),
  ];
  const first = buttons[0];
  const last = buttons.at(-1);
  if (!first) {
    event.preventDefault();
    dialog.value?.focus();
    return;
  }
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  }
  if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
function direction(side: string) {
  return side === 'long'
    ? '多'
    : side === 'short'
      ? '空'
      : side === '*'
        ? '多 / 空'
        : '方向未知';
}
function remaining(row: PairLock) {
  if (!row.active) return '已失效';
  const end = row.lock_end_timestamp;
  if (end === undefined) return '结束时间未知';
  const seconds = Math.ceil((end - Date.now()) / 1000);
  return seconds <= 0
    ? '已到期，等待引擎确认'
    : seconds < 3600
      ? Math.ceil(seconds / 60) + ' 分钟'
      : Math.floor(seconds / 3600) +
        ' 小时 ' +
        Math.ceil((seconds % 3600) / 60) +
        ' 分钟';
}
</script>

<template>
  <main class="pair-policy">
    <header class="policy-heading">
      <div>
        <p>设置 / 旧交易引擎</p>
        <h1>交易对与保护策略</h1>
        <p>运行期生效名单、临时限制与逐笔保护锁。</p>
      </div>
      <button
        class="refresh"
        :disabled="state.loading || state.busy"
        @click="policy.refresh()"
      >
        {{ state.loading ? '正在读取' : '刷新事实' }}
      </button>
    </header>
    <div class="engine-boundary" role="note">
      <strong>旧交易引擎</strong><span>实际模拟 / 实盘模式未知</span>
      <p>本页操作不会解除 Binance Demo 的账户冷却或风险锁。</p>
    </div>
    <div class="policy-grid">
      <section class="policy-ledger" aria-label="名单与保护锁">
        <nav aria-label="名单视图">
          <button
            v-for="item in views"
            :key="item.key"
            :data-view="item.key"
            :aria-pressed="view === item.key"
            @click="choose(item.key)"
          >
            {{ item.name
            }}<span>{{
              state[item.key].data === null
                ? '未知'
                : item.key === 'whitelist'
                  ? state.whitelist.data?.length
                  : item.key === 'blacklist'
                    ? state.blacklist.data?.pairs.length
                    : state.locks.data?.count
            }}</span>
          </button>
        </nav>
        <div class="ledger-toolbar">
          <label
            >筛选当前视图<input
              v-model="query"
              aria-label="筛选交易对"
              placeholder="交易对或锁定原因" /></label
          ><span
            class="source-state"
            :data-failed="!!source.error"
            role="status"
            >{{ sourceLabel
            }}<small
              >本机接收
              {{ source.at === null ? '未知' : lockTime(source.at) }}</small
            ></span
          >
        </div>
        <p v-if="source.error" class="read-error" role="alert">
          {{ source.error }}。{{
            source.data === null
              ? '当前事实未知。'
              : '保留上次成功数据，请确认时效。'
          }}
        </p>
        <template v-if="view === 'whitelist'">
          <p class="view-description">
            引擎经过 pairlist 过滤后的运行名单；它不代表新 Demo 账户的交易许可。
          </p>
          <div v-if="filteredWhitelist.length" class="whitelist">
            <span v-for="pair in filteredWhitelist" :key="pair">{{
              pair
            }}</span>
          </div>
          <p v-else class="empty">
            {{
              state.whitelist.data === null
                ? '白名单尚未读取'
                : query
                  ? '没有匹配的交易对'
                  : '白名单为空'
            }}
          </p>
        </template>
        <template v-else-if="view === 'blacklist'">
          <p class="view-description">
            临时禁止旧引擎开仓及加仓；重载配置或重启可能清除临时条目。
          </p>
          <form class="add-form" @submit.prevent="confirm('add')">
            <label
              >新增限制<input
                v-model="newPairs"
                aria-label="新增黑名单交易对"
                placeholder="BTC/USDT:USDT，可用逗号或空格分隔"
                :disabled="state.busy" /></label
            ><button
              data-action="add"
              :disabled="
                !freshBlacklist || state.busy || !splitPairs(newPairs).length
              "
            >
              加入黑名单
            </button>
          </form>
          <div class="bulk-tools">
            <span>已选 {{ selected.length }} 项</span
            ><button
              class="danger"
              data-action="remove"
              :disabled="!freshBlacklist || state.busy || !selected.length"
              @click="confirm('remove')"
            >
              移出选中条目
            </button>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">选择</th>
                  <th scope="col">限制条目</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="pair in filteredBlacklist" :key="pair">
                  <td>
                    <input
                      v-model="selected"
                      type="checkbox"
                      :value="pair"
                      :aria-label="'选择 ' + pair"
                      :disabled="state.busy"
                    />
                  </td>
                  <td>{{ pair }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!filteredBlacklist.length" class="empty">
            {{
              state.blacklist.data === null
                ? '黑名单尚未读取'
                : query
                  ? '没有匹配的限制'
                  : '黑名单为空'
            }}
          </p>
          <div class="method">
            <span>引擎方法</span
            >{{ state.blacklist.data?.methods.join(' / ') || '未提供' }}
          </div>
          <ul v-if="state.blacklist.data?.errors.length" class="pair-errors">
            <li
              v-for="[pair, error] in state.blacklist.data.errors"
              :key="pair"
            >
              {{ pair }}：{{ error }}
            </li>
          </ul>
          <details class="expansion">
            <summary>
              展开后的实际生效名单（{{
                state.blacklist.data?.expanded.length ?? '未知'
              }}）
            </summary>
            <div class="whitelist">
              <span
                v-for="pair in state.blacklist.data?.expanded"
                :key="pair"
                >{{ pair }}</span
              >
            </div>
          </details>
        </template>
        <template v-else>
          <p class="view-description">
            由旧引擎 protections
            产生。解除后旧引擎可能立即重新开仓，其他限制仍按各自政策生效。
          </p>
          <div class="table-scroll">
            <table class="lock-table">
              <thead>
                <tr>
                  <th scope="col">交易对 / 方向</th>
                  <th scope="col">保护原因</th>
                  <th scope="col">时间与状态</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in filteredLocks" :key="row.id">
                  <td>
                    {{ row.pair
                    }}<small>{{ direction(row.side) }} · 锁 {{ row.id }}</small>
                  </td>
                  <td>{{ row.reason || '未提供' }}</td>
                  <td>
                    <span>{{ remaining(row) }}</span
                    ><small
                      >开始
                      {{ lockTime(row.lock_timestamp ?? row.lock_time) }}</small
                    ><small
                      >结束
                      {{
                        lockTime(row.lock_end_timestamp ?? row.lock_end_time)
                      }}</small
                    >
                  </td>
                  <td>
                    <button
                      v-if="row.active"
                      :data-lock="row.id"
                      class="danger"
                      :disabled="!freshLocks || state.busy"
                      @click="confirm('unlock', row)"
                    >
                      解除旧引擎锁</button
                    ><span v-else>无需操作</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!filteredLocks.length" class="empty">
            {{
              state.locks.data === null
                ? '保护锁尚未读取'
                : query
                  ? '没有匹配的保护锁'
                  : '当前无保护锁'
            }}
          </p>
        </template>
      </section>
      <aside aria-label="证据与操作边界">
        <h2>读取证据</h2>
        <dl>
          <template v-for="item in views" :key="item.key"
            ><dt>{{ item.name }}</dt>
            <dd>
              {{
                state[item.key].error
                  ? '读取失败，保留已知事实'
                  : state[item.key].data === null
                    ? '未知'
                    : '已读取'
              }}<small>{{
                state[item.key].at === null
                  ? '接收时间未知'
                  : lockTime(state[item.key].at!)
              }}</small>
            </dd></template
          >
        </dl>
        <p>每 30 秒只读刷新。接收时间只说明本页何时收到响应。</p>
        <h2>操作结果</h2>
        <div v-if="state.command" role="status" class="command-result">
          <strong>{{ state.command.label }}</strong>
          <p>
            {{
              state.command.status === 'blocked'
                ? '未发送命令，请刷新完整事实后重新确认。'
                : state.command.status === 'received'
                  ? '已收到命令回执；当前状态以上方重新读取事实为准。'
                  : state.command.status === 'rejected'
                    ? '存在逐项拒绝，请检查明细。'
                    : '命令结果未知；不会自动重发，请先核对引擎事实。'
            }}
          </p>
          <ul>
            <li v-for="[pair, error] in state.command.errors" :key="pair">
              {{ pair }}：{{ error }}
            </li>
          </ul>
        </div>
        <p v-else>尚无本页操作。读取和切换视图不会修改交易状态。</p>
        <h2>适用范围</h2>
        <p>
          仅作用于旧引擎。新账户风险、资金升级和策略上线由各自的批准与风险服务控制。
        </p>
      </aside>
    </div>
    <div v-if="pending" class="confirm-overlay" @keydown="dialogKeys">
      <section
        ref="dialog"
        class="confirm-dialog"
        role="alertdialog"
        tabindex="-1"
        aria-modal="true"
        aria-labelledby="pair-confirm-title"
      >
        <h2 id="pair-confirm-title">{{ pendingTitle }}</h2>
        <p>
          {{
            pending.kind === 'add'
              ? '这些条目将限制旧引擎入场。'
              : '限制解除后，旧引擎可能立即重新开仓。'
          }}
        </p>
        <p class="confirm-identities">
          {{
            pending.lock
              ? pending.lock.pair + ' / 锁 ' + pending.lock.id
              : pending.pairs.join('、')
          }}
        </p>
        <p>此操作不修改新账户的冷却或风险锁。</p>
        <div>
          <button data-action="cancel" :disabled="state.busy" @click="close">
            取消</button
          ><button
            data-action="confirm"
            class="danger"
            :disabled="state.busy"
            @click="execute"
          >
            {{ state.busy ? '等待回执与复核' : '确认操作' }}
          </button>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.pair-policy {
  --bg: #0c1b2a;
  --surface: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --blue: #69b4e4;
  min-width: 0;
  background: var(--bg);
  color: var(--text);
  padding: 24px;
  font:
    14px/1.6 'IBM Plex Sans',
    'Source Han Sans SC',
    'Noto Sans CJK SC',
    sans-serif;
}
.policy-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}
.policy-heading h1 {
  font-size: 26px;
  line-height: 1.3;
  font-weight: 600;
  margin: 5px 0 8px;
}
.policy-heading p,
.view-description,
aside p {
  color: var(--muted);
  max-width: 76ch;
  margin: 0;
}
.policy-heading p:first-child {
  font-size: 12px;
}
.pair-policy button {
  background: #17394f;
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 7px 12px;
  cursor: pointer;
  color: var(--text);
  font: inherit;
}
.pair-policy button:hover:not(:disabled) {
  border-color: var(--blue);
}
.pair-policy :is(button, input, summary):focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 3px;
}
.pair-policy button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.pair-policy button.danger {
  color: #f19a9a;
  border-color: #79565a;
}
.engine-boundary {
  border-left: 3px solid #f0c17b;
  background: #183044;
  padding: 12px 16px;
  margin-bottom: 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
}
.engine-boundary strong {
  color: #f0c17b;
}
.engine-boundary p {
  flex-basis: 100%;
  margin: 0;
  color: var(--muted);
}
.policy-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 275px;
  border: 1px solid var(--line);
  background: var(--surface);
}
.policy-ledger {
  min-width: 0;
}
.policy-ledger nav {
  display: flex;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--line);
}
.policy-ledger nav button {
  flex: 1;
  min-width: 130px;
  border: 0;
  border-radius: 0;
  background: transparent;
  padding: 14px;
}
.policy-ledger nav button[aria-pressed='true'] {
  box-shadow: inset 0 -2px var(--blue);
  background: #1b3a50;
}
.policy-ledger nav span {
  color: var(--muted);
  margin-left: 12px;
  font-size: 12px;
}
.ledger-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 16px;
}
.pair-policy label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
  color: var(--muted);
  min-width: 0;
}
.pair-policy input:not([type='checkbox']) {
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 8px 10px;
  font: inherit;
  max-width: 100%;
  width: 280px;
  box-sizing: border-box;
}
.source-state {
  font-size: 12px;
  color: #7acbaa;
  text-align: right;
}
.source-state[data-failed='true'],
.read-error,
.pair-errors {
  color: #f0c17b;
}
small {
  display: block;
  font-size: 12px;
  color: var(--muted);
  font-weight: 400;
}
.view-description,
.read-error {
  margin: 0 16px 16px;
}
.whitelist {
  padding: 0 16px 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.whitelist > span {
  border: 1px solid var(--line);
  padding: 6px 9px;
  font-variant-numeric: tabular-nums;
}
.empty {
  padding: 32px 16px;
  color: var(--muted);
  text-align: center;
}
.add-form {
  padding: 0 16px 16px;
  display: flex;
  align-items: end;
  gap: 10px;
}
.add-form label {
  flex: 1;
}
.add-form input:not([type='checkbox']) {
  width: 100%;
}
.bulk-tools {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  color: var(--muted);
  font-size: 12px;
}
.table-scroll {
  max-width: 100%;
  overflow: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-variant-numeric: tabular-nums;
}
th {
  font-size: 12px;
  color: var(--muted);
  background: #102638;
  font-weight: 500;
}
th,
td {
  padding: 11px 16px;
  border-bottom: 1px solid var(--line);
}
td {
  overflow-wrap: anywhere;
}
th:first-child {
  width: 72px;
}
.lock-table {
  min-width: 660px;
}
.lock-table th:first-child {
  width: 28%;
}
.lock-table td:last-child {
  white-space: nowrap;
}
.method {
  padding: 14px 16px;
  display: flex;
  gap: 18px;
  font-size: 12px;
}
.method span {
  color: var(--muted);
}
.expansion {
  border-top: 1px solid var(--line);
  padding: 12px 16px;
}
.expansion summary {
  cursor: pointer;
  color: var(--blue);
}
.expansion .whitelist {
  padding: 14px 0 0;
}
aside {
  border-left: 1px solid var(--line);
  padding: 20px;
}
aside h2 {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 12px;
}
aside h2:not(:first-child) {
  margin-top: 26px;
}
aside p,
aside dd {
  font-size: 12px;
}
aside dl {
  margin: 0;
}
aside dt {
  font-size: 12px;
  color: var(--muted);
  margin: 12px 0 3px;
}
aside dd {
  margin: 0;
}
.command-result {
  border-left: 2px solid var(--blue);
  padding-left: 12px;
}
.command-result strong {
  font-size: 12px;
}
.command-result ul {
  padding-left: 16px;
  color: #f0c17b;
  font-size: 12px;
}
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: #020b14bf;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.confirm-dialog {
  background: var(--surface);
  border: 1px solid var(--line);
  padding: 24px;
  width: 480px;
  max-width: 100%;
  box-shadow: 0 20px 80px #0008;
}
.confirm-dialog h2 {
  font-size: 20px;
  margin: 0 0 12px;
}
.confirm-dialog p {
  color: var(--muted);
}
.confirm-dialog .confirm-identities {
  color: var(--text);
  overflow-wrap: anywhere;
}
.confirm-dialog > div {
  display: flex;
  justify-content: end;
  gap: 10px;
  margin-top: 24px;
}
@media (max-width: 1050px) {
  .policy-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  aside {
    border-left: 0;
    border-top: 1px solid var(--line);
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 24px;
  }
  aside h2:not(:first-child) {
    margin-top: 0;
  }
  aside dl {
    grid-row: span 2;
  }
}
@media (max-width: 600px) {
  .pair-policy {
    padding: 14px;
  }
  .policy-heading {
    align-items: start;
  }
  .policy-heading h1 {
    font-size: 22px;
  }
  .policy-heading .refresh {
    white-space: nowrap;
  }
  .policy-ledger nav button {
    min-width: 0;
    flex-basis: 33%;
    font-size: 12px;
    padding: 12px 4px;
  }
  .policy-ledger nav span {
    display: block;
    margin: 2px 0 0;
  }
  .ledger-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
  .ledger-toolbar input:not([type='checkbox']) {
    width: 100%;
  }
  .source-state {
    text-align: left;
  }
  .add-form {
    align-items: stretch;
    flex-direction: column;
  }
  .add-form > button {
    align-self: end;
  }
  aside {
    display: block;
  }
  .pair-policy aside h2:not(:first-child) {
    margin-top: 24px;
  }
  .confirm-dialog {
    padding: 18px;
  }
}
</style>
