import { reactive } from 'vue';

interface PasswordDependencies {
  username: () => string;
  change: (oldPassword: string, newPassword: string) => Promise<unknown>;
  reset: (username: string) => Promise<unknown>;
  changed: () => void;
  recovered?: () => void;
  copy: (value: string) => Promise<void>;
}

function statusOf(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return;
  const response = (error as { response?: { status?: unknown } }).response;
  return typeof response?.status === 'number' ? response.status : undefined;
}

/** 密码仅驻留当前组件内存；请求完成不写日志、URL 或浏览器存储。 */
export function createPasswordActions(dependencies: PasswordDependencies) {
  const state = reactive({
    form: { oldPassword: '', newPassword: '', confirm: '' },
    busy: false,
    changed: false,
    resetCompleted: false,
    copied: false,
    error: '',
    generatedPassword: '',
  });
  let active = true;
  let operation = 0;
  const clearForm = () => {
    state.form.oldPassword = '';
    state.form.newPassword = '';
    state.form.confirm = '';
  };
  const clearGeneratedPassword = () => {
    state.generatedPassword = '';
    state.copied = false;
  };

  async function changePassword() {
    if (!active || state.busy || state.changed || state.resetCompleted) return;
    state.error = '';
    const { oldPassword, newPassword, confirm } = state.form;
    const username = dependencies.username();
    if (!dependencies.username())
      state.error = '当前登录身份未知，请重新登录后修改。';
    else if (!oldPassword) state.error = '请输入当前密码。';
    else if (newPassword.length < 6) state.error = '新密码至少 6 位。';
    else if (oldPassword === newPassword)
      state.error = '新密码不能与当前密码相同。';
    else if (newPassword !== confirm) state.error = '两次输入的新密码不一致。';
    if (state.error) return;
    state.busy = true;
    const current = ++operation;
    try {
      await dependencies.change(oldPassword, newPassword);
      if (!active || operation !== current) return;
      if (dependencies.username() !== username) {
        clearForm();
        return;
      }
      clearForm();
      clearGeneratedPassword();
      state.changed = true;
      dependencies.changed();
    } catch (error) {
      if (!active || operation !== current) return;
      if (dependencies.username() !== username) return;
      const status = statusOf(error);
      state.error =
        status === 401 || status === 409
          ? '登录状态或密码已变化，请重新登录。'
          : status === 400
            ? '修改未完成，请检查当前密码与新密码规则。'
            : '未收到修改成功确认，请检查连接后重新登录核验密码状态。';
    } finally {
      if (active && operation === current) state.busy = false;
    }
  }

  async function resetPassword() {
    if (!active || state.busy || state.changed || state.resetCompleted) return;
    state.error = '';
    const username = dependencies.username();
    if (!username) {
      state.error = '当前登录身份未知，请重新登录后恢复。';
      return;
    }
    state.busy = true;
    clearGeneratedPassword();
    const current = ++operation;
    try {
      const result = await dependencies.reset(username);
      if (!active || operation !== current) return;
      if (dependencies.username() !== username) {
        clearForm();
        return;
      }
      if (!result || typeof result !== 'object')
        throw new Error('invalid reset result');
      const data = result as Record<string, unknown>;
      if (
        data.username !== username ||
        data.generated !== true ||
        typeof data.password !== 'string' ||
        data.password.length < 6
      )
        throw new Error('invalid reset result');
      clearForm();
      state.generatedPassword = data.password;
      state.resetCompleted = true;
      dependencies.recovered?.();
    } catch (error) {
      if (!active || operation !== current) return;
      if (dependencies.username() !== username) return;
      state.error =
        statusOf(error) === 403
          ? '恢复密码仅允许从认证服务所在的本机操作。'
          : '未收到有效恢复结果，请在本机核验账户后重新登录；请勿连续重试。';
    } finally {
      if (active && operation === current) state.busy = false;
    }
  }

  async function copyGeneratedPassword() {
    if (!active || !state.generatedPassword) return;
    const password = state.generatedPassword;
    try {
      await dependencies.copy(password);
      if (active && state.generatedPassword === password) state.copied = true;
    } catch {
      if (active) state.error = '无法访问剪贴板，请手动选择并保存当前密码。';
    }
  }

  function dispose() {
    active = false;
    operation++;
    clearForm();
    clearGeneratedPassword();
    state.error = '';
  }

  return {
    state,
    changePassword,
    resetPassword,
    copyGeneratedPassword,
    clearGeneratedPassword,
    dispose,
  };
}
