import { Alert, Button, Card, Empty, Skeleton, Tag } from 'antd';
import type { ReactNode } from 'react';
import { errorMessage } from '../api/caseApi';

export function StatusTag({ value }: { value: string }) {
  const colors: Record<string, string> = { MATCH: 'success', CONFIRMED: 'success', SUPPORTED: 'processing',
    MISMATCH: 'error', SAFETY_CRITICAL: 'error', UNKNOWN: 'warning', POSSIBLE: 'warning',
    ELIMINATED: 'default', DISCRIMINATING: 'warning', CURRENT: 'success', STALE: 'warning',
    INVESTIGATING: 'processing', OPEN: 'warning' };
  return <Tag color={colors[value]}>{value}</Tag>;
}
export function Panel({ title, extra, children, className = '' }: { title: string; extra?: ReactNode; children: ReactNode; className?: string }) {
  return <Card className={`panel ${className}`} title={<h2>{title}</h2>} extra={extra}>{children}</Card>;
}
export function ErrorPanel({ error }: { error: Error }) {
  const message = errorMessage(error);
  return <Alert showIcon type="error" title={message.title} description={message.detail} />;
}
export function QueryPanel({ title, pending, error, children }: { title: string; pending: boolean; error: Error | null; children: ReactNode }) {
  if (error) return <Panel title={title}><ErrorPanel error={error} /></Panel>;
  if (pending) return <Panel title={title}><Skeleton active paragraph={{ rows: 5 }} /></Panel>;
  return <>{children}</>;
}
export function EvidenceRefs({ refs, onSelect }: { refs: string[]; onSelect: (id: string) => void }) {
  return refs.length ? <div className="refs">{refs.map(id => <Button type="link" key={id} onClick={() => onSelect(id)} title={id}>{id}</Button>)}</div> : <span className="muted">No evidence refs</span>;
}
export function NoData({ text = 'No observations available' }: { text?: string }) { return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text} />; }
