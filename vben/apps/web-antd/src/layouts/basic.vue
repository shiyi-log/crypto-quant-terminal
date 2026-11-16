<script lang="ts" setup>
import { computed, watch } from 'vue';
import { useRouter } from 'vue-router';

import { AuthenticationLoginExpiredModal } from '@vben/common-ui';
import { useWatermark } from '@vben/hooks';
import { BasicLayout, LockScreen, UserDropdown } from '@vben/layouts';
import { preferences, usePreferences } from '@vben/preferences';
import { useAccessStore, useUserStore } from '@vben/stores';

import { $t } from '#/locales';
import { useAuthStore } from '#/store';
import LoginForm from '#/views/_core/authentication/login.vue';

import QuantShellHeader from './components/QuantShellHeader.vue';

const router = useRouter();
const quantWorkspace = computed(() =>
  router.currentRoute.value.path.startsWith('/quant/'),
);
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
    :class="{ 'quant-workspace-shell': quantWorkspace }"
    :avatar
    :text="displayName"
    @clear-preferences-and-logout="handleLogout"
    @logout="handleLogout"
  >
    <template #content-top>
      <QuantShellHeader />
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

<style scoped>
/* 壳级颜色作用于导航、顶栏与工作面；业务页面的收益语义色由页面自行定义。 */
.quant-workspace-shell {
  --background: 210 56% 11%;
  --background-deep: 210 56% 11%;
  --foreground: 208 41% 93%;
  --card: 207 53% 16%;
  --card-foreground: 208 41% 93%;
  --popover: 207 53% 16%;
  --popover-foreground: 208 41% 93%;
  --border: 205 38% 26%;
  --input: 205 38% 26%;
  --muted: 207 53% 16%;
  --muted-foreground: 206 26% 71%;
  --sidebar: 210 56% 11%;
  --sidebar-deep: 210 56% 11%;
  --header: 210 56% 11%;
  --menu: 210 56% 11%;
  --menu-foreground: 208 41% 93%;
  min-width: 0;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.quant-workspace-shell :deep([data-layout-region='scroll']) {
  min-width: 0;
}
/* 主体须随实际内容增长；固定视口高度会让 sticky 环境栏在正文结束前失去边界。 */
.quant-workspace-shell :deep(#__vben_main_content) {
  flex-basis: auto !important;
  flex-shrink: 0 !important;
}
</style>
