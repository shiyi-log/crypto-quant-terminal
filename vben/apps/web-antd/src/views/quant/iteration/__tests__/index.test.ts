import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';

vi.mock('../IterationRuns.vue', async () => {
  const { h: render } = await import('vue');
  return {
    default: { render: () => render('div', { 'data-mounted-child': 'runs' }) },
  };
});
vi.mock('../IterationAnalysis.vue', async () => {
  const { h: render } = await import('vue');
  return {
    default: {
      render: () => render('div', { 'data-mounted-child': 'analysis' }),
    },
  };
});
vi.mock('../VersionRegistryWorkspace.vue', async () => {
  const { h: render } = await import('vue');
  return {
    default: {
      render: () => render('div', { 'data-mounted-child': 'versions' }),
    },
  };
});
vi.mock('../ResearchLedger.vue', async () => {
  const { h: render } = await import('vue');
  return {
    default: {
      render: () => render('div', { 'data-mounted-child': 'ledger' }),
    },
  };
});

import IterationWorkspace from '../index.vue';

const mounted: Array<{ app: App; element: HTMLDivElement }> = [];
async function flush() {
  await nextTick();
  await nextTick();
}
async function mount() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(IterationWorkspace) });
  app.mount(element);
  mounted.push({ app, element });
  await flush();
  return element;
}
function choose(element: Element, label: string) {
  const button = [...element.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === label,
  );
  if (!button) throw new Error('Workspace button not found: ' + label);
  button.click();
}
afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});

describe('iteration history workspace shell', () => {
  it('mounts only the selected polling workspace and releases it on section change', async () => {
    const element = await mount();
    expect(element.querySelector('[data-workspace="runs"]')).not.toBeNull();
    expect(element.querySelectorAll('[data-mounted-child]').length).toBe(1);
    expect(element.querySelector('[data-mounted-child="runs"]')).not.toBeNull();
    expect(element.querySelector('[data-workspace="analysis"]')).toBeNull();
    expect(element.querySelector('[data-workspace="versions"]')).toBeNull();
    expect(element.querySelector('[data-workspace="ledger"]')).toBeNull();

    choose(element, '走查分析');
    await flush();
    expect(element.querySelector('[data-workspace="runs"]')).toBeNull();
    expect(element.querySelector('[data-workspace="analysis"]')).not.toBeNull();
    expect(element.querySelectorAll('[data-mounted-child]').length).toBe(1);
    expect(
      element.querySelector('[data-mounted-child="analysis"]'),
    ).not.toBeNull();

    choose(element, '版本登记');
    await flush();
    expect(element.querySelector('[data-workspace="analysis"]')).toBeNull();
    expect(element.querySelector('[data-workspace="versions"]')).not.toBeNull();
    expect(element.querySelectorAll('[data-mounted-child]').length).toBe(1);
    expect(
      element.querySelector('[data-mounted-child="versions"]'),
    ).not.toBeNull();

    choose(element, '研究证据账');
    await flush();
    expect(element.querySelector('[data-workspace="versions"]')).toBeNull();
    expect(element.querySelector('[data-workspace="ledger"]')).not.toBeNull();
    expect(element.querySelectorAll('[data-mounted-child]').length).toBe(1);
    expect(
      element.querySelector('[data-mounted-child="ledger"]'),
    ).not.toBeNull();
  });
});
