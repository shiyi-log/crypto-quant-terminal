import { defineConfig } from '@vben/vite-config';

/**
 * 开发环境（前端 dev server 5666）把 /ftapi 代理到本地认证服务（8890）。
 *
 *   /ftapi/auth/login  →  http://127.0.0.1:8890/auth/login
 *   /ftapi/v1/ping     →  http://127.0.0.1:8890/api/v1/ping   （认证服务再代理到 Freqtrade 8889）
 *
 * 生产环境前端由 8888 静态托管，通过运行时配置 dist/_app-config-*.js
 * 指定 http://127.0.0.1:8890/api 。
 */
export default defineConfig(async () => {
  return {
    application: {},
    vite: {
      server: {
        proxy: {
          '/ftapi': {
            changeOrigin: true,
            rewrite: (path) => path.replace(/^\/ftapi/, ''),
            target: 'http://127.0.0.1:8890',
            ws: true,
          },
        },
      },
    },
  };
});
