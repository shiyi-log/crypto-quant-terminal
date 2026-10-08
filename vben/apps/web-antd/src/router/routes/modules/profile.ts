import type { RouteRecordRaw } from 'vue-router';

import { $t } from '#/locales';

/**
 * 个人中心路由。
 *
 * 原先这条路由夹在 `vben.ts`（Vben 自带演示页 + 官方外链）里，
 * 清理模板残留时整个文件被改名为 `vben.ts.bak`，导致 `Profile` 路由一起消失，
 * 而顶栏用户下拉菜单仍然 `router.push({ name: 'Profile' })`
 * （见 layouts/basic.vue），点击「个人中心」没有任何反应。
 *
 * 因此把这条真实路由单独拆出来常驻，不再和模板演示页混在一起。
 */
const routes: RouteRecordRaw[] = [
  {
    name: 'Profile',
    path: '/profile',
    component: () => import('#/views/_core/profile/index.vue'),
    meta: {
      hideInMenu: true,
      icon: 'lucide:user',
      title: $t('page.auth.profile'),
    },
  },
];

export default routes;
