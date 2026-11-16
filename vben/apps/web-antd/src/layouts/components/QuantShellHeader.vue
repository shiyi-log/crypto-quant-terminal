<script lang="ts" setup>
import { computed, nextTick, ref, watch } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

const route = useRoute();
const expanded = ref(false);
const navigationToggle = ref<HTMLButtonElement>();
const quantArea = computed(() => route.path.startsWith('/quant/'));
const view = computed(() => route.path.slice('/quant/'.length));
// 旧交易入口是明确白名单；新增页面不会因为遗漏条件而挂载写操作。
const legacyViews = new Set([
  'overview',
  'positions',
  'market',
  'live',
  'pairlist',
  'logs',
]);
const demoViews = new Set(['trade-desk', 'acceptance', 'binance-demo']);
const paperViews = new Set(['strategy-lab', 'strategy-focus']);
const researchViews = new Set([
  'factors',
  'experiments',
  'data-quality',
  'operations',
  'research',
  'backtest',
  'detail',
  'tasks',
  'iteration',
  'orderflow',
  'price-spikes',
  'data',
  'ops',
  'research-ledger',
]);
const legacyControls = computed(() => legacyViews.has(view.value));
const environment = computed(() => {
  if (demoViews.has(view.value))
    return {
      label: 'Binance Demo',
      detail: '账户身份未核验',
      tone: 'demo',
      note: '账户与风险事实见当前页面，页面入口不授予交易资格。',
    };
  if (legacyControls.value)
    return {
      label: '旧交易引擎',
      detail: '账户模式未核验',
      tone: 'legacy',
      note: '实际模拟盘 / 实盘模式以旧引擎配置读取结果为准。',
    };
  if (paperViews.has(view.value))
    return {
      label: '纸面研究',
      detail: '独立模拟账户',
      tone: 'research',
      note: '纸面成交与收益不属于 Demo 或实盘验收。',
    };
  if (researchViews.has(view.value))
    return {
      label: '历史研究与证据',
      detail: '交易账户不适用',
      tone: 'research',
      note: '数据来源与完整性按各工作区分别核对。',
    };
  if (view.value === 'security')
    return {
      label: '身份与安全',
      detail: '交易账户不适用',
      tone: 'research',
      note: '登录身份与交易账户分开管理。',
    };
  return {
    label: '环境未确认',
    detail: '账户身份未核验',
    tone: 'unknown',
    note: '当前入口未建立账户环境映射，请核对页面来源。',
  };
});

// 导航使用独立工作区；各页继续提供原历史入口与原始身份查询。
const workspaces = [
  {
    key: 'trading',
    label: '交易台',
    to: '/quant/trade-desk',
    hint: 'Demo 账户与风险',
  },
  {
    key: 'factors',
    label: '因子实验室',
    to: '/quant/factors',
    hint: '因子来源与研究证据',
  },
  {
    key: 'orderflow',
    label: '订单流与价格',
    to: '/quant/orderflow',
    hint: '观测与成熟标签',
  },
  {
    key: 'strategies',
    label: '策略研究',
    to: '/quant/research',
    hint: '研究与纸面账户',
  },
  {
    key: 'experiments',
    label: '实验任务',
    to: '/quant/experiments',
    hint: '任务与历史产物',
  },
  {
    key: 'acceptance',
    label: '验收与上线',
    to: '/quant/acceptance',
    hint: '固定身份验收',
  },
  {
    key: 'data',
    label: '数据中心',
    to: '/quant/data-quality',
    hint: '采集与覆盖盘点',
  },
  {
    key: 'operations',
    label: '运维与证据',
    to: '/quant/operations',
    hint: '服务与证据记录',
  },
];
const activeWorkspace = computed(() => {
  if (view.value === 'factors') return 'factors';
  if (view.value === 'ops' && route.query.workspace === 'factors')
    return 'factors';
  if (
    ['trade-desk', 'overview', 'positions', 'market', 'live'].includes(
      view.value,
    )
  )
    return 'trading';
  if (['orderflow', 'price-spikes'].includes(view.value)) return 'orderflow';
  if (['research', 'strategy-lab', 'strategy-focus'].includes(view.value))
    return 'strategies';
  if (
    ['experiments', 'backtest', 'detail', 'tasks', 'iteration'].includes(
      view.value,
    )
  )
    return 'experiments';
  if (view.value === 'acceptance') return 'acceptance';
  if (['data', 'data-quality'].includes(view.value)) return 'data';
  if (['operations', 'ops', 'research-ledger', 'logs'].includes(view.value))
    return 'operations';
  return '';
});
const currentWorkspace = computed(
  () =>
    workspaces.find((item) => item.key === activeWorkspace.value)?.label ||
    '设置与系统',
);

async function closeNavigation() {
  expanded.value = false;
  await nextTick();
  navigationToggle.value?.focus();
}
watch(
  () => route.fullPath,
  () => {
    expanded.value = false;
  },
);
</script>

