<script setup lang="ts">
import { computed, ref } from 'vue';

import { changePasswordApi } from '#/api/core/auth';

const oldPassword = ref('');
const newPassword = ref('');
const confirmPassword = ref('');
const busy = ref(false);
const error = ref('');
const changed = ref(false);

const canSubmit = computed(
  () =>
    !busy.value &&
    !changed.value &&
    oldPassword.value.length > 0 &&
    newPassword.value.length >= 6 &&
    confirmPassword.value.length > 0,
);

function clearForm() {
  oldPassword.value = '';
  newPassword.value = '';
  confirmPassword.value = '';
}

async function submit() {
  error.value = '';
  if (!oldPassword.value) {
    error.value = '请输入当前密码。';
    return;
  }
  if (newPassword.value.length < 6) {
    error.value = '新密码至少 6 位。';
    return;
  }
  if (oldPassword.value === newPassword.value) {
    error.value = '新密码不能与当前密码相同。';
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = '两次输入的新密码不一致。';
    return;
  }

  busy.value = true;
  try {
    await changePasswordApi(oldPassword.value, newPassword.value);
    clearForm();
    changed.value = true;
  } catch (cause) {
    const status =
      cause && typeof cause === 'object' && 'response' in cause
        ? (cause as { response?: { status?: number } }).response?.status
        : undefined;
    error.value =
      status === 401 || status === 409
        ? '登录状态或当前密码已变化，请重新登录。'
        : '修改未完成，请检查当前密码与连接状态。';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section aria-labelledby="profile-password-heading">
    <h2 id="profile-password-heading">修改密码</h2>
    <p class="identity-help">需要验证当前密码，成功后请重新登录。</p>
    <p v-if="error" class="identity-message identity-error" role="alert">
      {{ error }}
    </p>
    <div v-if="changed" class="identity-message" role="status">
      <strong>密码已修改</strong>
      <p>请退出当前会话，再使用新密码登录。</p>
    </div>
    <form v-else aria-label="修改密码" novalidate @submit.prevent="submit">
      <label>
        当前密码
        <input
          v-model="oldPassword"
          :disabled="busy"
          autocomplete="current-password"
          name="oldPassword"
          type="password"
        />
      </label>
      <label>
        新密码
        <input
          v-model="newPassword"
          :disabled="busy"
          autocomplete="new-password"
          name="newPassword"
          type="password"
        />
      </label>
      <p class="identity-help">至少 6 位，不能与当前密码相同。</p>
      <label>
        确认新密码
        <input
          v-model="confirmPassword"
          :disabled="busy"
          autocomplete="new-password"
          name="confirmPassword"
          type="password"
        />
      </label>
      <button :disabled="!canSubmit" type="submit">
        {{ busy ? '正在确认…' : '确认修改密码' }}
      </button>
    </form>
  </section>
</template>
