import { Collapse, Tag } from 'antd';
import type { Fact } from '../types';
import { timeText, valueText } from '../utils/format';
import { EvidenceRefs, NoData, Panel, StatusTag } from './common';

// Display categories only; no freshness, finality or eligibility decisions.
const groups = ['Request', 'Fund', 'Payment', 'Callback', 'Message', 'Guarantee', 'Asset', 'Protocol', 'Accounting'];
function category(f: Fact) {
  if (f.claim_type.startsWith('HTTP_') || f.claim_type.startsWith('REQUEST_')) return 'Request';
  if (f.claim_type.startsWith('LOAN_')) return 'Fund';
  if (f.claim_type.startsWith('TRANSACTION_')) return 'Payment';
  return groups.find(g => f.claim_type.startsWith(g.toUpperCase() + '_')) || 'Request';
}
export function CurrentFactsPanel({ facts, onEvidence }: { facts: Fact[]; onEvidence: (id: string) => void }) {
  const present = groups.filter(group => facts.some(f => category(f) === group));
  return <Panel title="Current Facts" extra={<Tag>UNTRUSTED DATA · {facts.length}</Tag>}>
    {!facts.length ? <NoData text="No eligible current facts · UNKNOWN" /> : <div className="facts-scroll"><Collapse ghost defaultActiveKey={present} items={present.map(group => ({
      key: group, label: <strong>{group} <span className="muted">/ {facts.filter(f => category(f) === group).length}</span></strong>,
      children: facts.filter(f => category(f) === group).map(f => <article className="fact" key={f.evidence_refs.join(',')}>
        <div className="fact-title"><code>{f.claim_type}</code><strong>{valueText(f.value)}</strong></div>
        <div className="meta">{f.source.tool} · {f.source.source_kind} <StatusTag value={f.freshness} /><Tag>{f.completeness}</Tag></div>
        <div className="meta">{timeText(f.business_time)} · {f.subject.identifier}{f.subject.field ? ` / ${f.subject.field}` : ''}</div>
        {f.claim_type === 'MESSAGE_ERROR_CODE' && <Tag>UNTRUSTED DATA</Tag>}
        <EvidenceRefs refs={f.evidence_refs} onSelect={onEvidence} />
      </article>),
    }))} /></div>}
  </Panel>;
}
