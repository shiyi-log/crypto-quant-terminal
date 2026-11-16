import { describe, expect, it } from 'vitest';

import { readSessionSnapshot } from './sessionSnapshot';

describe('会话快照读取', () => {
  it('用捕获的会话读取历史并在返回前核对身份', async () => {
    const status = { runner: { virtualAccountKey: 'round-a', selectedAccountId: 'strategy-a' } };
    const result = await readSessionSnapshot(async () => status, async (session) => ({ session }));
    expect(result).toEqual({ status, data: { session: 'round-a' } });
  });

  it('切换策略期间的旧账户数据不会被发布', async () => {
    let status = { runner: { virtualAccountKey: 'round-a', selectedAccountId: 'strategy-a' } };
    await expect(readSessionSnapshot(async () => status, async () => {
      status = { runner: { virtualAccountKey: 'round-b', selectedAccountId: 'strategy-b' } };
      return { orders: ['old-account-order'] };
    })).rejects.toThrow('Demo 会话已切换');
  });

  it('没有会话身份或请求已取消时拒绝发布', async () => {
    await expect(readSessionSnapshot(async () => ({ runner: {} }), async () => ({}))).rejects.toThrow('Demo 会话身份未知');
    const controller = new AbortController();
    await expect(readSessionSnapshot(async () => ({ runner: { virtualAccountKey: 'round-a' } }), async () => {
      controller.abort();
      return {};
    }, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
  });
});
