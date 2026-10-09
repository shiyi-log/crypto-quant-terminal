/** 所有币种共享四个请求名额，避免全屏图表同时冲击行情接口。 */
let active = 0;
const waiters: Array<() => void> = [];

export async function withMarketSlot<T>(run: () => Promise<T>): Promise<T> {
  if (active < 4) active += 1;
  else await new Promise<void>((resolve) => waiters.push(resolve));
  try {
    return await run();
  } finally {
    const next = waiters.shift();
    if (next) next();
    else active -= 1;
  }
}
