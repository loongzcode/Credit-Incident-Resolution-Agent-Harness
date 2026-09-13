import { displayLabel, fieldLabel } from '../utils/labels';
import { useState } from 'react';
import { Alert, Button, Descriptions, Empty, Tag } from 'antd';
import type { InvestigationFrame, TracePage } from '../types';
import { caseApi } from '../api/caseApi';
import { ErrorPanel, StatusTag } from './common';
import { timeText } from '../utils/format';

export function TracePanel({ page, frame, section, onEvidence }: { page: TracePage; frame: InvestigationFrame; section: string; onEvidence: (id: string) => void }) {
  const [current, setCurrent] = useState(page);
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(false);
  async function more() {
    if (!current.next_cursor) return;
    setLoading(true);
    try {
      const next = await caseApi.trace(frame.case_id, frame.frame_id, section, current.next_cursor);
      if (next.frame_id !== frame.frame_id) throw new Error('Frame binding mismatch');
      setCurrent(next);
    } catch (e) { setError(e as Error); }
    finally { setLoading(false); }
  }
  return <div className="trace-list">
    <p className="trace-count">{current.total} 条已记录事件 · 快照 {frame.frame_id.slice(0, 12)}</p>
    {error && <ErrorPanel error={error} />}
    {!current.items.length && <Empty description="当前快照尚无持久化记录" />}
    {current.items.map(item => <article className="trace-event" key={item.trace_id}>
      <header><Tag>{displayLabel(item.kind)}</Tag><StatusTag value={item.status} /><time>{item.occurred_at ? timeText(item.occurred_at) : '未记录时间'}</time></header>
      {item.historical && <Tag color="purple">历史资料 · 不属于当前证据</Tag>}
      {item.warning && <Alert type="warning" title={displayLabel(item.warning)} />}
      <Descriptions size="small" column={2} items={item.fields.map(f => ({key:f.name, label:fieldLabel(f.name), children:displayLabel(f.value)}))} />
      <div className="trace-evidence">{item.evidence_refs.map(ref => <Button key={ref} size="small" type="link" onClick={() => onEvidence(ref)}>{ref.slice(0, 18)}…</Button>)}</div>
      <code>{item.trace_id}</code>
      {item.related_refs.length > 0 && <p>关联记录： {item.related_refs.join(' · ')}</p>}
    </article>)}
    {current.next_cursor && <Button loading={loading} onClick={() => void more()}>下一页</Button>}
  </div>;
}
