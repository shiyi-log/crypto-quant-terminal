import type { RouteLocationGeneric, RouteRecordRaw } from 'vue-router';

import { LOGIN_PATH } from '@vben/constants';
import { preferences } from '@vben/preferences';

import { $t } from '#/locales';

const BasicLayout = () => import('#/layouts/basic.vue');
const AuthPageLayout = () => import('#/layouts/auth.vue');

// 前端演示阶段，所有量化深链接先进入固定本地样本。
// 旧量化业务页面源码已移除；未来若恢复真实页面，必须作为独立工作重新验收。
// 这些兼容入口不读取账户、业务 API 或生产数据。
type DemoPreviewWorkspace = 'desk' | 'research' | 'governance';

const demoRedirect =
  (preview: DemoPreviewWorkspace, workspace: string) =>
  (to: RouteLocationGeneric) => ({
    name: 'QuantDemoPreview',
    query: { ...to.query, preview, workspace },
  });

const quantDemoAliasDefinitions: {
  path: string;
  name: string;
  preview: DemoPreviewWorkspace;
  workspace: string;
}[] = [
  {
    path: '/quant/trade-desk',
    name: 'QuantDemoAliasTradeDesk',
    preview: 'desk',
    workspace: 'desk',
  },
  {
    path: '/quant/acceptance',
    name: 'QuantDemoAliasAcceptance',
    preview: 'governance',
    workspace: 'acceptance',
  },
  {
    path: '/quant/factors',
    name: 'QuantDemoAliasFactors',
    preview: 'research',
    workspace: 'factors',
  },
  {
    path: '/quant/experiments',
    name: 'QuantDemoAliasExperiments',
    preview: 'research',
    workspace: 'experiments',
  },
  {
    path: '/quant/data-quality',
    name: 'QuantDemoAliasDataQuality',
    preview: 'governance',
    workspace: 'data-quality',
  },
  {
    path: '/quant/operations',
    name: 'QuantDemoAliasOperations',
    preview: 'governance',
    workspace: 'operations',
  },
  {
    path: '/quant/orderflow',
    name: 'QuantDemoAliasOrderflow',
    preview: 'research',
    workspace: 'orderflow',
  },
  {
    path: '/quant/price-spikes',
    name: 'QuantDemoAliasPriceSpikes',
    preview: 'research',
    workspace: 'orderflow',
  },
  {
    path: '/quant/research',
    name: 'QuantDemoAliasResearch',
    preview: 'research',
    workspace: 'research',
  },
  {
    path: '/quant/strategy-lab',
    name: 'QuantDemoAliasStrategyLab',
    preview: 'research',
    workspace: 'research',
  },
  {
    path: '/quant/binance-demo',
    name: 'QuantDemoAliasBinanceDemo',
    preview: 'governance',
    workspace: 'settings',
  },
  {
    path: '/quant/security',
    name: 'QuantDemoAliasSecurity',
    preview: 'governance',
    workspace: 'settings',
  },
];

const quantDemoAliases: RouteRecordRaw[] = quantDemoAliasDefinitions.map(
  ({ path, name, preview, workspace }) => ({
    path,
    name,
    redirect: demoRedirect(preview, workspace),
    meta: {
      hideInMenu: true,
      hideInTab: true,
      title: `${path.split('/').at(-1)}演示`,
    },
  }),
);
/** 全局404页面 */
const fallbackNotFoundRoute: RouteRecordRaw = {
  component: () => import('#/views/_core/fallback/not-found.vue'),
  meta: {
    hideInBreadcrumb: true,
    hideInMenu: true,
    hideInTab: true,
    title: '404',
  },
  name: 'FallbackNotFound',
  path: '/:path(.*)*',
};

/** 基本路由，这些路由是必须存在的 */
const coreRoutes: RouteRecordRaw[] = [
  // 默认展示纯本地固定样本；此路由不依赖认证、账户 provider 或业务 API。
  {
    name: 'QuantDemoPreview',
    path: '/quant/demo-preview',
    component: () => import('#/views/preview/DemoPreview.vue'),
    props: (route) => ({
      preview: route.query.preview,
      workspace: route.query.workspace,
    }),
    meta: { hideInMenu: true, hideInTab: true, title: '本地演示预览' },
  },
  ...quantDemoAliases,
  // 系统状态入口可独立访问，不依赖尚未加载的业务菜单。
  {
    name: 'SystemForbidden',
    path: '/system/forbidden',
    component: () => import('#/views/_core/fallback/forbidden.vue'),
    meta: { hideInMenu: true, hideInTab: true, title: '访问权限' },
  },
  {
    name: 'SystemInternalError',
    path: '/system/internal-error',
    component: () => import('#/views/_core/fallback/internal-error.vue'),
    meta: { hideInMenu: true, hideInTab: true, title: '读取异常' },
  },
  {
    name: 'SystemOffline',
    path: '/system/offline',
    component: () => import('#/views/_core/fallback/offline.vue'),
    meta: { hideInMenu: true, hideInTab: true, title: '连接中断' },
  },
  {
    name: 'SystemComingSoon',
    path: '/system/coming-soon',
    component: () => import('#/views/_core/fallback/coming-soon.vue'),
    meta: { hideInMenu: true, hideInTab: true, title: '尚未开放' },
  },
  /**
   * 根路由
   * 使用基础布局，作为所有页面的父级容器，子级就不必配置BasicLayout。
   * 此路由必须存在，且不应修改
   */
  {
    component: BasicLayout,
    meta: {
      hideInBreadcrumb: true,
      title: 'Root',
    },
    name: 'Root',
    path: '/',
    redirect: preferences.app.defaultHomePath,
    children: [],
  },
  {
    component: AuthPageLayout,
    meta: {
      hideInTab: true,
      title: 'Authentication',
    },
    name: 'Authentication',
    path: '/auth',
    redirect: LOGIN_PATH,
    children: [
      {
        name: 'Login',
        path: 'login',
        component: () => import('#/views/_core/authentication/login.vue'),
        meta: {
          title: $t('page.auth.login'),
        },
      },
    ],
  },
];

export { coreRoutes, fallbackNotFoundRoute };
