import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';

const api = vi.hoisted(() => ({
  getModelVersions: vi.fn(),
  promoteModelVersion: vi.fn(),
}));
vi.mock('#/api/freqtrade', () => api);

import VersionRegistryWorkspace from '../VersionRegistryWorkspace.vue';

const candidate = (id = 'candidate-a', passed = false) => ({
  id,
  layer: 'ml_model',
  status: 'challenger',
  label: '候选模型',
  round: 9,
  metrics: { ic_period: 0, t_period: null },
  gate: { passed, reasons: ['样本不足'], criteria: '逐窗口检验' },
  deployed: null,
  artifacts: [],
  notes: null,
});
const registry = () => ({
  updated: null,
  running: null,
  trials: { total: 3, runs: 2, best: null, recent: [] },
  layers: {
    live_strategy: { champion: null, challengers: [], archived: [] },
    ml_model: {
      champion: null,
      challengers: [candidate()],
      archived: [{ ...candidate('archived-a'), status: 'archived' }],
    },
  },
});
const mounted: { app: App; element: HTMLDivElement }[] = [];
function get<T extends HTMLElement = HTMLElement>(
  element: Element,
  selector: string,
) {
  const item = element.querySelector<T>(selector);
  if (!item) throw new Error('Missing rendered element ' + selector);
  return item;
}
async function open() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(VersionRegistryWorkspace);
  app.mount(element);
  mounted.push({ app, element });
  await vi.waitFor(() => expect(api.getModelVersions).toHaveBeenCalled());
  await vi.waitFor(() => expect(element.textContent).toContain('candidate-a'));
  return { element };
}
afterEach(() => {
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
  vi.resetAllMocks();
});

describe('版本登记工作区', () => {
  it('显示真实零值和未知值，选择不会自动登记', async () => {
    api.getModelVersions.mockResolvedValue(registry());
    const { element } = await open();
    expect(api.getModelVersions.mock.results[0]?.value).toBeDefined();
    expect(element.textContent).toContain('ic_period: 0');
    expect(element.textContent).toContain('t_period: 未知');
    expect(api.promoteModelVersion).not.toHaveBeenCalled();
    get(element, 'button.version-link').click();
    await nextTick();
    expect(element.textContent).toContain('所选版本');
    expect(api.promoteModelVersion).not.toHaveBeenCalled();
  });

  it('失败刷新保留旧快照且错误文案不泄漏请求详情', async () => {
    api.getModelVersions
      .mockResolvedValueOnce(registry())
      .mockRejectedValueOnce(new Error('postgresql://user:secret@host/db'));
    const { element } = await open();
    get(element, '[data-testid="refresh-version-registry"]').click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('保留上次成功快照'),
    );
    expect(element.textContent).toContain('candidate-a');
    expect(element.textContent).not.toContain('secret');
  });

  it('未通过判据需理由和显式确认，之后才发送登记请求', async () => {
    api.getModelVersions.mockResolvedValue(registry());
    api.promoteModelVersion.mockResolvedValue({
      status: 'ok',
      id: 'candidate-a',
    });
    const { element } = await open();
    get(element, 'tbody tr:first-child td:last-child button').click();
    await nextTick();
    expect(element.textContent).toContain('未通过冻结判据');
    get(element, '[data-testid="confirm-version-promotion"]').click();
    await nextTick();
    expect(api.promoteModelVersion).not.toHaveBeenCalled();
    const note = get<HTMLTextAreaElement>(element, 'textarea');
    note.value = '人工复核样本';
    note.dispatchEvent(new Event('input', { bubbles: true }));
    const confirm = get<HTMLInputElement>(element, '.confirm-unpassed input');
    confirm.checked = true;
    confirm.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    get(element, '[data-testid="confirm-version-promotion"]').click();
    await vi.waitFor(() =>
      expect(element.textContent).toContain('仅更新版本注册表'),
    );
    expect(api.promoteModelVersion).toHaveBeenCalledWith(
      'candidate-a',
      '人工复核样本',
      true,
    );
    expect(element.textContent).toContain('不修改部署配置，也不创建或改变订单');
  });
});
