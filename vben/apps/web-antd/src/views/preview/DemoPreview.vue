<script lang="ts" setup>
import { computed, ref, watch } from 'vue';

import { Bell, CircleAlert, Grid, Search } from '@vben/icons';

import AcceptancePreview from './AcceptancePreview.vue';
import DeskPreview from './DeskPreview.vue';
import { DEMO_DESK_SCENARIOS } from './deskScenarioData';
import type { DeskScenario } from './deskScenarioData';
import GovernancePreview from './GovernancePreview.vue';
import ResearchPreview from './ResearchPreview.vue';

const props = defineProps<{ preview?: unknown; workspace?: unknown }>();
const quantVersion = import.meta.env.VITE_QUANT_VERSION;
const deskScenario = ref<DeskScenario>('normal');
const riskStrip = computed(() => DEMO_DESK_SCENARIOS[deskScenario.value]);
const previewKeys = ['desk', 'research', 'governance'] as const;
type PreviewKey = (typeof previewKeys)[number];
type WorkspaceItem = { key: string; label: string };
type NavigationSection = {
  key: PreviewKey;
  label: string;
  icon: string;
  children: WorkspaceItem[];
};
const navigation: NavigationSection[] = [
  {
    key: 'desk',
    label: '交易台',
    icon: 'grid',
    children: [{ key: 'desk', label: '账户总览' }],
  },
  {
    key: 'research',
    label: '研究工作区',
    icon: 'search',
    children: [
      { key: 'factors', label: '因子实验室' },
      { key: 'orderflow', label: '订单流与价格' },
      { key: 'research', label: '策略研究' },
      { key: 'experiments', label: '实验任务' },
    ],
  },
  {
    key: 'governance',
    label: '验收与运维',
    icon: 'shield',
    children: [
      { key: 'acceptance', label: 'Demo 验收' },
      { key: 'data-quality', label: '数据中心' },
      { key: 'operations', label: '运维与证据' },
      { key: 'settings', label: '系统设置' },
    ],
  },
];
const active = ref<PreviewKey>(
  previewKeys.includes(props.preview as PreviewKey)
    ? (props.preview as PreviewKey)
    : 'desk',
);
const activeWorkspace = ref(
  typeof props.workspace === 'string' ? props.workspace : 'desk',
);
const activeSection = computed(
  () => navigation.find((item) => item.key === active.value) ?? navigation[0]!,
);
const activeWorkspaceLabel = computed(
  () =>
    navigation
      .flatMap((item) => item.children)
      .find((item) => item.key === activeWorkspace.value)?.label ?? '账户总览',
);
const researchWorkspace = computed(() =>
  ['experiments', 'factors', 'orderflow', 'research'].includes(
    activeWorkspace.value,
  )
    ? activeWorkspace.value
    : 'factors',
);
const governanceWorkspace = computed(() =>
  ['acceptance', 'data-quality', 'operations', 'settings'].includes(
    activeWorkspace.value,
  )
    ? activeWorkspace.value
    : 'acceptance',
);

// 深链接仅切换固定本地样本，不读取账户或业务接口。
watch(
  () => [props.preview, props.workspace] as const,
  ([preview, workspace]) => {
    active.value = previewKeys.includes(preview as PreviewKey)
      ? (preview as PreviewKey)
      : 'desk';
    activeWorkspace.value = typeof workspace === 'string' ? workspace : 'desk';
  },
);
function selectSection(section: PreviewKey) {
  active.value = section;
  activeWorkspace.value =
    navigation.find((item) => item.key === section)?.children[0]?.key ?? 'desk';
}
function selectWorkspace(section: PreviewKey, workspace: string) {
  active.value = section;
  activeWorkspace.value = workspace;
}
</script>

