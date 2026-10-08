/**
 * 对接本地认证服务（auth_service.py，端口 8890）。
 *
 * Freqtrade 自身没有密码管理接口（账号密码在配置文件里，改了要重启），
 * 因此 UI 的用户体系由认证服务维护：
 *   POST /auth/login            账号密码 -> 会话令牌
 *   POST /auth/change-password  修改密码（需旧密码 + 登录态）
 *   POST /auth/reset-password   重置密码（仅限本机）
 * 数据接口由该服务代理，前端始终拿不到 Freqtrade 的真实凭据。
 */
import { FT_API_URL, FT_SERVER_ORIGIN, FT_TOKEN_KEY, ftLogout } from '#/api/freqtrade';

export namespace AuthApi {
  /** 登录接口参数 */
  export interface LoginParams {
    password?: string;
    username?: string;
  }

  /** 登录接口返回值 */
  export interface LoginResult {
    accessToken: string;
    username?: string;
  }

  export interface RefreshTokenResult {
    data: string;
    status: number;
  }
}

/**
 * 认证服务的基地址。
 * 生产：运行时配置是绝对地址 http://127.0.0.1:8890/api → 取站点根
 * 开发：配置是相对路径 /ftapi/api → 取 /ftapi（由 vite 代理转发）
 */
const AUTH_BASE =
  FT_SERVER_ORIGIN || FT_API_URL.replace(/\/api\/?$/i, '') || '';

function token() {
  return localStorage.getItem(FT_TOKEN_KEY) ?? '';
}

async function request(path: string, options: RequestInit = {}, withAuth = false) {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options.headers as Record<string, string>) ?? {}),
  };
  if (withAuth) {
    headers.Authorization = `Bearer ${token()}`;
  }
  const res = await fetch(`${AUTH_BASE}${path}`, { ...options, headers });
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    throw new Error(body?.detail || `${res.status} ${res.statusText}`);
  }
  return body;
}

/**
 * 登录
 *
 * 传 { auto: true } 走本机免密自动登录（仅 127.0.0.1 可用），
 * 否则走常规账号密码登录。
 */
export async function loginApi(
  data: AuthApi.LoginParams & { auto?: boolean },
): Promise<AuthApi.LoginResult> {
  const auto = Boolean((data as any).auto);
  const body = auto
    ? await request('/auth/auto-login', {
        body: JSON.stringify({}),
        method: 'POST',
      })
    : await request('/auth/login', {
        body: JSON.stringify({
          password: data.password ?? '',
          username: data.username ?? '',
        }),
        method: 'POST',
      });
  const accessToken = body?.access_token;
  if (!accessToken) {
    throw new Error('登录失败：认证服务未返回令牌');
  }
  localStorage.setItem(FT_TOKEN_KEY, accessToken);
  try {
    sessionStorage.removeItem('ft_manual_logout');
  } catch {
    /* 忽略 */
  }
  return { accessToken, username: body?.username };
}

/** 修改密码（需登录态） */
export async function changePasswordApi(
  oldPassword: string,
  newPassword: string,
): Promise<{ message?: string }> {
  return request(
    '/auth/change-password',
    {
      body: JSON.stringify({
        new_password: newPassword,
        old_password: oldPassword,
      }),
      method: 'POST',
    },
    true,
  );
}

/** 重置密码（仅限本机调用；不传 newPassword 则服务端随机生成） */
export async function resetPasswordApi(
  username: string,
  newPassword?: string,
): Promise<{ generated: boolean; password: string; username: string }> {
  return request('/auth/reset-password', {
    body: JSON.stringify({ new_password: newPassword, username }),
    method: 'POST',
  });
}

/**
 * Freqtrade 侧没有 refresh 接口，认证服务用的是无状态签名令牌，
 * 直接回传当前令牌即可。
 */
export async function refreshTokenApi(): Promise<AuthApi.RefreshTokenResult> {
  return { data: token(), status: 200 };
}

/**
 * 退出登录：清掉本地令牌，并标记本次会话为「主动退出」
 */
export async function logoutApi() {
  try {
    await request('/auth/logout', { method: 'POST' }, true);
  } catch {
    /* 服务不可达也要允许本地退出 */
  }
  ftLogout();
  try {
    sessionStorage.setItem('ft_manual_logout', '1');
  } catch {
    /* 忽略 */
  }
  return { status: 200 };
}

/**
 * 认证服务没有权限码体系，返回空数组表示不限制
 */
export async function getAccessCodesApi(): Promise<string[]> {
  return [];
}

/**
 * 当前登录用户。
 *
 * 认证服务一直是按「用户名」签发令牌的，但前端此前把用户名写死成 'admin'，
 * 改了密码或换账号后界面身份不会跟着变。这里改成真实读取 /auth/me。
 */
export async function getMeApi(): Promise<{ username: string }> {
  const body = await request('/auth/me', { method: 'GET' }, true);
  return { username: body?.username ?? '' };
}
