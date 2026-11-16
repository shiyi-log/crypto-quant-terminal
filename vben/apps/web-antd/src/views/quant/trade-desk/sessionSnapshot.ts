type SessionStatus = { runner: { virtualAccountKey?: null | string; selectedAccountId?: null | string } };

/** 历史接口固定会话；账户切换或取消后不发布已经失效的一组响应。 */
export async function readSessionSnapshot<S extends SessionStatus, D>(
  readStatus: () => Promise<S>, readData: (session: string) => Promise<D>, signal?: AbortSignal,
): Promise<{ status: S; data: D }> {
  signal?.throwIfAborted();
  const initial = await readStatus();
  const session = initial.runner.virtualAccountKey;
  if (!session) throw new Error('Demo 会话身份未知，请先核对账户状态');
  const data = await readData(session);
  signal?.throwIfAborted();
  const final = await readStatus();
  signal?.throwIfAborted();
  if (final.runner.virtualAccountKey !== session || final.runner.selectedAccountId !== initial.runner.selectedAccountId) {
    throw new Error('Demo 会话已切换，旧会话响应已丢弃，请刷新');
  }
  return { status: final, data };
}
