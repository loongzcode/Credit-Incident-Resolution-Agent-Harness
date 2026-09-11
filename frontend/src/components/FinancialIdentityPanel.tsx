import { Descriptions, Tag } from 'antd';
import type { Identity } from '../types';
import { EvidenceRefs, Panel, StatusTag } from './common';

export function FinancialIdentityPanel({ identity, onEvidence }: { identity: Identity; onEvidence: (id: string) => void }) {
  return <Panel title="Financial Identity" extra={<StatusTag value={identity.result} />}>
    {!!identity.mismatch_dimensions.length && <div className="identity-warning"><strong>Mismatch dimensions</strong><div>{identity.mismatch_dimensions.map(d => <Tag color="error" key={d}>{d}</Tag>)}</div></div>}
    {!!identity.unknown_dimensions.length && <div className="identity-warning"><strong>Unknown dimensions</strong><div>{identity.unknown_dimensions.map(d => <Tag color="warning" key={d}>{d}</Tag>)}</div></div>}
    <Descriptions size="small" column={1} items={[
      { key: 'count', label: 'Candidate transaction count', children: identity.candidate_transaction_count },
      { key: 'preview', label: 'Transaction preview', children: identity.transaction_ref_preview.join(', ') || 'None observed' },
      { key: 'version', label: 'Verification version', children: identity.verification_version },
      { key: 'refs', label: 'Evidence count', children: identity.evidence_ref_count },
    ]} />
    <h3>Critical evidence refs</h3><EvidenceRefs refs={identity.evidence_refs} onSelect={onEvidence} />
  </Panel>;
}
