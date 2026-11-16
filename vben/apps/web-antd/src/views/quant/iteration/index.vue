<script setup lang="ts">
import { computed, ref } from 'vue';

import IterationAnalysis from './IterationAnalysis.vue';
import IterationRuns from './IterationRuns.vue';
import ResearchLedger from './ResearchLedger.vue';
import VersionRegistryWorkspace from './VersionRegistryWorkspace.vue';

const sections = [
  { id: 'runs', label: '运行队列' },
  { id: 'analysis', label: '走查分析' },
  { id: 'versions', label: '版本登记' },
  { id: 'ledger', label: '研究证据账' },
] as const;
type SectionId = (typeof sections)[number]['id'];
const activeSection = ref<SectionId>('runs');
const activeLabel = computed(
  () => sections.find((section) => section.id === activeSection.value)?.label,
);
function chooseSection(section: SectionId) {
  activeSection.value = section;
}
</script>

<template>
  <main class="iteration-workspace" aria-labelledby="iteration-title">
    <header class="workspace-heading">
      <div>
        <p class="workspace-kicker">研究记录 / 历史工作区</p>
        <h1 id="iteration-title">模型迭代</h1>
        <p>查看执行记录、走查结果、版本登记与可追溯研究证据。</p>
      </div>
      <span class="active-section">{{ activeLabel }}</span>
    </header>

    <nav class="workspace-nav" aria-label="迭代工作区">
      <button
        v-for="section in sections"
        :key="section.id"
        :aria-current="activeSection === section.id ? 'page' : undefined"
        :aria-pressed="activeSection === section.id"
        type="button"
        @click="chooseSection(section.id)"
      >
        {{ section.label }}
      </button>
    </nav>

    <div class="workspace-content">
      <section v-if="activeSection === 'runs'" data-workspace="runs">
        <IterationRuns />
      </section>
      <section
        v-else-if="activeSection === 'analysis'"
        data-workspace="analysis"
      >
        <IterationAnalysis />
      </section>
      <section
        v-else-if="activeSection === 'versions'"
        data-workspace="versions"
      >
        <VersionRegistryWorkspace />
      </section>
      <section v-else data-workspace="ledger">
        <ResearchLedger />
      </section>
    </div>
  </main>
</template>

<style scoped>
.iteration-workspace {
  --iteration-ink: #e5edf4;
  --iteration-muted: #a2b8c9;
  --iteration-line: #29485c;
  --iteration-surface: #132b3e;
  --iteration-base: #0c1b2a;
  --iteration-accent: #69b4e4;
  min-width: 0;
  color: var(--iteration-ink);
}

.workspace-heading {
  display: flex;
  min-width: 0;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
  border-bottom: 1px solid var(--iteration-line);
  background: transparent;
}

.workspace-kicker {
  margin: 0 0 5px;
  color: var(--iteration-accent);
  font-size: 12px;
}

.workspace-heading h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
}

.workspace-heading p:last-child {
  margin: 6px 0 0;
  color: var(--iteration-muted);
  font-size: 13px;
}

.active-section {
  flex: 0 0 auto;
  color: var(--iteration-muted);
  font-size: 12px;
}

.workspace-nav {
  display: flex;
  gap: 2px;
  margin: 14px 0 18px;
  border-bottom: 1px solid var(--iteration-line);
}

.workspace-nav button {
  min-height: 42px;
  padding: 0 14px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--iteration-muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.workspace-nav button[aria-pressed='true'] {
  border-bottom-color: var(--iteration-accent);
  color: var(--iteration-ink);
}

.workspace-nav button:hover {
  color: var(--iteration-ink);
}

.workspace-nav button:focus-visible {
  outline: 2px solid var(--iteration-accent);
  outline-offset: -2px;
}

.workspace-content {
  min-width: 0;
  background: var(--iteration-surface);
  border-top: 2px solid var(--iteration-line);
  padding: 12px;
}

@media (max-width: 640px) {
  .workspace-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
    padding: 14px;
  }

  .workspace-nav {
    overflow-x: auto;
  }

  .workspace-nav button {
    flex: 0 0 auto;
    padding: 0 11px;
  }
}
</style>
