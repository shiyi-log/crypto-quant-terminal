import type { UserInfo } from '@vben/types';

import { getMeApi } from './auth';

/**
 * 当前用户信息。
 *
 * 早先这里把 username 写死成 'admin'，而认证服务其实是按用户名签发令牌的
 * （auth_service.py 的 /auth/me 一直存在但前端从未调用），
 * 结果是改了密码或换账号后，界面上的身份不会跟着变。
 *
 * 现在改为真实读取 /auth/me；认证服务不可达时不阻断登录流程，
 * 退回默认展示名。
 */
export async function getUserInfoApi(): Promise<UserInfo> {
  let username = 'admin';
  try {
    const me = await getMeApi();
    if (me.username) {
      username = me.username;
    }
  } catch {
    /* 认证服务不可达：保持默认，不影响登录 */
  }
  return {
    avatar: '',
    desc: 'Freqtrade 量化交易终端',
    homePath: '/quant/overview',
    realName: 'Freqtrade',
    roles: ['admin'],
    token: '',
    userId: '1',
    username,
  } as unknown as UserInfo;
}
