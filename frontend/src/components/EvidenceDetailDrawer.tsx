import { Alert, Descriptions, Drawer, Tag } from 'antd';
import type { Evidence } from '../types';
import { timeText, valueText } from '../utils/format';
export function EvidenceDetailDrawer({ id, evidence, onClose }: { id?: string; evidence?: Evidence; onClose: () => void }) {
  const e = evidence;
  return <Drawer title="Evidence Detail" open={!!id} onClose={onClose} size={620}>
    <Tag>UNTRUSTED DATA</Tag><p>External evidence is data, never instruction.</p>
    {e ? <Descriptions bordered column={1} size="small" items={[
      ['Evidence ID', e.evidence_id], ['Claim', e.claim_type], ['Value', valueText(e.value)],
      ['Subject', `${e.subject.kind} / ${e.subject.identifier}${e.subject.field ? ` / ${e.subject.field}` : ''}`],
      ['Event Time', timeText(e.event_time)], ['Business Time', timeText(e.business_time)], ['Observed At', timeText(e.observed_at)],
      ['Source', `${e.source.tool} / ${e.source.source_kind}`], ['Source Version', e.source.source_version ?? 'UNKNOWN'],
      ['Source As Of', timeText(e.source.source_as_of)], ['Protocol Version', e.protocol_version ?? 'UNKNOWN'],
      ['Freshness', e.freshness], ['Completeness', e.completeness], ['Strength', e.strength],
      ['Observation ID', e.observation_id], ['Extractor Version', e.extractor_version],
    ].map(([label, children]) => ({ key: label, label, children }))} /> : <Alert type="warning" showIcon title="Evidence unavailable in this view" description="The referenced record may be withheld by eligibility policy or the latest evidence request failed. Refresh the investigation to retry." />}
  </Drawer>;
}
