import type { Component } from 'vue';

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

import ComingSoon from '../coming-soon.vue';
import Forbidden from '../forbidden.vue';
import InternalError from '../internal-error.vue';
import NotFound from '../not-found.vue';
import Offline from '../offline.vue';

const cleanups: (() => void)[] = [];
afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup());
  vi.restoreAllMocks();
});

async function mountPage(
  component: Component,
  path = '/missing-record#event-7',
) {
  const history = createMemoryHistory();
  const router = createRouter({
    history,
    routes: [{ path: '/:path(.*)*', component: { template: '<div />' } }],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(component);
  app.use(router).mount(element);
  cleanups.push(() => {
    app.unmount();
    element.remove();
  });
  await nextTick();
  return { element, history, router };
}

describe('system page recovery contract', () => {
  it.each([
    [NotFound, '找不到这个页面'],
    [Forbidden, '当前账户无权访问'],
    [InternalError, '页面暂时无法读取'],
    [Offline, '暂时无法连接服务'],
    [ComingSoon, '此功能尚未开放'],
  ] as const)(
    'shows a specific reason and safe demo preview link',
    async (page, title) => {
      const { element } = await mountPage(page);
      expect(element.querySelector('h1')?.textContent).toBe(title);
      expect(
        element.querySelector('a[href="/quant/demo-preview"]')?.textContent,
      ).toContain('本地演示');
      expect(element.querySelector('form')).toBeNull();
      expect(
        element.querySelector('main')?.getAttribute('aria-labelledby'),
      ).toBeTruthy();
    },
  );

  it.each([InternalError, Offline])(
    'reloads the same route only after explicit retry',
    async (page) => {
      const { element, router } = await mountPage(page);
      const retry = vi.spyOn(router, 'go').mockImplementation(() => {});
      expect(retry).not.toHaveBeenCalled();
      const button = element.querySelector<HTMLButtonElement>(
        '[data-action="retry"]',
      );
      expect(button?.textContent).toContain('重试');
      button?.click();
      expect(retry).toHaveBeenCalledExactlyOnceWith(0);
      expect(router.currentRoute.value.fullPath).toBe(
        '/missing-record#event-7',
      );
    },
  );

  it('returns to the demo preview when no same-tab history is known', async () => {
    const { element, router } = await mountPage(NotFound);
    const back = vi.spyOn(router, 'back');
    element.querySelector<HTMLButtonElement>('[data-action="back"]')?.click();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(back).not.toHaveBeenCalled();
    expect(router.currentRoute.value.path).toBe('/quant/demo-preview');
  });

  it('returns through the router only when same-tab history is known', async () => {
    const { element, history, router } = await mountPage(Forbidden);
    history.replace(history.location, {
      position: 1,
      back: '/quant/experiments',
    });
    const back = vi.spyOn(router, 'back').mockImplementation(() => {});
    element.querySelector<HTMLButtonElement>('[data-action="back"]')?.click();
    expect(back).toHaveBeenCalledOnce();
  });

  it('keeps a fresh external arrival in the application even with a positive browser position', async () => {
    const { element, history, router } = await mountPage(NotFound);
    history.replace(history.location, { position: 3, back: null });
    const back = vi.spyOn(router, 'back').mockImplementation(() => {});
    element.querySelector<HTMLButtonElement>('[data-action="back"]')?.click();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(back).not.toHaveBeenCalled();
    expect(router.currentRoute.value.path).toBe('/quant/demo-preview');
  });

  it('shows path and hash as text without reflecting arbitrary query reasons', async () => {
    const { element } = await mountPage(
      InternalError,
      '/records/missing?reason=%3Cimg%20src=x%3E#fill-9',
    );
    expect(element.querySelector('[data-current-location]')?.textContent).toBe(
      '/records/missing#fill-9',
    );
    expect(element.textContent).not.toContain('<img');
    expect(element.querySelector('img')).toBeNull();
  });
});
