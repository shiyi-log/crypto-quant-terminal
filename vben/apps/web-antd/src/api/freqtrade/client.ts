/**
 * Freqtrade API 客户端
 *
 * Freqtrade 的 REST API 与 Vben 默认约定不同：
 *  - 返回裸 JSON，没有 { code, data } 信封
 *  - 认证是 HTTP Basic 换取 JWT，之后用 Bearer
 *
 * ⚠️ 重要：不要依赖 Vben 的 `responseReturn` 选项。
 * 该选项由 `defaultResponseInterceptor` 实现，未注册该拦截器时它完全无效，
 * 请求会返回完整的 AxiosResponse（带 status/headers/config），
 * 直接取 `res.access_token` 必然是 undefined。
 * 因此这里统一用 unwrap() 显式取响应体，行为确定。
 *
 * API 地址走 Vben 的运行时配置（dist/_app-config-*.js），
 * 部署后改端口不需要重新打包。
 */
import { useAppConfig } from '@vben/hooks';
import { RequestClient } from '@vben/request';

export const FT_TOKEN_KEY = 'ft_access_token';

const { apiURL } = useAppConfig(import.meta.env, import.meta.env.PROD);

/** 当前生效的 API 基地址，例如 http://127.0.0.1:8889/api */
export const FT_API_URL: string = (apiURL as string) || '/api';

/**
 * 推导 Freqtrade 服务器站点根地址（进度文件放在站点根，不在 /api 之下）
 */
export const FT_SERVER_ORIGIN: string = /^https?:\/\//i.test(FT_API_URL)
  ? FT_API_URL.replace(/\/api\/?$/i, '')
  : '';

export const ftClient = new RequestClient({
  baseURL: FT_API_URL,
  timeout: 20_000,
});

ftClient.addRequestInterceptor({
  fulfilled: (config) => {
    // 登录接口使用 HTTP Basic，绝不能被已存在的 Bearer token 覆盖，
    // 否则一旦本地残留旧 token，登录会静默失败。
    if (config.url && config.url.includes('/token/login')) {
      return config;
    }
    const token = localStorage.getItem(FT_TOKEN_KEY);
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
});

/**
 * 兼容两种返回：完整 AxiosResponse 或已解包的 body。
 * 这样无论 RequestClient 的默认行为如何变化，取值都正确。
 */
export function unwrap<T = any>(res: any): T {
  if (
    res &&
    typeof res === 'object' &&
    'data' in res &&
    ('status' in res || 'headers' in res || 'config' in res)
  ) {
    return res.data as T;
  }
  return res as T;
}

/** 把任意错误整理成可读文案 */
export function errText(error: any): string {
  const body = error?.response?.data ?? error?.data ?? error;
  const detail =
    body?.detail ?? body?.error ?? body?.message ?? error?.message ?? '';
  if (typeof detail === 'string' && detail) {
    return detail;
  }
  if (detail) {
    return JSON.stringify(detail);
  }
  return error?.message || '未知错误';
}

/**
 * 清掉本地令牌。
 *
 * 说明：登录走的是认证服务（auth_service.py 的 /auth/login 与 /auth/auto-login），
 * 由它在服务端代持 Freqtrade 的 Basic 凭据，前端从不接触 Freqtrade 账号密码。
 * 因此这里只需要在退出时清除令牌。
 */
export function ftLogout() {
  localStorage.removeItem(FT_TOKEN_KEY);
}

