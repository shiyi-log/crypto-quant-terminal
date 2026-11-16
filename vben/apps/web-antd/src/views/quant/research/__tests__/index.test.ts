import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';
import ResearchWorkspace from '../index.vue';

const api = vi.hoisted(() => ({ research: vi.fn(), status: vi.fn() }));
vi.mock('#/api/freqtrade', () => ({
  getResearch: api.research,
  getResearchStatus: api.status,
}));
const fixture = () => ({
  generated_at: '2026-11-16 10:00:00',
  routes: [
    {
      name: 'Carry',
      level: 'good',
      evidence: '资金费历史研究',
      annual: '3%',
      verdict: '推荐',
    },
    {
      name: 'FreqAI',
      level: 'bad',
      evidence: '样本外亏损',
      annual: '-20%',
      verdict: '已证伪',
    },
  ],
  models: [
    { identifier: 'model-1', label: '模型一', trades: 10, profit_pct: null },
  ],
  carry: {
    cash: { net_pct: -0.25, yearly: [] },
    sweep: [],
    liquidation: [],
    buffers: [],
    schemes: [],
  },
});
const mounts: { app: App; el: HTMLDivElement }[] = [];
async function open(path = '/quant/research') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/quant/research', component: ResearchWorkspace },
      { path: '/quant/strategy-lab', component: { template: '<p>paper</p>' } },
      { path: '/quant/experiments', component: { template: '<p>runs</p>' } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const el = document.createElement('div');
  document.body.append(el);
  const app = createApp(ResearchWorkspace);
  app.use(router).mount(el);
  mounts.push({ app, el });
  await vi.waitFor(() =>
    expect(el.querySelector('[aria-label="研究内容"]')?.textContent).toContain(
      '资金费历史研究',
    ),
  );
  return { el, router };
}
function button(el: Element, text: string) {
  const found = [...el.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === text,
  );
  if (!found) throw new Error('button unavailable: ' + text);
  return found;
}
beforeEach(() => {
  api.research.mockReset().mockResolvedValue(fixture());
  api.status.mockReset().mockResolvedValue({ ok: true });
});
afterEach(() => {
  mounts.splice(0).forEach(({ app, el }) => {
    app.unmount();
    el.remove();
  });
});

describe('strategy research workspace behavior', () => {
  it('keeps historical recommendation separate from cost and approval and switches evidence candidate', async () => {
    const { el, router } = await open();
    expect(el.textContent).toContain('尚无 Demo 准入裁定');
    expect(
      [...el.querySelectorAll('a')].map((link) => link.getAttribute('href')),
    ).toEqual(['/quant/strategy-lab', '/quant/experiments']);
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('Carry');
    button(el, 'FreqAI').click();
    await nextTick();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.query.candidate).toBe('FreqAI'),
    );
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('样本外亏损');
  });
  it('preserves known evidence after a malformed refresh and exposes a source failure', async () => {
    const { el } = await open();
    api.research.mockResolvedValueOnce({ routes: [] });
    button(el, '刷新研究证据').click();
    await vi.waitFor(() =>
      expect(el.querySelector('[role="alert"]')?.textContent).toContain('保留'),
    );
    expect(el.textContent).toContain('资金费历史研究');
  });
  it('uses exact candidate deep links and reports missing records without selecting another route', async () => {
    const { el, router } = await open('/quant/research?candidate=absent');
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('未找到指定研究');
    await router.push('/quant/research?candidate=FreqAI');
    await nextTick();
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('样本外亏损');
  });
  it('makes all historical sections available and preserves unknown model metrics', async () => {
    const { el } = await open();
    button(el, '模型对比').click();
    await nextTick();
    expect(el.querySelector('[aria-label="研究内容"]')?.textContent).toContain(
      '模型一',
    );
    expect(el.querySelector('[aria-label="研究内容"]')?.textContent).toContain(
      '未知',
    );
    button(el, 'Carry 拆解').click();
    await nextTick();
    expect(el.querySelector('[aria-label="研究内容"]')?.textContent).toContain(
      '-0.25',
    );
    for (const title of ['强平情景', '参数扫描', '保证金缓冲', '保证金方案']) {
      button(el, title).click();
      await nextTick();
      expect(
        el.querySelector('[aria-label="研究内容"]')?.textContent,
      ).toContain('无');
    }
  });
  it('does not replace an empty or repeated candidate identity with the first record', async () => {
    const { el, router } = await open('/quant/research?candidate=');
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('研究身份无效');
    await router.push('/quant/research?candidate=Carry&candidate=FreqAI');
    await nextTick();
    expect(
      el.querySelector('[aria-label="所选研究证据"]')?.textContent,
    ).toContain('研究身份无效');
  });
  it('keeps the newer successful snapshot when a prior refresh completes later', async () => {
    const { el } = await open();
    let resolveOld!: (value: ReturnType<typeof fixture>) => void;
    api.research.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    el.querySelector<HTMLButtonElement>('.research-heading button')!.click();
    await nextTick();
    const newer = fixture();
    newer.routes[0]!.evidence = '第二次完整来源';
    api.research.mockResolvedValueOnce(newer);
    el.querySelector<HTMLButtonElement>('.research-heading button')!.click();
    await vi.waitFor(() => expect(el.textContent).toContain('第二次完整来源'));
    const older = fixture();
    older.routes[0]!.evidence = '已经失效的旧来源';
    resolveOld(older);
    await Promise.resolve();
    await nextTick();
    expect(el.textContent).toContain('第二次完整来源');
    expect(el.textContent).not.toContain('已经失效的旧来源');
  });
  it('returns to the default evidence board when the historical section query is removed', async () => {
    const { el, router } = await open();
    await router.push('/quant/research?section=models');
    await nextTick();
    expect(button(el, '模型对比').getAttribute('aria-pressed')).toBe('true');
    await router.push('/quant/research');
    await nextTick();
    expect(button(el, '研究方向').getAttribute('aria-pressed')).toBe('true');
    await router.push('/quant/research?section=models&section=carry');
    await nextTick();
    expect(button(el, '研究方向').getAttribute('aria-pressed')).toBe('true');
    expect(el.querySelector('[role="alert"]')?.textContent).toContain(
      '板块定位无效',
    );
  });
  it('retains known Carry evidence when a success response loses its required arrays', async () => {
    const initial = fixture();
    initial.carry.sweep = [{ total_pct: 12.34 }] as never[];
    api.research.mockResolvedValueOnce(initial);
    const { el } = await open();
    button(el, '参数扫描').click();
    await nextTick();
    expect(el.textContent).toContain('12.34');
    api.research.mockResolvedValueOnce({ ...fixture(), carry: {} });
    button(el, '刷新研究证据').click();
    await vi.waitFor(() =>
      expect(el.querySelector('[role="alert"]')?.textContent).toContain('保留'),
    );
    expect(el.textContent).toContain('12.34');
  });
  it('shows unavailable Carry evidence separately from a successful empty scenario list', async () => {
    api.research.mockResolvedValueOnce({ ...fixture(), carry: null });
    const { el } = await open();
    button(el, '参数扫描').click();
    await nextTick();
    expect(el.querySelector('[aria-label="研究内容"]')?.textContent).toContain(
      'Carry 证据尚未提供',
    );
    expect(
      el.querySelector('[aria-label="研究内容"]')?.textContent,
    ).not.toContain('无此板块记录');
  });
});
