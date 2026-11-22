import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';

const { changePasswordApi } = vi.hoisted(() => ({
  changePasswordApi: vi.fn(),
}));

vi.mock('#/api/core/auth', () => ({ changePasswordApi }));

import PasswordSetting from '../password-setting.vue';

describe('个人资料密码设置', () => {
  let element: HTMLDivElement;
  let app: ReturnType<typeof createApp>;

  beforeEach(() => {
    changePasswordApi.mockReset();
    element = document.createElement('div');
    document.body.append(element);
    app = createApp(PasswordSetting);
    app.mount(element);
  });

  afterEach(() => {
    app.unmount();
    element.remove();
  });

  function input(name: string, value: string) {
    const field = element.querySelector<HTMLInputElement>(
      '[name="' + name + '"]',
    );
    if (!field) throw new Error('Missing input ' + name);
    field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
  }

  async function submit() {
    element
      .querySelector('form')
      ?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await nextTick();
  }

  it('拒绝不符合规则的密码，且不发送请求', async () => {
    input('oldPassword', 'current');
    input('newPassword', 'short');
    input('confirmPassword', 'short');
    await submit();

    expect(element.textContent).toContain('新密码至少 6 位');
    expect(changePasswordApi).not.toHaveBeenCalled();
  });

  it('修改成功后清空密码并显示重新登录提示', async () => {
    changePasswordApi.mockResolvedValueOnce({ message: '密码已更新' });
    input('oldPassword', 'current-secret');
    input('newPassword', 'new-secret');
    input('confirmPassword', 'new-secret');
    await submit();
    await nextTick();

    expect(changePasswordApi).toHaveBeenCalledWith(
      'current-secret',
      'new-secret',
    );
    expect(element.querySelector('form')).toBeNull();
    expect(element.textContent).toContain('密码已修改');
    expect(element.textContent).not.toContain('current-secret');
    expect(element.textContent).not.toContain('new-secret');
  });
});
