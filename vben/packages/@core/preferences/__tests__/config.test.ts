import { afterEach, describe, expect, it, vi } from 'vitest';

import { defaultPreferences } from '../src/config';
import { PreferenceManager } from '../src/preferences';

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('defaultPreferences', () => {
  it('keeps application overrides and resets from mutating the shared defaults', async () => {
    vi.useFakeTimers();
    const baseline = JSON.parse(JSON.stringify(defaultPreferences));
    const manager = new PreferenceManager();

    // 首页属于应用选择；修改和恢复偏好不应污染下一实例的默认配置。
    manager.updatePreferences({
      app: { defaultHomePath: '/quant/trade-desk' },
      widget: { order: ['fixture-only'] },
    });

    expect(manager.getPreferences().app.defaultHomePath).toBe(
      '/quant/trade-desk',
    );
    expect(manager.getPreferences().widget.order).toEqual(['fixture-only']);
    expect(defaultPreferences).toEqual(baseline);

    await manager.resetPreferences();

    expect(manager.getPreferences()).toEqual(baseline);
    expect(defaultPreferences).toEqual(baseline);
    expect(new PreferenceManager().getPreferences()).toEqual(baseline);
  });

  it('uses an internal absolute fallback route', () => {
    // 禁止默认导航变成外站跳转；产品首页由应用的路由和登录流程验收。
    expect(defaultPreferences.app.defaultHomePath).toMatch(/^\/(?!\/)[^?#]+$/);
  });
});