<template>
  <main class="preview-shell">
    <aside
      class="terminal-sidebar"
      data-testid="terminal-sidebar"
      aria-label="主导航"
    >
      <a
        class="terminal-brand"
        href="/quant/demo-preview"
        aria-label="量化交易终端首页"
      >
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span><strong>量化终端</strong><small>TRADING SYSTEM</small></span>
      </a>
      <div class="sidebar-divider"></div>
      <nav aria-label="交易系统页面" class="side-navigation">
        <section
          v-for="section in navigation"
          :key="section.key"
          class="navigation-section"
          :class="{ active: active === section.key }"
          :aria-label="section.label"
        >
          <button
            type="button"
            class="navigation-heading"
            :class="{ selected: active === section.key }"
            :data-workspace="section.key"
            :aria-current="active === section.key ? 'true' : undefined"
            @click="selectSection(section.key)"
          >
            <Grid
              v-if="section.icon === 'grid'"
              class="nav-icon"
              :size="16"
              aria-hidden="true"
            />
            <Search
              v-else-if="section.icon === 'search'"
              class="nav-icon"
              :size="16"
              aria-hidden="true"
            />
            <CircleAlert
              v-else
              class="nav-icon"
              :size="16"
              aria-hidden="true"
            />
            <span>{{ section.label }}</span>
          </button>
          <button
            v-for="child in section.children"
            :key="child.key"
            type="button"
            class="navigation-child"
            :class="{
              selected: active === section.key && activeWorkspace === child.key,
            }"
            :data-workspace="child.key"
            :aria-current="
              active === section.key && activeWorkspace === child.key
                ? 'true'
                : undefined
            "
            @click="selectWorkspace(section.key, child.key)"
          >
            <span class="nav-child-line" aria-hidden="true"></span
            >{{ child.label }}
          </button>
        </section>
      </nav>
      <div class="sidebar-bottom">
        <div class="sidebar-divider"></div>
        <div class="account-stamp">
          <span class="account-avatar">D</span
          ><span><strong>演示账户</strong><small>固定本地样本</small></span
          ><span class="account-dot" aria-hidden="true"></span>
        </div>
        <p>账户数据与交易命令<br />均未连接</p>
      </div>
    </aside>
    <div class="terminal-main">
      <header class="terminal-topbar" data-testid="terminal-topbar">
        <div class="breadcrumb">
          <span>工作区</span><b aria-hidden="true">/</b
          ><strong>{{ activeWorkspaceLabel }}</strong>
        </div>
        <div class="topbar-controls">
          <span class="environment-chip"
            ><i aria-hidden="true"></i>Binance Demo</span
          ><span class="topbar-divider"></span
          ><span class="market-clock"
            ><span>市场时间</span><strong>14:32:18 UTC+8</strong></span
          ><button
            class="icon-button"
            type="button"
            aria-label="通知"
            title="通知"
          >
            <Bell :size="16" aria-hidden="true" /><i
              class="notification-dot"
            ></i>
          </button>
        </div>
      </header>
      <div class="preview-scope" role="status" aria-live="polite">
        <span class="scope-mark" aria-hidden="true">i</span
        ><strong>本地演示数据</strong><span>不读取账户，不发送命令</span
        ><span class="scope-version">预览样本 · {{ quantVersion }}</span>
      </div>
      <section
        class="terminal-risk-strip"
        data-testid="terminal-risk-strip"
        aria-label="风险状态摘要"
      >
        <div class="risk-strip-title">
          <span class="risk-shield" aria-hidden="true">!</span
          ><strong>风险监控</strong>
        </div>
        <div class="risk-strip-item">
          <span>账户状态</span
          ><strong class="state-demo" data-testid="risk-account">{{ riskStrip.account }}</strong>
        </div>
        <div class="risk-strip-item">
          <span>日净损益</span
          ><strong class="negative-value" data-testid="risk-pnl">{{ riskStrip.pnl }}</strong>
        </div>
        <div class="risk-strip-item">
          <span>剩余风险预算</span><strong data-testid="risk-budget">{{ riskStrip.budget }}</strong>
        </div>
        <div class="risk-strip-item protection-state">
          <span>保护单状态</span
          ><strong data-testid="risk-protection"><i aria-hidden="true"></i>{{ riskStrip.protection }}</strong>
        </div>
        <span class="risk-strip-clock" data-testid="risk-strip-reason"
          >{{ riskStrip.reason }} · 14:32:18</span
        >
      </section>
      <section
        id="preview-panel"
        class="preview-content"
        data-testid="preview-content"
        role="region"
        :aria-label="activeSection.label"
        tabindex="0"
      >
        <h1 class="sr-only">{{ activeSection.label }}</h1>
        <DeskPreview
          v-if="active === 'desk'"
          :scenario="deskScenario"
          @scenario-change="deskScenario = $event"
        />
        <ResearchPreview
          v-else-if="active === 'research'"
          :workspace="researchWorkspace"
        />
        <AcceptancePreview v-else-if="governanceWorkspace === 'acceptance'" />
        <GovernancePreview v-else :workspace="governanceWorkspace" />
      </section>
      <footer class="preview-footer">
        <span><i aria-hidden="true"></i>本地固定样本</span
        ><span>交互仅更新当前页面状态</span
        ><span>不构成 Demo 验收或实盘资格</span
        ><span class="footer-build">QT · {{ quantVersion }}</span>
      </footer>
    </div>
  </main>
