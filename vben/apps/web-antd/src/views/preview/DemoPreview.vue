<script lang="ts" setup>
import { computed, ref } from 'vue';

import DeskPreview from './DeskPreview.vue';
import GovernancePreview from './GovernancePreview.vue';
import ResearchPreview from './ResearchPreview.vue';

const active = ref('desk');
const navigation = [
  { key: 'desk', label: '交易台' },
  { key: 'research', label: '研究工作区' },
  { key: 'governance', label: '验收与运维' },
];
const activeLabel = computed(
  () => navigation.find((item) => item.key === active.value)?.label ?? '交易台',
);
</script>

<template>
  <main class="preview-shell">
    <header class="preview-heading">
      <div>
        <p class="preview-kicker">QUANT TERMINAL / DESIGN PREVIEW</p>
        <h1>交易系统设计预览</h1>
        <p class="preview-subtitle">{{ activeLabel }} · 固定本地样本</p>
      </div>
      <div class="preview-scope" role="status" aria-live="polite">
        <span class="scope-dot" aria-hidden="true" />
        <div><strong>本地演示数据</strong><span>不读取账户，不发送命令</span></div>
      </div>
    </header>
    <nav class="preview-navigation" aria-label="预览工作区" role="tablist">
      <button
        v-for="item in navigation"
        :key="item.key"
        type="button"
        role="tab"
        :data-workspace="item.key"
        :aria-selected="active === item.key"
        @click="active = item.key"
      >{{ item.label }}</button>
    </nav>
    <section class="preview-content" data-testid="preview-content">
      <DeskPreview v-if="active === 'desk'" />
      <ResearchPreview v-else-if="active === 'research'" workspace="factors" />
      <GovernancePreview v-else workspace="acceptance" />
    </section>
    <footer class="preview-footer">
      <span>所有数值均为固定样本</span>
      <span>交互仅更新当前页面状态</span>
      <span>不构成 Demo 验收或实盘资格</span>
    </footer>
  </main>
</template>

<style scoped>
.preview-shell {
  --ocean: #0c1b2a;
  --surface: #132b3e;
  --line: #29485c;
  --text: #e5edf4;
  --muted: #a2b8c9;
  --signal: #69b4e4;
  display: flex;
  min-height: 100vh;
  flex-direction: column;
  min-width: 0;
  color: var(--text);
  background: var(--ocean);
  font-family: 'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif;
}
.preview-shell *, .preview-shell *::before, .preview-shell *::after { box-sizing: border-box; }
.preview-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 22px 26px 20px;
  border-bottom: 1px solid var(--line);
}
.preview-kicker { margin: 0 0 5px; color: var(--signal); font-size: 11px; line-height: 1.5; }
h1 { margin: 0; font-size: 24px; font-weight: 600; line-height: 1.3; }
.preview-subtitle { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.preview-scope { display: flex; align-items: center; gap: 10px; padding: 10px 13px; border: 1px solid #49634f; background: #172d32; }
.scope-dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: #80c68b; }
.preview-scope div { display: grid; gap: 3px; }
.preview-scope strong { font-size: 12px; font-weight: 600; }
.preview-scope span { color: var(--muted); font-size: 11px; }
.preview-navigation { display: flex; gap: 4px; overflow-x: auto; padding: 0 26px; border-bottom: 1px solid var(--line); scrollbar-width: thin; }
.preview-navigation button { min-height: 46px; padding: 0 15px; border: 0; border-bottom: 2px solid transparent; color: var(--muted); background: transparent; font: inherit; font-size: 13px; white-space: nowrap; cursor: pointer; }
.preview-navigation button[aria-selected='true'] { border-bottom-color: var(--signal); color: var(--text); }
.preview-navigation button:focus-visible { outline: 2px solid var(--signal); outline-offset: -3px; }
.preview-content { min-width: 0; }
.preview-content { flex: 1 0 auto; }
.preview-footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 18px; padding: 13px 26px; border-top: 1px solid var(--line); color: var(--muted); background: var(--ocean); font-size: 11px; }
@media (max-width: 600px) {
  .preview-heading { align-items: flex-start; flex-direction: column; padding: 18px 16px 15px; }
  h1 { font-size: 21px; }
  .preview-navigation { padding-inline: 12px; }
  .preview-navigation button { padding-inline: 12px; }
  .preview-footer { flex-direction: column; padding-inline: 16px; }
}
</style>
