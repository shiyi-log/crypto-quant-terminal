<script lang="ts" setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { resetPasswordApi } from '#/api/core/auth';
import { FT_SESSION_EXPIRED_KEY } from '#/api/session';
import { useAuthStore } from '#/store';

defineOptions({ name: 'Login' });
const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const username = ref('admin');
const password = ref('');
const pending = ref(false);
const autoLoading = ref(false);
const error = ref(
  sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)
    ? '登录已失效，请重新登录'
    : '',
);
const showReset = ref(route.query.recovery === 'local');
const resetUser = ref('admin');
const resetLoading = ref(false);
const resetError = ref('');
const resetResult = ref<null | { password: string }>(null);
const busy = computed(
  () => pending.value || autoLoading.value || resetLoading.value,
);
let alive = true;
let generation = 0;

function errorText(reason: unknown, fallback: string) {
  // 只展示已知安全错误，不把上游诊断、URL 或凭据原样带入页面。
  if (reason && typeof reason === 'object' && 'response' in reason) {
    const response = (
      reason as { response?: { data?: { detail?: unknown }; status?: unknown } }
    ).response;
    if (response?.status === 403) return '此操作仅允许从运行终端的本机发起';
    if (response?.status === 404) return '未找到该用户名，请核对账户';
    if (response?.data?.detail === '用户名或密码错误')
      return '用户名或密码错误';
  }
  if (reason instanceof Error && reason.message === '用户名或密码错误')
    return '用户名或密码错误';
  return fallback;
}

async function submitLogin() {
  if (busy.value) return;
  error.value = '';
  if (!username.value.trim() || !password.value) {
    error.value = '请输入用户名和密码';
    return;
  }
  pending.value = true;
  const current = generation;
  try {
    // 会话建立、原目标返回和过期状态由既有 store 与路由守卫统一处理。
    await authStore.authLogin({
      username: username.value,
      password: password.value,
    });
  } catch (reason) {
    if (alive && current === generation)
      error.value = errorText(reason, '登录未完成，请核对认证服务后重试');
  } finally {
    if (alive) {
      password.value = '';
      pending.value = false;
    }
  }
}

async function localLogin(explicit: boolean) {
  if (busy.value) return;
  autoLoading.value = true;
  const current = generation;
  if (explicit) {
    error.value = '';
    sessionStorage.removeItem('ft_manual_logout');
  }
  try {
    await authStore.authLogin({ auto: true });
  } catch {
    if (alive && current === generation && explicit) {
      error.value = '本机免密登录不可用，请使用用户名和密码';
      sessionStorage.setItem('ft_manual_logout', '1');
    }
  } finally {
    if (alive) autoLoading.value = false;
  }
}

function openRecovery() {
  if (busy.value) return;
  password.value = '';
  resetResult.value = null;
  resetError.value = '';
  resetUser.value = username.value || 'admin';
  showReset.value = true;
  void router.replace({
    path: route.path,
    query: { ...route.query, recovery: 'local' },
  });
}
function closeRecovery() {
  if (resetLoading.value) return;
  // 恢复密码仅停留在本次结果视图，离开后不继续保留。
  resetResult.value = null;
  resetError.value = '';
  showReset.value = false;
  const query = { ...route.query };
  delete query.recovery;
  void router.replace({ path: route.path, query });
}
async function resetLocalPassword() {
  if (busy.value) return;
  resetError.value = '';
  resetResult.value = null;
  if (!resetUser.value.trim()) {
    resetError.value = '请输入需要恢复的用户名';
    return;
  }
  resetLoading.value = true;
  const current = generation;
  const requestedUser = resetUser.value.trim();
  try {
    const result = await resetPasswordApi(requestedUser);
    if (!alive || current !== generation) return;
    if (
      result?.username !== requestedUser ||
      result?.generated !== true ||
      typeof result?.password !== 'string' ||
      result.password.length < 1 ||
      result.password.length > 256 ||
      /[\u0000-\u001f\u007f]/.test(result.password)
    ) {
      resetError.value = '无法确认恢复结果，请核对账户状态；恢复请求可能已执行';
      return;
    }
    resetResult.value = { password: result.password };
  } catch (reason) {
    if (alive && current === generation)
      resetError.value = errorText(
        reason,
        '恢复未完成，请核对本机认证服务与账户状态',
      );
  } finally {
    if (alive) resetLoading.value = false;
  }
}

onMounted(() => {
  // 主动退出、会话失效与恢复入口均不会被自动登录覆盖。
  if (
    showReset.value ||
    sessionStorage.getItem('ft_manual_logout') ||
    sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)
  )
    return;
  void localLogin(false);
});

watch(
  () => route.fullPath,
  () => {
    generation += 1;
    password.value = '';
    resetResult.value = null;
    resetError.value = '';
    const nextRecovery = route.query.recovery === 'local';
    if (!nextRecovery || !showReset.value) resetUser.value = 'admin';
    showReset.value = nextRecovery;
  },
);
onBeforeUnmount(() => {
  alive = false;
  generation += 1;
  password.value = '';
  resetResult.value = null;
  resetError.value = '';
});
</script>

