import { labReason } from './displayLabels';

type RecordData = Record<string, unknown>;

export interface LabBookDecisionEvidence {
  pair: string;
  groupLabel: string;
  gateLabel: string;
  pairedSampleLabel: string;
  qualityLabel: string;
  featuresLabel: string;
  featureValidityLabel: string;
}

function record(value: unknown): RecordData | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordData : null;
}

function numberOf(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function labRecordPayload(row: RecordData): RecordData {
  return record(row.payload) ?? row;
}

/** Explain recorded book evidence; missing data never becomes zero or a pass. */
export function labBookDecisionEvidence(row: RecordData): LabBookDecisionEvidence[] {
  const payload = labRecordPayload(row);
  const signals = Array.isArray(payload.signals) ? payload.signals
    : record(payload.signals) ? Object.entries(payload.signals as RecordData)
      .map(([pair, signal]) => ({ ...record(signal), pair })) : [payload];
  return signals.flatMap((value) => {
    const signal = record(value);
    const evidence = record(signal?.evidence ?? signal?.entry_evidence);
    const book = record(evidence?.orderbook);
    if (!book) return [];
    const quality = record(book.quality);
    const features = record(book.features);
    const spread = numberOf(features?.relative_spread_time_mean);
    const obi = numberOf(features?.obi_5_time_mean);
    const consistency = numberOf(features?.lagged_obi_mid_directional_consistency);
    const coverage = numberOf(quality?.coverage_ratio);
    const snapshots = numberOf(quality?.valid_snapshots);
    const groupLabel = book.group === 'control' ? '无订单簿过滤对照'
      : book.group === 'filter' ? '订单簿确认过滤' : '对照方式未知';
    const noNewEntry = book.gate_reason === 'orderbook_confirmation_not_applicable' ||
      ('baseline_target_side' in book && ![1, -1].includes(numberOf(book.baseline_target_side) ?? 0));
    return [{
      pair: typeof signal?.pair === 'string' ? signal.pair : '币种未知',
      groupLabel,
      gateLabel: book.group === 'control' ? '按原 K 线信号运行，不按订单簿拦截'
        : noNewEntry ? '本根无新入场，订单簿确认不适用'
          : book.gate_pass === true ? '入场确认通过'
          : book.gate_pass === false ? `入场确认未通过：${labReason(book.gate_reason)}`
            : `确认结果未知：${labReason(book.gate_reason)}`,
      pairedSampleLabel: book.paired_subsample_eligible === true ? '可用于配对评估过滤贡献'
        : book.paired_subsample_eligible === false ? '不纳入配对过滤贡献样本'
          : '配对样本资格未知',
      featureValidityLabel: book.feature_validity === true ? '本根前向特征有效'
        : book.feature_validity === false ? `本根特征不可用：${labReason(book.gate_reason)}`
          : '本根特征有效性未知',
      qualityLabel: [
        quality?.eligible === true ? '特征质量合格' : quality?.eligible === false ? '特征质量不合格' : '特征质量未知',
        coverage === null ? '覆盖率未知' : `覆盖率 ${(coverage * 100).toFixed(1)}%`,
        snapshots === null ? '有效快照数未知' : `有效快照 ${snapshots}`,
      ].join(' · '),
      featuresLabel: [
        obi === null ? '五档失衡未知' : `五档失衡 ${obi.toFixed(4)}`,
        spread === null ? '相对价差未知' : `相对价差 ${(spread * 100).toFixed(4)}%`,
        consistency === null ? '滞后方向一致性未知' : `滞后方向一致性 ${consistency.toFixed(4)}`,
      ].join(' · '),
    }];
  });
}