</template>

<style scoped>
.preview-shell {
  --ocean: #0c1b2a;
  --surface: #132b3e;
  --raised: #18364b;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #9bb1c3;
  --blue: #69b4e4;
  --green: #65c998;

  display: grid;
  grid-template-columns: 226px minmax(0, 1fr);
  min-height: 100vh;
  font-family:
    'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif;
  color: var(--text);
  background: var(--ocean);
}

.preview-shell *,
.preview-shell *::before,
.preview-shell *::after {
  box-sizing: border-box;
}

.terminal-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
  min-height: 620px;
  padding: 20px 13px 14px;
  background: #102437;
  border-right: 1px solid var(--line);
}

.terminal-brand {
  display: flex;
  gap: 11px;
  align-items: center;
  min-height: 46px;
  padding: 0 8px;
  color: var(--text);
  text-decoration: none;
}

.brand-mark {
  display: flex;
  gap: 3px;
  align-items: flex-end;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 7px;
  background: #17374d;
  border: 1px solid #3c637d;
}

.brand-mark i {
  display: block;
  width: 4px;
  background: var(--blue);
}

.brand-mark i:nth-child(1) {
  height: 8px;
}

.brand-mark i:nth-child(2) {
  height: 14px;
}

.brand-mark i:nth-child(3) {
  height: 10px;
}

.terminal-brand strong,
.terminal-brand small,
.account-stamp strong,
.account-stamp small {
  display: block;
}

.terminal-brand strong {
  font-size: 14px;
  font-weight: 600;
}

.terminal-brand small {
  margin-top: 3px;
  font-size: 9px;
  color: #7893a8;
}

.sidebar-divider {
  height: 1px;
  margin: 19px 4px 15px;
  background: var(--line);
}

.side-navigation {
  display: grid;
  gap: 15px;
  align-content: start;
  overflow: auto;
}

.navigation-section {
  display: grid;
  gap: 2px;
}

.navigation-heading,
.navigation-child {
  display: flex;
  align-items: center;
  width: 100%;
  font: inherit;
  color: #9bb1c3;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.navigation-heading {
  gap: 11px;
  min-height: 37px;
  padding: 0 10px;
  font-size: 12px;
  font-weight: 600;
}

.nav-icon {
  width: 17px;
  color: #7caac7;
}

.navigation-heading.selected {
  color: var(--text);
  background: #1a3b53;
}

.navigation-heading.selected .nav-icon {
  color: var(--blue);
}

.navigation-child {
  gap: 12px;
  min-height: 31px;
  padding: 0 0 0 20px;
  font-size: 11px;
  color: #849caf;
}

.navigation-child:hover,
.navigation-child.selected {
  color: #deebf4;
}

.navigation-child.selected {
  color: #8dc8ea;
}

.nav-child-line {
  width: 9px;
  height: 1px;
  background: #496477;
}

.navigation-child.selected .nav-child-line {
  height: 2px;
  background: var(--blue);
}

.navigation-heading:focus-visible,
.navigation-child:focus-visible,
.icon-button:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}

.sidebar-bottom {
  margin-top: auto;
}

