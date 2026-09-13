import { Descriptions, Tag } from 'antd';
import type { Case, Context } from '../types';
import { amountText, timeText } from '../utils/format';
import { StatusTag } from './common';

export function CaseHeader({ data, context, identity, investigationAllowed }: { data: Case; context?: Context; identity?: string; investigationAllowed?: boolean }) {
  return <section className="case-header" aria-label="调查任务概览">
    <div className="header-title"><div><div className="eyebrow">任务 / 调查记录</div><h1>{data.case_id}</h1></div><StatusTag value={data.status} /></div>
    <Descriptions size="small" column={4} items={[
      { key: 'order', label: '内部订单号', children: <code>{data.internal_order_id}</code> },
      { key: 'amount', label: '预期放款本金', children: <strong>{data.financial_subject ? amountText(data.financial_subject.expected_principal_minor, data.financial_subject.currency) : '未知'}</strong> },
      { key: 'budget', label: '工具调用预算', children: `${data.budget.used_tool_calls} / ${data.budget.max_tool_calls}` },
      { key: 'identity', label: '支付身份核验', children: identity || context ? <StatusTag value={identity || context!.financial_identity.result} /> : '不可用' },
      { key: 'allowed', label: '是否允许调查', children: investigationAllowed !== undefined || context ? <Tag>{(investigationAllowed ?? context!.budget.investigation_allowed) ? '允许' : '不允许'}</Tag> : '不可用' },
      { key: 'updated', label: '更新时间', span: 3, children: timeText(data.updated_at) },
    ]} />
  </section>;
}