<template>
  <section class="access-panel" aria-labelledby="access-title">
    <header>
      <p class="access-context">量化交易终端</p>
      <h1 id="access-title">{{ showReset ? '本机恢复密码' : '登录工作站' }}</h1>
      <p class="access-description">
        {{
          showReset
            ? '由本机认证服务生成随机新密码。不会发送邮件，也不改变交易账户凭据。'
            : '登录后查看账户风险、研究任务与运行证据。交易环境以账户事实为准。'
        }}
      </p>
    </header>
    <p v-if="autoLoading" class="access-notice" role="status">
      正在验证本机访问…
    </p>
    <p v-if="error && !showReset" class="access-error" role="alert">
      {{ error }}
    </p>
    <template v-if="!showReset">
      <form aria-label="用户名密码登录" @submit.prevent="submitLogin">
        <label for="workstation-username">用户名</label>
        <input
          id="workstation-username"
          v-model="username"
          name="username"
          autocomplete="username"
          required
          :disabled="busy"
        />
        <label for="workstation-password">密码</label>
        <input
          id="workstation-password"
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
          :disabled="busy"
        />
        <button class="primary-action" type="submit" :disabled="busy">
          {{ pending ? '正在登录…' : '登录' }}
        </button>
      </form>
      <div class="access-secondary">
        <button type="button" :disabled="busy" @click="localLogin(true)">
          本机免密登录
        </button>
        <button type="button" :disabled="busy" @click="openRecovery">
          本机恢复密码
        </button>
      </div>
      <p class="access-footnote">
        本机入口由服务端核验访问来源；远程访问请使用用户名和密码。
      </p>
    </template>
    <template v-else>
      <p class="recovery-warning">
        恢复后旧密码失效。请保存生成结果，再返回登录。
      </p>
      <p v-if="resetError" class="access-error" role="alert">
        {{ resetError }}
      </p>
      <form aria-label="本机恢复密码" @submit.prevent="resetLocalPassword">
        <label for="recovery-username">需要恢复的用户名</label>
        <input
          id="recovery-username"
          v-model="resetUser"
          name="recovery-username"
          autocomplete="username"
          required
          :disabled="resetLoading"
        />
        <button class="primary-action" type="submit" :disabled="busy">
          {{ resetLoading ? '正在生成…' : '生成随机新密码' }}
        </button>
      </form>
      <section
        v-if="resetResult"
        class="recovery-result"
        aria-label="本次恢复结果"
        role="status"
      >
        <h2>新密码已生成</h2>
        <p>仅在当前页面显示，请妥善保存。</p>
        <output>{{ resetResult.password }}</output>
      </section>
      <button
        class="return-action"
        type="button"
        :disabled="resetLoading"
        @click="closeRecovery"
      >
        返回登录
      </button>
    </template>
  </section>
</template>

<style scoped>
.access-panel {
  width: 100%;
  min-width: 0;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif;
}
.access-panel * {
  box-sizing: border-box;
}
h1,
h2,
p {
  margin: 0;
}
.access-context {
  color: #69b4e4;
  font-size: 13px;
}
h1 {
  margin-top: 10px;
  font-size: clamp(25px, 4vw, 32px);
  font-weight: 600;
  line-height: 1.3;
}
.access-description {
  margin: 15px 0 30px;
  color: #a2b8c9;
  font-size: 14px;
  line-height: 1.8;
}
form {
  display: grid;
  gap: 10px;
}
label {
  margin-top: 9px;
  font-size: 13px;
  font-weight: 500;
}
input {
  width: 100%;
  min-width: 0;
  height: 44px;
  padding: 10px 12px;
  border: 1px solid #49677c;
  border-radius: 3px;
  background: #0c1b2a;
  color: #e5edf4;
  font: inherit;
}
button {
  min-height: 42px;
  border: 1px solid #49677c;
  border-radius: 3px;
  padding: 9px 14px;
  background: #132b3e;
  color: #e5edf4;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
button:disabled {
  cursor: wait;
  opacity: 0.6;
}
button:hover:not(:disabled) {
  border-color: #69b4e4;
}
input:focus-visible,
button:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
.primary-action {
  margin-top: 16px;
  background: #69b4e4;
  border-color: #69b4e4;
  color: #0c1b2a;
  font-weight: 600;
}
.access-secondary {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding-top: 24px;
  margin-top: 24px;
  border-top: 1px solid #29485c;
}
.access-footnote {
  margin-top: 16px;
  font-size: 12px;
  color: #a2b8c9;
  line-height: 1.7;
}
.access-error,
.access-notice,
.recovery-warning {
  padding: 12px;
  border-left: 3px solid #f19a9a;
  background: #132b3e;
  color: #f5b7b7;
  line-height: 1.7;
  font-size: 13px;
  margin-bottom: 18px;
  overflow-wrap: anywhere;
}
.access-notice {
  border-color: #69b4e4;
  color: #a2b8c9;
}
.recovery-warning {
  border-color: #f0c17b;
  color: #f0c17b;
}
.recovery-result {
  margin: 22px 0;
  padding: 16px;
  border: 1px solid #49677c;
  background: #132b3e;
}
.recovery-result h2 {
  font-size: 15px;
}
.recovery-result p {
  margin-top: 6px;
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.7;
}
.recovery-result output {
  display: block;
  margin-top: 12px;
  font-family: 'IBM Plex Mono', monospace;
  overflow-wrap: anywhere;
  user-select: all;
}
.return-action {
  width: 100%;
  margin-top: 16px;
}
@media (max-width: 420px) {
  .access-secondary {
    grid-template-columns: 1fr;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    scroll-behavior: auto;
  }
}
</style>