.sidebar-bottom .sidebar-divider {
  margin-bottom: 14px;
}

.account-stamp {
  display: flex;
  gap: 9px;
  align-items: center;
  padding: 0 7px;
}

.account-avatar {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  font-size: 12px;
  color: #a7c3d6;
  background: #1a394f;
  border: 1px solid #43647a;
}

.account-stamp strong {
  font-size: 11px;
  font-weight: 500;
}

.account-stamp small {
  margin-top: 3px;
  font-size: 10px;
  color: #7f98ab;
}

.account-dot {
  width: 6px;
  height: 6px;
  margin-left: auto;
  background: #e2bd72;
  border-radius: 50%;
}

.sidebar-bottom p {
  margin: 13px 7px 0;
  font-size: 10px;
  line-height: 1.65;
  color: #7690a4;
}

.terminal-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 100vh;
}

.terminal-topbar {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  min-height: 58px;
  padding: 0 24px;
  background: #102437;
  border-bottom: 1px solid var(--line);
}

.breadcrumb {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: #8ea6b9;
}

.breadcrumb b {
  font-weight: 400;
  color: #496477;
}

.breadcrumb strong {
  font-weight: 500;
  color: #e5edf4;
}

.topbar-controls {
  display: flex;
  gap: 17px;
  align-items: center;
}

.environment-chip {
  display: flex;
  gap: 7px;
  align-items: center;
  min-height: 28px;
  padding: 0 9px;
  font-size: 10px;
  color: #d5dfc2;
  background: #27352f;
  border: 1px solid #506348;
}

.environment-chip i,
.preview-footer i {
  width: 6px;
  height: 6px;
  background: #9ccb7c;
  border-radius: 50%;
}

.topbar-divider {
  width: 1px;
  height: 23px;
  background: var(--line);
}

.market-clock {
  display: grid;
  gap: 2px;
}

.market-clock span {
  font-size: 9px;
  color: #8198aa;
}

.market-clock strong {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  font-weight: 400;
}

.icon-button {
  position: relative;
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  font-size: 18px;
  color: #afc3d2;
  cursor: pointer;
  background: transparent;
  border: 1px solid transparent;
}

.notification-dot {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 5px;
  height: 5px;
  background: #e1b96c;
  border: 1px solid #102437;
  border-radius: 50%;
}

.preview-scope {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 34px;
  padding: 0 24px;
  font-size: 10px;
  color: #b3d0b3;
  background: #172e32;
  border-bottom: 1px solid #345446;
}

.scope-mark {
  display: grid;
  place-items: center;
  width: 14px;
  height: 14px;
  font-family: serif;
  font-size: 9px;
  font-weight: 700;
  color: #abd5b2;
  border: 1px solid #6e9a78;
  border-radius: 50%;
}

.preview-scope strong {
  font-size: 10px;
  font-weight: 600;
}

.preview-scope > span:not(.scope-mark):not(.scope-version) {
  color: #91aa9d;
}

.scope-version {
  margin-left: auto;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 9px;
  color: #819c91;
}

.terminal-risk-strip {
  display: flex;
  gap: 0;
  align-items: center;
  min-height: 62px;
  padding: 8px 24px;
  background: #12283a;
  border-bottom: 1px solid var(--line);
}

.risk-strip-title {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 122px;
  font-size: 11px;
}

.risk-shield {
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  font-size: 11px;
  color: #f0c17b;
  border: 1px solid #ba8b5e;
}

.risk-strip-item {
  display: grid;
  gap: 4px;
  min-width: 135px;
  padding: 0 18px;
  border-left: 1px solid #29485c;
}

.risk-strip-item > span {
  font-size: 9px;
  color: #8ea6b9;
}

.risk-strip-item > strong {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  font-weight: 400;
  white-space: nowrap;
}

.risk-strip-item .state-demo {
  font-family: 'IBM Plex Sans', 'Noto Sans SC', sans-serif;
  color: #edcb83;
}

.risk-strip-item .negative-value {
  color: #ec8a8a;
}

.protection-state {
  min-width: 160px;
}

