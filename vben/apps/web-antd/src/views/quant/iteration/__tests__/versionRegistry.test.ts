import { describe, expect, it, vi } from 'vitest';
import {
  createVersionRegistry,
  parseVersionRegistry,
} from '../versionRegistry';

const version = (id = 'candidate-a', passed = false) => ({
  id,
  layer: 'ml_model',
  status: 'challenger',
  label: '候选模型',
  round: 9,
  source: 'auto',
  spec: { kind: 'gru' },
  metrics: { ic_period: 0, t_period: null },
  gate: { passed, reasons: ['样本不足'], criteria: '逐窗口检验' },
  deployed: null,
  artifacts: [],
  notes: null,
});
const snapshot = () => ({
  updated: null,
  running: null,
  trials: { total: 3, runs: 2, best: null, recent: [] },
  layers: {
    live_strategy: { champion: null, challengers: [], archived: [] },
    ml_model: {
      champion: null,
      challengers: [version()],
      archived: [{ ...version('archived-a'), status: 'archived' }],
    },
  },
});
function api() {
  return {
    load: vi.fn(async () => snapshot()),
    promote: vi.fn(async (id: string) => ({
      status: 'ok',
      id,
      champions: { ml_model: id },
    })),
  };
}

describe('版本登记合同及命令边界', () => {
  it('保留合法空值、零指标、归档版本及未通过判据', () => {
    const result = parseVersionRegistry(snapshot());
    expect(result.rows.map((row) => row.id)).toEqual([
      'candidate-a',
      'archived-a',
    ]);
    expect(result.rows[0]?.metrics.ic_period).toBe(0);
    expect(result.rows[0]?.metrics.t_period).toBeNull();
    expect(result.rows[0]?.gate?.passed).toBe(false);
    expect(result.updated).toBeNull();
  });
  it('拒绝重复版本和损坏列表或布尔门禁', () => {
    const duplicate = snapshot();
    duplicate.layers.ml_model.archived = [{ ...version(), status: 'archived' }];
    expect(() => parseVersionRegistry(duplicate)).toThrow();
    const broken = snapshot();
    (broken.layers.ml_model as any).challengers = null;
    expect(() => parseVersionRegistry(broken)).toThrow();
    const invalidGate = snapshot();
    (invalidGate.layers.ml_model.challengers[0]!.gate as any).passed = 'false';
    expect(() => parseVersionRegistry(invalidGate)).toThrow();
  });
  it('读取失败保留版本快照并隐藏私密错误，无自动登记', async () => {
    const calls = api();
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    calls.load.mockRejectedValueOnce(
      new Error('postgresql://u:secret@private/db'),
    );
    await registry.refresh();
    expect(registry.state.snapshot?.rows[0]?.id).toBe('candidate-a');
    expect(registry.state.error).toContain('保留');
    expect(JSON.stringify(registry.state)).not.toContain('secret');
    expect(calls.promote).not.toHaveBeenCalled();
    registry.dispose();
  });
  it('未通过版本需要明确确认且确认目标不随列表选择漂移', async () => {
    const calls = api();
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    registry.openConfirmation('candidate-a');
    registry.state.note = '复核登记理由';
    await registry.submit();
    expect(calls.promote).not.toHaveBeenCalled();
    registry.state.confirmUnpassed = true;
    registry.select('archived-a');
    await registry.submit();
    expect(calls.promote).toHaveBeenCalledWith(
      'candidate-a',
      '复核登记理由',
      true,
    );
    expect(registry.state.outcome).toContain('仅更新版本注册表');
    registry.dispose();
  });
  it('函数级重复提交只发送一次且错误身份回执不能冒充成功', async () => {
    const calls = api();
    let finish!: (value: any) => void;
    calls.promote.mockImplementationOnce(
      () =>
        new Promise((r) => {
          finish = r;
        }),
    );
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    registry.openConfirmation('candidate-a');
    registry.state.note = '复核登记理由';
    registry.state.confirmUnpassed = true;
    const first = registry.submit();
    await registry.submit();
    expect(calls.promote).toHaveBeenCalledTimes(1);
    finish({ status: 'ok', id: 'other' });
    await first;
    expect(registry.state.outcome).toBe('');
    expect(registry.state.commandError).toContain('结果未知');
    expect(registry.state.confirmTarget?.id).toBe('candidate-a');
    expect(registry.state.note).toBe('复核登记理由');
    registry.dispose();
  });
  it('刷新后判据变化或口径作废时拒绝沿用旧确认', async () => {
    const calls = api();
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    registry.openConfirmation('candidate-a');
    registry.state.note = '复核登记理由';
    registry.state.confirmUnpassed = true;
    const next = snapshot();
    (next.layers.ml_model.challengers[0] as any).metrics_validity =
      'invalidated';
    calls.load.mockResolvedValueOnce(next);
    await registry.refresh();
    await registry.submit();
    expect(calls.promote).not.toHaveBeenCalled();
    expect(registry.state.commandError).toContain('变化');
    registry.dispose();
  });
  it('已作废版本不能开启登记确认', async () => {
    const calls = api();
    const value = snapshot();
    (value.layers.ml_model.challengers[0] as any).metrics_validity =
      'invalidated';
    calls.load.mockResolvedValueOnce(value);
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    registry.openConfirmation('candidate-a');
    expect(registry.state.confirmTarget).toBeNull();
    expect(calls.promote).not.toHaveBeenCalled();
    registry.dispose();
  });
  it('dispose 丢弃迟到读取和登记回执', async () => {
    const calls = api();
    let finish!: (value: any) => void;
    calls.promote.mockImplementationOnce(
      () =>
        new Promise((r) => {
          finish = r;
        }),
    );
    const registry = createVersionRegistry(calls);
    await registry.refresh();
    registry.openConfirmation('candidate-a');
    registry.state.note = '复核登记理由';
    registry.state.confirmUnpassed = true;
    const pending = registry.submit();
    registry.dispose();
    finish({ status: 'ok', id: 'candidate-a' });
    await pending;
    expect(registry.state.outcome).toBe('');
    expect(calls.load).toHaveBeenCalledTimes(1);
  });
});
