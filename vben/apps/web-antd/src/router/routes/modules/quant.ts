import type { RouteRecordRaw } from 'vue-router';

/**
 * 新版量化工作区；旧版界面已退役，旧地址由统一 404 处理
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
        name: 'QuantTradeDesk',
        path: 'trade-desk',
        component: () => import('#/views/quant/trade-desk/index.vue'),
        meta: {
          affixTab: true,
          icon: 'lucide:layout-dashboard',
          title: '交易台',
        },
      },
      {
        name: 'QuantAcceptance',
        path: 'acceptance',
        component: () => import('#/views/quant/acceptance/index.vue'),
        meta: {
          icon: 'lucide:clipboard-check',
          title: '验收与上线',
        },
      },
      {
        name: 'QuantFactors',
        path: 'factors',
        component: () => import('#/views/quant/factors/index.vue'),
        meta: { icon: 'lucide:chart-network', title: '因子实验室' },
      },
      {
        name: 'QuantExperiments',
        path: 'experiments',
        component: () => import('#/views/quant/experiments/index.vue'),
        meta: { icon: 'lucide:flask-conical', title: '实验任务' },
      },
      {
        name: 'QuantDataQuality',
        path: 'data-quality',
        component: () => import('#/views/quant/data-quality/index.vue'),
        meta: { icon: 'lucide:database', title: '数据中心' },
      },
      {
        name: 'QuantOperations',
        path: 'operations',
        component: () => import('#/views/quant/operations/index.vue'),
        meta: { icon: 'lucide:activity', title: '运维与证据' },
      },
      {
        name: 'QuantOrderflow',
        path: 'orderflow',
        component: () => import('#/views/quant/orderflow/index.vue'),
        meta: {
          icon: 'lucide:chart-no-axes-combined',
          title: '订单流与价格',
        },
      },
      {
        name: 'QuantPriceSpikes',
        path: 'price-spikes',
        component: () => import('#/views/quant/orderflow/price-spikes.vue'),
        meta: {
          hideInMenu: true,
          icon: 'lucide:chart-no-axes-combined',
          title: '价格事件研究',
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
        name: 'QuantStrategyLab',
        path: 'strategy-lab',
        component: () => import('#/views/quant/strategy-lab/index.vue'),
        meta: {
          hideInMenu: true,
          icon: 'lucide:panels-top-left',
          title: '纸面账户档案',
        },
      },
      {
        name: 'QuantBinanceDemo',
        path: 'binance-demo',
        component: () => import('#/views/quant/binance-demo/index.vue'),
        meta: {
          hideInMenu: true,
          icon: 'lucide:landmark',
          title: 'Demo 账户设置',
        },
      },
      {
        name: 'QuantSecurity',
        path: 'security',
        component: () => import('#/views/quant/security/index.vue'),
        meta: {
          hideInMenu: true,
          icon: 'lucide:shield-check',
          title: '身份与安全',
        },
      },
    ],
  },
];

export default routes;