.protection-state strong {
  display: flex;
  gap: 6px;
  align-items: center;
  font-family: 'IBM Plex Sans', 'Noto Sans SC', sans-serif;
  font-size: 10px;
  color: #e3c77f;
}

.protection-state i {
  width: 6px;
  height: 6px;
  background: #e3bd67;
  border-radius: 50%;
}

.risk-strip-clock {
  margin-left: auto;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 9px;
  color: #7991a3;
  white-space: nowrap;
}

.preview-content {
  flex: 1 0 auto;
  min-width: 0;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  white-space: nowrap;
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
}

.preview-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
  justify-content: space-between;
  min-height: 38px;
  padding: 8px 24px;
  font-size: 9px;
  color: #7f98ab;
  background: #102437;
  border-top: 1px solid var(--line);
}

.preview-footer span:first-child {
  display: flex;
  gap: 6px;
  align-items: center;
}

.preview-footer i {
  width: 5px;
  height: 5px;
}

.footer-build {
  font-family: 'IBM Plex Mono', monospace;
  color: #6d8598;
}

@media (max-width: 1100px) {
  .preview-shell {
    grid-template-columns: 196px minmax(0, 1fr);
  }

  .terminal-sidebar {
    padding-inline: 10px;
  }

  .terminal-topbar {
    padding-inline: 18px;
  }

  .terminal-risk-strip {
    flex-wrap: wrap;
    gap: 8px 0;
  }

  .risk-strip-title {
    min-width: 110px;
  }

  .risk-strip-item {
    min-width: 120px;
    padding-inline: 12px;
  }

  .risk-strip-clock {
    display: none;
  }
}

@media (max-width: 700px) {
  .preview-shell {
    display: flex;
    flex-direction: column;
  }

  .terminal-sidebar {
    position: static;
    height: auto;
    min-height: 0;
    padding: 10px 12px 0;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }

  .terminal-brand {
    min-height: 36px;
  }

  .brand-mark {
    width: 28px;
    height: 28px;
  }

  .terminal-sidebar > .sidebar-divider,
  .sidebar-bottom {
    display: none;
  }

  .side-navigation {
    display: flex;
    gap: 5px;
    padding: 9px 0 0;
    overflow-x: auto;
  }

  .navigation-section {
    position: relative;
    display: block;
    flex: 0 0 auto;
  }

  .navigation-heading {
    gap: 7px;
    width: auto;
    min-height: 36px;
    padding: 0 9px;
    font-size: 10px;
  }

  .navigation-heading.selected {
    background: transparent;
    border-bottom: 2px solid var(--blue);
  }

  .navigation-child {
    display: none;
  }

  .navigation-section.active {
    display: grid;
    grid-template-columns: repeat(2, max-content);
    align-content: start;
  }

  .navigation-section.active .navigation-heading {
    grid-column: 1/-1;
  }

  .navigation-section.active .navigation-child {
    display: flex;
    min-height: 28px;
    padding: 0 5px;
    font-size: 9px;
  }

  .terminal-main {
    flex: 1;
    min-height: 0;
  }

  .terminal-topbar {
    min-height: 48px;
    padding-inline: 14px;
  }

  .topbar-controls {
    gap: 9px;
  }

  .market-clock {
    display: none;
  }

  .preview-scope {
    padding-inline: 14px;
  }

  .scope-version {
    display: none;
  }

  .terminal-risk-strip {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0;
    padding: 7px 14px;
  }

  .risk-strip-title {
    grid-column: 1/-1;
    min-height: 27px;
  }

  .risk-strip-item {
    min-width: 0;
    min-height: 38px;
    padding: 5px 8px;
    border-top: 1px solid #29485c;
    border-left: 0;
  }

  .risk-strip-item:nth-of-type(2n) {
    border-left: 1px solid #29485c;
  }

  .risk-strip-item > strong {
    font-size: 10px;
  }

  .protection-state {
    min-width: 0;
  }

  .preview-footer {
    justify-content: flex-start;
    padding-inline: 14px;
  }

  .preview-footer span:nth-child(2) {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
</style>
