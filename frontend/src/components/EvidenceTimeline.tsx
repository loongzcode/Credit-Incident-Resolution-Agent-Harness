import { displayLabel } from '../utils/labels';
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
  const options = (values: string[]) => [...new Set(values)].sort().map(value => ({ value, label: displayLabel(value) }));
  const items = useMemo(() => data.items.filter(e => (!tool || e.source.tool === tool) && (!claim || e.claim_type === claim) && (!freshness || e.freshness === freshness) && (!source || e.source.source_kind === source) && `${e.evidence_id} ${e.claim_type} ${displayLabel(e.claim_type)}`.toLowerCase().includes(search.trim().toLowerCase()))
    .sort((a, b) => Date.parse(b.business_time) - Date.parse(a.business_time) || Date.parse(b.observed_at) - Date.parse(a.observed_at) || a.evidence_id.localeCompare(b.evidence_id)), [data, search, tool, claim, freshness, source]);
  return <Panel title="证据时间线" extra={<Tag>{data.items.length} / {data.total} 可见</Tag>}>
    <div className="filters"><Input aria-label="搜索证据编号或事实类型" placeholder="搜索证据编号或事实类型" value={search} onChange={e => setSearch(e.target.value)} allowClear />
      <Select aria-label="筛选工具" placeholder="工具" allowClear value={tool} onChange={setTool} options={options(data.items.map(e => e.source.tool))} />
      <Select aria-label="筛选事实类型" placeholder="事实类型" allowClear value={claim} onChange={setClaim} options={options(data.items.map(e => e.claim_type))} />
      <Select aria-label="筛选时效" placeholder="时效" allowClear value={freshness} onChange={setFreshness} options={options(data.items.map(e => e.freshness))} />
      <Select aria-label="筛选来源类型" placeholder="来源类型" allowClear value={source} onChange={setSource} options={options(data.items.map(e => e.source.source_kind))} /></div>
    {!!data.eligibility_denied_count && <p className="muted">{data.eligibility_denied_count} 条证据因后端可用性策略未予展示。</p>}
    <Table<Evidence> size="small" rowKey="evidence_id" dataSource={items} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 760 }} columns={[
      { title: '业务时间 / 观测时间', key: 'time', width: 190, render: (_, e) => <><div>{timeText(e.business_time)}</div><small className="muted">{timeText(e.observed_at)}</small></> },
      { title: '事实类型 / 值', key: 'claim', width: 280, render: (_, e) => <><code>{displayLabel(e.claim_type)}</code><div><strong>{valueText(e.value)}</strong></div><small className="muted">{displayLabel(e.source.tool)} · {e.subject.identifier}</small></> },
      { title: '证据质量', key: 'quality', width: 120, render: (_, e) => <><StatusTag value={e.freshness} /><Tag>{displayLabel(e.completeness)}</Tag></> },
      { title: '证据编号', key: 'id', render: (_, e) => <Button className="evidence-id" type="link" title={e.evidence_id} onClick={() => onEvidence(e.evidence_id)}>{e.evidence_id}</Button> },
    ]} />
  </Panel>;
}
