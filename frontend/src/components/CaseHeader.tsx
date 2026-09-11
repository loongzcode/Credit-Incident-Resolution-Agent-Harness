import { Descriptions, Tag } from 'antd';
import type { Case, Context } from '../types';
import { amountText, timeText } from '../utils/format';
import { StatusTag } from './common';

export function CaseHeader({ data, context }: { data: Case; context?: Context }) {
  return <section className="case-header" aria-label="Case Header">
    <div className="header-title"><div><div className="eyebrow">CASE / INVESTIGATION RECORD</div><h1>{data.case_id}</h1></div><StatusTag value={data.status} /></div>
    <Descriptions size="small" column={4} items={[
      { key: 'order', label: 'Internal Order ID', children: <code>{data.internal_order_id}</code> },
      { key: 'amount', label: 'Expected Principal', children: <strong>{data.financial_subject ? amountText(data.financial_subject.expected_principal_minor, data.financial_subject.currency) : 'UNKNOWN'}</strong> },
      { key: 'budget', label: 'Tool Budget', children: `${data.budget.used_tool_calls} / ${data.budget.max_tool_calls}` },
      { key: 'identity', label: 'Payment Identity', children: context ? <StatusTag value={context.financial_identity.result} /> : 'Unavailable' },
      { key: 'allowed', label: 'Investigation Allowed', children: context ? <Tag>{String(context.budget.investigation_allowed)}</Tag> : 'Unavailable' },
      { key: 'updated', label: 'Updated At', span: 3, children: timeText(data.updated_at) },
    ]} />
  </section>;
}
