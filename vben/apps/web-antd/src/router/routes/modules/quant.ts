import type { RouteRecordRaw } from 'vue-router';

/**
 * 量化交易终端路由
 */
const routes: RouteRecordRaw[] = [
  {
    meta: {
      icon: 'lucide:trending-up',
      order: -10,
      title: '量化交易',
    },
    name: 'Quant',
    path: '/quant',
    children: [
      {
        name: 'QuantOverview',
        path: 'overview',
        component: () => import('#/views/quant/overview/index.vue'),
        meta: {
          affixTab: true,
          icon: 'lucide:layout-dashboard',
          title: '总览',
        },
      },
      {
        name: 'QuantPositions',
        path: 'positions',
        component: () => import('#/views/quant/positions/index.vue'),
        meta: {
          icon: 'lucide:wallet',
          title: '持仓与成交',
        },
      },
      {
        name: 'QuantMarket',
        path: 'market',
        component: () => import('#/views/quant/market/index.vue'),
        meta: {
          icon: 'lucide:candlestick-chart',
          title: '行情图表',
        },
      },
      {
        name: 'QuantBacktest',
        path: 'backtest',
        component: () => import('#/views/quant/backtest/index.vue'),
        meta: {
          icon: 'lucide:flask-conical',
          title: '回测与任务',
        },
      },
      {
        name: 'QuantDetail',
        path: 'detail',
        component: () => import('#/views/quant/detail/index.vue'),
        meta: {
          icon: 'lucide:list-tree',
          title: '回测明细',
        },
      },
      {
        name: 'QuantTasks',
        path: 'tasks',
        component: () => import('#/views/quant/tasks/index.vue'),
        meta: {
          // 2026-10-09 合并：已并入「回测与任务」/「实盘运维」，研究任务从菜单隐藏但路由保留
          hideInMenu: true,
          icon: 'lucide:list-checks',
          title: '研究任务',
        },
      },
      {
        name: 'QuantLive',
        path: 'live',
        component: () => import('#/views/quant/live/index.vue'),
        meta: {
          // 2026-10-09 合并：已并入「回测与任务」/「实盘运维」，实盘统计从菜单隐藏但路由保留
          hideInMenu: true,
          icon: 'lucide:bar-chart-3',
          title: '实盘统计',
        },
      },
      {
        name: 'QuantResearch',
        path: 'research',
        component: () => import('#/views/quant/research/index.vue'),
        meta: {
          icon: 'lucide:flask-conical',
          title: '策略研究',
        },
      },
      {
        name: 'QuantIteration',
        path: 'iteration',
        component: () => import('#/views/quant/iteration/index.vue'),
        meta: {
          // 2026-10-09 合并：已并入「回测与任务」/「实盘运维」，模型迭代从菜单隐藏但路由保留
          hideInMenu: true,
          icon: 'lucide:refresh-cw',
          title: '模型迭代',
        },
      },
      {
        name: 'QuantOps',
        path: 'ops',
        component: () => import('#/views/quant/ops/index.vue'),
        meta: {
          icon: 'lucide:activity',
          title: '实盘运维',
        },
      },
      {
        name: 'QuantPairlist',
        path: 'pairlist',
        component: () => import('#/views/quant/pairlist/index.vue'),
        meta: {
          icon: 'lucide:shield-alert',
          title: '交易对与锁',
        },
      },
      {
        name: 'QuantSecurity',
        path: 'security',
        component: () => import('#/views/quant/security/index.vue'),
        meta: {
          icon: 'lucide:shield-check',
          title: '密码管理',
        },
      },
      {
        name: 'QuantLogs',
        path: 'logs',
        component: () => import('#/views/quant/logs/index.vue'),
        meta: {
          icon: 'lucide:scroll-text',
          title: '运行日志',
        },
      },
    ],
  },
];

export default routes;
