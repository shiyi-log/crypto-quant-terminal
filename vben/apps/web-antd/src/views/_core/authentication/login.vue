<script lang="ts" setup>
import type { VbenFormSchema } from '@vben/common-ui';

import { computed, onMounted, ref } from 'vue';

import { AuthenticationLogin, z } from '@vben/common-ui';
import { Alert, Button, Input, message, Space } from 'ant-design-vue';

import { resetPasswordApi } from '#/api/core/auth';
import { useAuthStore } from '#/store';

defineOptions({ name: 'Login' });

const authStore = useAuthStore();

const errMsg = ref('');

/** 重置密码面板 */
const showReset = ref(false);
const resetUser = ref('admin');
const resetLoading = ref(false);
const resetResult = ref<null | { generated: boolean; password: string }>(null);

/**
 * 对接认证服务的登录表单。
 *
 * 原始模板带演示用的「选择账号」下拉和 SliderCaptcha 滑块验证码，
 * 滑块是必填项，未拖动会导致表单校验不通过 —— 表现就是点击登录毫无反应。
 * 已改成纯账号密码表单，并移除自动登录（需要显式输入账号密码）。
 */
const formSchema = computed((): VbenFormSchema[] => [
  {
    component: 'VbenInput',
    componentProps: {
      autocomplete: 'username',
      placeholder: '请输入用户名',
    },
    defaultValue: 'admin',
    fieldName: 'username',
    label: '用户名',
    rules: z.string().min(1, { message: '请输入用户名' }),
  },
  {
    component: 'VbenInputPassword',
    componentProps: {
      autocomplete: 'current-password',
      placeholder: '请输入密码',
    },
    fieldName: 'password',
    label: '密码',
    rules: z.string().min(1, { message: '请输入密码' }),
  },
]);

async function handleSubmit(values: Record<string, any>) {
  errMsg.value = '';
  try {
    await authStore.authLogin(values);
  } catch (error: any) {
    errMsg.value =
      error?.response?.data?.detail ?? error?.message ?? '登录失败';
  }
}

/**
 * 本机自动登录：进页面直接进主界面，不需要手动输入。
 * 走认证服务的 /auth/auto-login（仅 127.0.0.1 可用，不受密码变更影响）。
 * 失败则回落到手动输入账号密码。
 */
const autoLoading = ref(false);

/**
 * 本机免密登录（供手动退出后重新进入）
 *
 * 为什么需要这个按钮：退出登录会写 sessionStorage.ft_manual_logout，
 * 之后同一标签页的每次刷新都会跳过自动登录（这是「退出要生效」的正确行为），
 * 但本机单人使用场景下就没法回来了 —— 只能关标签页重开。
 * 这里给一个显式入口：点击即清除该标记并重新免密登录。
 */
async function doAutoLogin() {
  autoLoading.value = true;
  try {
    sessionStorage.removeItem('ft_manual_logout');
    await authStore.authLogin({ auto: true });
  } catch {
    message.warning('本机免密登录不可用，请使用帐号密码');
    sessionStorage.setItem('ft_manual_logout', '1');
  } finally {
    autoLoading.value = false;
  }
}

onMounted(async () => {
  if (sessionStorage.getItem('ft_manual_logout')) {
    return;
  }
  autoLoading.value = true;
  try {
    await authStore.authLogin({ auto: true });
  } catch {
    /* 自动登录不可用时保留手动表单 */
  } finally {
    autoLoading.value = false;
  }
});

async function doReset(generate: boolean) {
  resetLoading.value = true;
  resetResult.value = null;
  try {
    const res = await resetPasswordApi(
      resetUser.value,
      generate ? undefined : undefined,
    );
    resetResult.value = { generated: res.generated, password: res.password };
    message.success('密码已重置');
  } catch (error: any) {
    message.error(error?.message ?? '重置失败');
  } finally {
    resetLoading.value = false;
  }
}
</script>

<template>
  <div>
    <div
      v-if="autoLoading"
      class="mb-3 rounded-md bg-blue-50 px-3 py-2 text-center text-sm text-blue-600 dark:bg-blue-950/40 dark:text-blue-300"
    >
      正在自动登录…
    </div>
    <div
      v-if="errMsg"
      class="mb-3 rounded-md bg-red-50 px-3 py-2 text-center text-sm text-red-600 dark:bg-red-950/40 dark:text-red-300"
    >
      {{ errMsg }}
    </div>

    <!-- 常规登录 -->
    <template v-if="!showReset">
      <AuthenticationLogin
        :form-schema="formSchema"
        :loading="authStore.loginLoading"
        :show-code-login="false"
        :show-forget-password="false"
        :show-qrcode-login="false"
        :show-register="false"
        :show-remember-me="false"
        :show-third-party-login="false"
        sub-title="Freqtrade 量化交易终端"
        submit-button-text="登 录"
        title="量化交易终端"
        @submit="handleSubmit"
      />
      <div class="mt-4 text-center text-sm">
        <a class="text-blue-500 hover:underline" @click="showReset = true">
          忘记密码？重置密码
        </a>
      </div>

      <div class="mt-3">
        <Button :loading="autoLoading" block @click="doAutoLogin">
          🔓 本机免密登录
        </Button>
      </div>
    </template>

    <!-- 重置密码 -->
    <template v-else>
      <div class="mb-5">
        <h2 class="text-xl font-semibold">重置密码</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          出于安全考虑，此操作仅允许从本机发起，且不需要旧密码。
        </p>
      </div>

      <Alert
        class="mb-4"
        message="重置后会生成一个新的随机密码，请立即保存。"
        show-icon
        type="warning"
      />

      <div class="mb-2 text-sm">用户名</div>
      <Input v-model:value="resetUser" placeholder="admin" />

      <Space class="mt-5 w-full" direction="vertical">
        <Button :loading="resetLoading" block type="primary" @click="doReset(true)">
          生成随机新密码
        </Button>
        <Button block @click="showReset = false">返回登录</Button>
      </Space>

      <div
        v-if="resetResult"
        class="mt-5 rounded-md border border-green-300 bg-green-50 p-4 text-sm dark:border-green-800 dark:bg-green-950/40"
      >
        <div class="mb-1 text-green-700 dark:text-green-300">重置成功，新密码：</div>
        <div class="font-mono text-base font-semibold tracking-wider">
          {{ resetResult.password }}
        </div>
        <div class="mt-2 text-xs text-muted-foreground">
          请复制保存。用该密码返回登录，登录后可在「密码管理」中改成自己的密码。
        </div>
      </div>
    </template>
  </div>
</template>
