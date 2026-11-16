<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { changePasswordApi, resetPasswordApi } from '#/api/core/auth';

import { createPasswordActions } from './passwordActions';

const props = withDefaults(
  defineProps<{ username: string; allowRecovery?: boolean }>(),
  { allowRecovery: true },
);
const acknowledged = ref(false);
const revealed = ref(false);
let redirectTimer: ReturnType<typeof setTimeout> | undefined;
function clearLocalSession() {
  localStorage.removeItem('ft_access_token');
  sessionStorage.setItem('ft_manual_logout', '1');
}
const actions = createPasswordActions({
  username: () => props.username,
  change: changePasswordApi,
  reset: resetPasswordApi,
  copy: (value) => navigator.clipboard.writeText(value),
  changed: () => {
    // 保留改密后重新登录语义，主动退出标记防止本机自动登录立即恢复。
    clearLocalSession();
    redirectTimer = setTimeout(() => {
      location.assign('/');
    }, 1800);
  },
  recovered: clearLocalSession,
});
const state = actions.state;
const disabled = computed(
  () => state.busy || state.changed || state.resetCompleted || !props.username,
);
watch(
  () => props.username,
  () => {
    state.form.oldPassword = '';
    state.form.newPassword = '';
    state.form.confirm = '';
    actions.clearGeneratedPassword();
    revealed.value = false;
    acknowledged.value = false;
  },
);
onBeforeUnmount(() => {
  actions.dispose();
  if (redirectTimer) clearTimeout(redirectTimer);
});
function clearRecovery() {
  revealed.value = false;
  actions.clearGeneratedPassword();
}
</script>

<template>
  <div class="identity-password-workbench">
    <p v-if="state.error" class="identity-message identity-error" role="alert">
      {{ state.error }}
    </p>
    <section
      aria-labelledby="identity-change-heading"
      class="identity-action-region"
    >
      <div class="identity-section-heading">
        <h2 id="identity-change-heading">修改密码</h2>
        <span>需要当前密码</span>
      </div>
      <div v-if="state.changed" class="identity-message" role="status">
        <strong>密码已修改</strong>
        <p>即将退出当前登录，请使用新密码重新登录。</p>
        <a href="/">前往登录</a>
      </div>
      <form
        v-else
        aria-label="修改密码"
        novalidate
        @submit.prevent="actions.changePassword"
      >
        <label
          >当前密码<input
            v-model="state.form.oldPassword"
            :disabled="disabled"
            name="oldPassword"
            type="password"
            autocomplete="current-password"
        /></label>
        <label
          >新密码<input
            v-model="state.form.newPassword"
            :disabled="disabled"
            name="newPassword"
            type="password"
            autocomplete="new-password"
            aria-describedby="identity-password-rule"
        /></label>
        <p id="identity-password-rule" class="identity-help">
          至少 6 位，不能与当前密码相同。
        </p>
        <label
          >确认新密码<input
            v-model="state.form.confirm"
            :disabled="disabled"
            name="confirm"
            type="password"
            autocomplete="new-password"
        /></label>
        <button class="identity-primary" type="submit" :disabled="disabled">
          {{ state.busy ? '正在确认…' : '确认修改密码' }}
        </button>
        <p class="identity-help">
          修改成功后重新登录。页面不会保存输入的密码。
        </p>
      </form>
    </section>

    <section
      v-if="allowRecovery"
      aria-labelledby="identity-recovery-heading"
      class="identity-action-region identity-recovery"
    >
      <div class="identity-section-heading">
        <h2 id="identity-recovery-heading">本机恢复</h2>
        <span>仅限认证服务本机</span>
      </div>
      <p>
        无法提供当前密码时，可在认证服务所在的本机为
        <strong>{{ username || '身份未知' }}</strong>
        生成随机新密码。原密码会立即失效。
      </p>
      <template v-if="!state.resetCompleted">
        <label class="identity-check"
          ><input
            v-model="acknowledged"
            :disabled="disabled"
            type="checkbox"
            name="confirmRecovery"
          />我已确认正在恢复当前登录账户</label
        >
        <button
          class="identity-danger"
          :disabled="disabled || !acknowledged"
          type="button"
          @click="actions.resetPassword"
        >
          生成随机新密码
        </button>
      </template>
      <div v-else class="identity-recovery-result" role="status">
        <strong>本机密码已重置</strong>
        <p>
          请保存本次密码后重新登录。此结果只驻留当前页面，清除或离开后无法找回。
        </p>
        <template v-if="state.generatedPassword">
          <input
            :type="revealed ? 'text' : 'password'"
            :value="state.generatedPassword"
            aria-label="本次恢复密码"
            autocomplete="off"
            readonly
          />
          <div class="identity-actions">
            <button type="button" @click="revealed = !revealed">
              {{ revealed ? '隐藏本次密码' : '显示本次密码' }}
            </button>
            <button type="button" @click="actions.copyGeneratedPassword">
              {{ state.copied ? '已复制' : '复制本次密码' }}
            </button>
            <button type="button" @click="clearRecovery">清除本次密码</button>
          </div>
        </template>
        <a href="/">前往登录</a>
      </div>
    </section>
  </div>
</template>