<template>
  <header v-if="quantArea" class="quant-shell-header" :data-expanded="expanded">
    <div class="quant-shell-summary">
      <div class="quant-shell-title">
        <span class="quant-shell-mark" aria-hidden="true">量</span>
        <div>
          <strong>量化交易终端</strong><span>{{ currentWorkspace }}</span>
        </div>
      </div>
      <div
        class="quant-shell-environment"
        role="status"
        aria-live="polite"
        :data-tone="environment.tone"
      >
        <strong>{{ environment.label }}</strong>
        <span>{{ environment.detail }}</span>
      </div>
      <RouterLink class="quant-shell-settings" to="/quant/security"
        >设置与安全</RouterLink
      >
      <button
        ref="navigationToggle"
        class="quant-shell-toggle"
        type="button"
        aria-controls="quant-workspace-navigation"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        工作区
      </button>
    </div>
    <nav
      id="quant-workspace-navigation"
      class="quant-shell-navigation"
      aria-label="工作区"
      @keydown.esc.stop.prevent="closeNavigation"
    >
      <RouterLink
        v-for="item in workspaces"
        :key="item.key"
        :to="item.to"
        :title="item.hint"
        :aria-current="activeWorkspace === item.key ? 'page' : undefined"
        :class="{ 'is-active': activeWorkspace === item.key }"
        >{{ item.label }}</RouterLink
      >
    </nav>
    <p class="quant-shell-boundary">{{ environment.note }}</p>
  </header>
  <section
    v-if="quantArea && legacyControls"
    class="quant-shell-legacy"
    aria-label="旧交易引擎操作"
  >
    <div class="quant-shell-legacy-heading">
      <h2>旧交易引擎操作</h2>
      <span>仅作用于原 Freqtrade 账户</span>
    </div>
    <slot name="legacy-controls" />
  </section>
</template>

<style scoped>
.quant-shell-header {
  position: sticky;
  top: 0;
  z-index: 110;
  min-width: 0;
  max-width: 100%;
  color: #e5edf4;
  background: #0c1b2a;
  border-bottom: 1px solid #29485c;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.quant-shell-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 24px;
  min-width: 0;
  padding: 14px 24px 12px;
}
.quant-shell-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.quant-shell-mark {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  flex-shrink: 0;
  color: #0c1b2a;
  background: #69b4e4;
  font-size: 18px;
  font-weight: 700;
}
.quant-shell-title div {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 14px;
}
.quant-shell-title strong {
  font-size: 16px;
  font-weight: 600;
}
.quant-shell-title span:not(.quant-shell-mark) {
  color: #a2b8c9;
  font-size: 12px;
}
.quant-shell-environment {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding-left: 16px;
  border-left: 1px solid #29485c;
}
.quant-shell-environment strong {
  color: #b4d9f0;
  font-size: 13px;
  font-weight: 600;
}
.quant-shell-environment span {
  color: #a2b8c9;
  font-size: 12px;
}
.quant-shell-environment[data-tone='legacy'] strong,
.quant-shell-environment[data-tone='unknown'] strong {
  color: #f0ce88;
}
.quant-shell-settings {
  margin-left: auto;
  color: #a2b8c9;
  font-size: 12px;
  white-space: nowrap;
}
.quant-shell-toggle {
  display: none;
  color: #e5edf4;
  background: #132b3e;
  border: 1px solid #29485c;
  padding: 7px 12px;
  font-size: 12px;
}
.quant-shell-navigation {
  display: flex;
  flex-wrap: wrap;
  gap: 0;
  padding: 0 12px;
  min-width: 0;
}
.quant-shell-navigation a {
  position: relative;
  display: block;
  padding: 11px 12px;
  color: #a2b8c9;
  font-size: 13px;
  line-height: 1.4;
  text-decoration: none;
}
.quant-shell-navigation a:hover,
.quant-shell-navigation a.is-active {
  color: #e5edf4;
  background: #132b3e;
}
.quant-shell-navigation a.is-active::after {
  position: absolute;
  right: 12px;
  bottom: 0;
  left: 12px;
  height: 2px;
  content: '';
  background: #69b4e4;
}
.quant-shell-header a:focus-visible,
.quant-shell-toggle:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: -2px;
}
.quant-shell-boundary {
  margin: 0;
  padding: 6px 24px 8px;
  color: #a2b8c9;
  border-top: 1px solid #1d3548;
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.quant-shell-legacy {
  margin: 16px 24px 0;
  padding: 16px;
  color: #e5edf4;
  background: #132b3e;
  border: 1px solid #29485c;
}
.quant-shell-legacy-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 16px;
  margin-bottom: 12px;
}
.quant-shell-legacy-heading h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.quant-shell-legacy-heading span {
  color: #a2b8c9;
  font-size: 12px;
}
@media (max-width: 900px) {
  .quant-shell-summary {
    gap: 10px 16px;
    padding-right: 16px;
    padding-left: 16px;
  }
  .quant-shell-title {
    flex: 1;
  }
  .quant-shell-title div {
    display: grid;
    gap: 1px;
  }
  .quant-shell-toggle {
    display: block;
  }
  .quant-shell-environment {
    order: 3;
    width: 100%;
    padding-left: 0;
    border-left: 0;
  }
  .quant-shell-settings {
    margin-left: 0;
  }
  .quant-shell-navigation {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 0 4px;
  }
  .quant-shell-header[data-expanded='false'] .quant-shell-navigation {
    display: none;
  }
  .quant-shell-boundary {
    padding-right: 16px;
    padding-left: 16px;
  }
  .quant-shell-legacy {
    margin-right: 12px;
    margin-left: 12px;
  }
}
@media (max-width: 420px) {
  .quant-shell-summary {
    column-gap: 10px;
  }
  .quant-shell-title strong {
    font-size: 14px;
  }
  .quant-shell-settings {
    flex-shrink: 0;
    white-space: nowrap;
    line-height: 1.4;
  }
}
</style>
