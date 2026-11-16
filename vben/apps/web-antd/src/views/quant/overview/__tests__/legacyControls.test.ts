import { describe, expect, it, vi } from 'vitest';
import { createLegacyControls } from '../legacyControls';
function setup(config: unknown = { dry_run: true, state: 'running' }) {
  const transport = {
    config: vi.fn(async () => config),
    count: vi.fn(async () => ({ current: 2, max: 10 })),
    command: vi.fn(async (_command: string) => ({ status: 'accepted' })),
  };
  return { transport, model: createLegacyControls(transport) };
}
describe('旧引擎显式命令', () => {
  it('does not write on read and blocks commands when actual environment or config/count facts are unknown', async () => {
    const { transport, model } = setup({ state: 'running' });
    await model.refresh();
    expect(model.environment).toBe('运行环境未知');
    expect(model.canCommand('forceexit')).toBe(false);
    await model.execute('forceexit');
    expect(transport.command).not.toHaveBeenCalled();
  });
  it('submits only once while busy and reports receipt instead of inventing execution completion', async () => {
    const { transport, model } = setup();
    await model.refresh();
    let finish!: (value: unknown) => void;
    transport.command.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }) as never,
    );
    const pending = model.execute('forceexit');
    await model.execute('forceexit');
    expect(transport.command).toHaveBeenCalledTimes(1);
    finish({ result: 'Created exit order' });
    await pending;
    expect(model.notice).toContain('收到命令回执');
    expect(model.notice).toContain('待后续查询核验');
    expect(model.notice).not.toContain('已平仓');
  });
  it('treats malformed/error acknowledgement or transport error as unknown without showing diagnostic secrets or retrying', async () => {
    const { transport, model } = setup();
    await model.refresh();
    transport.command.mockResolvedValueOnce({
      status: 'error',
      detail: 'postgres://secret',
    } as never);
    await model.execute('forceexit');
    expect(model.notice).toContain('结果未知');
    transport.command.mockRejectedValueOnce(new Error('postgres://secret'));
    await model.execute('forceexit');
    expect(model.notice).toContain('结果未知');
    expect(model.notice).not.toContain('secret');
    expect(transport.command).toHaveBeenCalledTimes(2);
  });
  it('validates strict count and ignores old config when a refresh fails before allowing any further write', async () => {
    const { transport, model } = setup();
    await model.refresh();
    transport.config.mockRejectedValueOnce(new Error('secret'));
    await model.refresh();
    expect(model.canCommand('forceexit')).toBe(false);
    expect(model.environment).toContain('模拟盘');
    transport.count.mockResolvedValueOnce({ current: false, max: 10 } as never);
    await model.refresh();
    expect(model.current).toBe(2);
    expect(model.canCommand('forceexit')).toBe(false);
  });
  it('does not issue writes after disposal even if a confirmation result arrives later', async () => {
    const { transport, model } = setup();
    await model.refresh();
    model.dispose();
    await model.execute('forceexit');
    expect(transport.command).not.toHaveBeenCalled();
  });
});
