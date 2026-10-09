<script lang="ts" setup>
import { computed, watch } from 'vue';
import { useRouter } from 'vue-router';

import { AuthenticationLoginExpiredModal } from '@vben/common-ui';
import { useWatermark } from '@vben/hooks';
import { BasicLayout, LockScreen, UserDropdown } from '@vben/layouts';
import { preferences, usePreferences } from '@vben/preferences';
import { useAccessStore, useUserStore } from '@vben/stores';

import { Card } from 'ant-design-vue';

import { $t } from '#/locales';
import { useAuthStore } from '#/store';
import LoginForm from '#/views/_core/authentication/login.vue';
import BotControls from '#/views/quant/components/BotControls.vue';

const router = useRouter();
const userStore = useUserStore();
const authStore = useAuthStore();
const accessStore = useAccessStore();
const { destroyWatermark, updateWatermark } = useWatermark();
const { isDark } = usePreferences();

/**
 * 用户下拉菜单只保留真实可用的入口。
 *
 * 之前这里有「文档 / GitHub / 常见问题」三个外链指向 Vben 官方站点，
 * 与本项目无关；顶栏还有一个通知铃铛，挂的是 6 条写死的示例通知
 * （「收到了 14 份新周报」「朱偏右 回复了你」…），红点恒亮且永远不会变。
 * 本项目没有通知数据源，因此把铃铛与假通知一起移除，
 * 而不是继续显示假数据。
 */
const menus = computed(() => [
  {
    handler: () => {
      router.push({ name: 'Profile' });
    },
    icon: 'lucide:user',
    text: $t('page.auth.profile'),
  },
]);

const avatar = computed(
  () => userStore.userInfo?.avatar ?? preferences.app.defaultAvatar,
);

/** 顶栏展示名：realName 为空时退回用户名，不留空白 */
const displayName = computed(
  () => userStore.userInfo?.realName || userStore.userInfo?.username || '',
);

async function handleLogout() {
  await authStore.logout(false);
}

watch(
  () => ({
    enable: preferences.app.watermark,
    content: preferences.app.watermarkContent,
    isDark: isDark.value,
  }),
  async ({ enable, content, isDark: isDarkValue }) => {
    if (enable) {
      const watermarkColor = isDarkValue
        ? 'rgba(255, 255, 255, 0.12)'
        : 'rgba(0, 0, 0, 0.12)';

      await updateWatermark({
        advancedStyle: {
          colorStops: [
            {
              color: watermarkColor,
              offset: 0,
            },
            {
              color: watermarkColor,
              offset: 1,
            },
          ],
          type: 'linear',
        },
        content:
          content ||
          `${userStore.userInfo?.username} - ${userStore.userInfo?.realName}`,
      });
    } else {
      destroyWatermark();
    }
  },
  {
    immediate: true,
  },
);
</script>

<template>
  <BasicLayout
    :avatar
    :text="displayName"
    @clear-preferences-and-logout="handleLogout"
    @logout="handleLogout"
  >
    <template #content-top>
      <!-- 交易状态与操作作为正文卡片展示，窄屏时自动换行。 -->
      <Card class="mx-4 mt-4" :bordered="false" title="交易状态与操作">
        <BotControls />
      </Card>
    </template>
    <template #user-dropdown>
      <UserDropdown
        :avatar
        :menus
        :text="displayName"
        @clear-preferences-and-logout="handleLogout"
        @logout="handleLogout"
      />
    </template>
    <template #extra>
      <AuthenticationLoginExpiredModal
        v-model:open="accessStore.loginExpired"
        :avatar
      >
        <LoginForm />
      </AuthenticationLoginExpiredModal>
    </template>
    <template #lock-screen>
      <LockScreen :avatar @to-login="handleLogout" />
    </template>
  </BasicLayout>
</template>
