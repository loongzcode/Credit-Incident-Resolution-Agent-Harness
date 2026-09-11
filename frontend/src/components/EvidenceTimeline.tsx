import { useMemo, useState } from 'react';
import { Button, Input, Select, Table, Tag } from 'antd';
import type { Evidence, EvidenceList } from '../types';
import { timeText, valueText } from '../utils/format';
import { Panel, StatusTag } from './common';

export function EvidenceTimeline({ data, onEvidence }: { data: EvidenceList; onEvidence: (id: string) => void }) {
  const [search, setSearch] = useState('');
  const [tool, setTool] = useState<string>();
  const [claim, setClaim] = useState<string>();
  const [freshness, setFreshness] = useState<string>();
  const [source, setSource] = useState<string>();
  const options = (values: string[]) => [...new Set(values)].sort().map(value => ({ value, label: value }));
  const items = useMemo(() => data.items.filter(e => (!tool || e.source.tool === tool) && (!claim || e.claim_type === claim) && (!freshness || e.freshness === freshness) && (!source || e.source.source_kind === source) && `${e.evidence_id} ${e.claim_type}`.toLowerCase().includes(search.trim().toLowerCase()))
    .sort((a, b) => Date.parse(b.business_time) - Date.parse(a.business_time) || Date.parse(b.observed_at) - Date.parse(a.observed_at) || a.evidence_id.localeCompare(b.evidence_id)), [data, search, tool, claim, freshness, source]);
  return <Panel title="Evidence Timeline" extra={<Tag>{data.items.length} / {data.total} VISIBLE</Tag>}>
    <div className="filters"><Input aria-label="Search evidence ID or claim type" placeholder="Search Evidence ID or Claim Type" value={search} onChange={e => setSearch(e.target.value)} allowClear />
      <Select aria-label="Filter tool" placeholder="Tool" allowClear value={tool} onChange={setTool} options={options(data.items.map(e => e.source.tool))} />
      <Select aria-label="Filter claim type" placeholder="Claim Type" allowClear value={claim} onChange={setClaim} options={options(data.items.map(e => e.claim_type))} />
      <Select aria-label="Filter freshness" placeholder="Freshness" allowClear value={freshness} onChange={setFreshness} options={options(data.items.map(e => e.freshness))} />
      <Select aria-label="Filter source kind" placeholder="Source Kind" allowClear value={source} onChange={setSource} options={options(data.items.map(e => e.source.source_kind))} /></div>
    {!!data.eligibility_denied_count && <p className="muted">{data.eligibility_denied_count} evidence records withheld by backend eligibility policy.</p>}
    <Table<Evidence> size="small" rowKey="evidence_id" dataSource={items} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 760 }} columns={[
      { title: 'Business / observed time', key: 'time', width: 190, render: (_, e) => <><div>{timeText(e.business_time)}</div><small className="muted">{timeText(e.observed_at)}</small></> },
      { title: 'Claim / value', key: 'claim', width: 280, render: (_, e) => <><code>{e.claim_type}</code><div><strong>{valueText(e.value)}</strong></div><small className="muted">{e.source.tool} · {e.subject.identifier}</small></> },
      { title: 'Quality', key: 'quality', width: 120, render: (_, e) => <><StatusTag value={e.freshness} /><Tag>{e.completeness}</Tag></> },
      { title: 'Evidence ID', key: 'id', render: (_, e) => <Button className="evidence-id" type="link" title={e.evidence_id} onClick={() => onEvidence(e.evidence_id)}>{e.evidence_id}</Button> },
    ]} />
  </Panel>;
}
