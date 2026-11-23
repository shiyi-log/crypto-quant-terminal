import { defineConfig } from '@vben/vite-config';
import { readFile } from 'node:fs/promises';

async function readQuantProjectVersion() {
  const projectToml = await readFile(
    new URL('../../../pyproject.toml', import.meta.url),
    'utf8',
  );
  let inProjectSection = false;
  let version: string | undefined;

  for (const line of projectToml.split(/\r?\n/)) {
    if (line.startsWith('[')) {
      inProjectSection = line.trim() === '[project]';
      continue;
    }
    if (!inProjectSection) continue;
    const match = line.match(/^\s*version\s*=\s*"([^"]+)"\s*$/);
    if (match) {
      version = match[1];
      break;
    }
  }

  if (!version) {
    throw new Error('无法从 pyproject.toml 的 [project] 段读取项目版本');
  }

  return version;
}

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
  const quantVersion = await readQuantProjectVersion();

  return {
    application: {},
    vite: {
      // 演示页显示与后端发布同步的版本，不使用 Vben 管理模板自身版本。
      define: {
        'import.meta.env.VITE_QUANT_VERSION': JSON.stringify(quantVersion),
      },
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
