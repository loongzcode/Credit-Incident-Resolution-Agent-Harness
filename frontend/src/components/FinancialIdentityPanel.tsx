import { displayLabel } from '../utils/labels';
import { Descriptions, Tag } from 'antd';
import type { Identity } from '../types';
import { EvidenceRefs, Panel, StatusTag } from './common';

export function FinancialIdentityPanel({ identity, onEvidence }: { identity: Identity; onEvidence: (id: string) => void }) {
  return <Panel title="资金身份绑定" extra={<StatusTag value={identity.result} />}>
    {!!identity.mismatch_dimensions.length && <div className="identity-warning"><strong>不一致维度</strong><div>{identity.mismatch_dimensions.map(d => <Tag color="error" key={d}>{displayLabel(d)}</Tag>)}</div></div>}
    {!!identity.unknown_dimensions.length && <div className="identity-warning"><strong>未确认维度</strong><div>{identity.unknown_dimensions.map(d => <Tag color="warning" key={d}>{displayLabel(d)}</Tag>)}</div></div>}
    <Descriptions size="small" column={1} items={[
      { key: 'count', label: '候选交易数', children: identity.candidate_transaction_count },
      { key: 'preview', label: '交易引用预览', children: identity.transaction_ref_preview.join(', ') || '尚未观测到' },
      { key: 'version', label: '核验规则版本', children: identity.verification_version },
      { key: 'refs', label: '证据数量', children: identity.evidence_ref_count },
    ]} />
    <h3>关键证据引用</h3><EvidenceRefs refs={identity.evidence_refs} onSelect={onEvidence} />
  </Panel>;
}
