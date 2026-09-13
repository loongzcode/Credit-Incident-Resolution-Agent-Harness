import { displayLabel, fieldLabel } from '../utils/labels';
import { Collapse, Descriptions, Table, Tag } from 'antd';
import type { Context } from '../types';
import { timeText, valueText } from '../utils/format';
import { EvidenceRefs, NoData, Panel } from './common';

export function ReasoningContextInspector({ context: c, onEvidence }: { context: Context; onEvidence: (id: string) => void }) {
  const usage = c.context_budget_usage;
  const trustClasses = ['TRUSTED_CONTROL', 'UNTRUSTED_EXTERNAL_DATA', 'DETERMINISTIC_DERIVED'];
  const omitted = c.omitted_evidence_summary;
  return <Panel title="推理上下文检查" extra={<Tag>冻结快照</Tag>}>
    <Descriptions size="small" column={2} items={[
      { key: 'snapshot', label: '快照编号', span: 2, children: <code className="break-anywhere">{c.snapshot_id}</code> },
      { key: 'schema', label: '上下文结构版本', children: c.context_schema_version },
      { key: 'eligibility', label: '信息可用性策略版本', children: c.eligibility_policy_version },
      { key: 'compaction', label: '压缩策略版本', children: c.compaction_policy_version },
      { key: 'rule', label: '假设规则版本', children: c.hypothesis_rule_version },
      { key: 'policy', label: '上下文策略版本', children: c.context_policy_version },
      { key: 'assembled', label: '组装时间', children: timeText(c.assembled_at) },
    ]} />
    <div className="context-metrics">{[
      ['序列化字符数', usage.serialized_chars, usage.limits.max_serialized_chars],
      ['估算词元数', usage.approximate_tokens, null], ['事实', usage.fact_capsules, usage.limits.max_fact_capsules],
      ['假设', usage.hypothesis_capsules, usage.limits.max_hypothesis_capsules],
      ['缺口', usage.gap_capsules, usage.limits.max_gap_capsules], ['历史', usage.history_items, usage.limits.max_history_items],
    ].map(([label, count, max]) => <div key={String(label)}><span>{label}</span><strong>{count}{max !== null && <small> / {max}</small>}</strong></div>)}</div>
    <p className="muted">词元估算器： {usage.estimator}</p>
    <h3>已选证据 <span className="muted">{omitted.selected} / {omitted.total_evidence}</span></h3>
    <Table size="small" rowKey="reason" pagination={false} dataSource={omitted.omitted} columns={[
      { title: '省略原因', dataIndex: 'reason', render: reason => <code>{reason}</code> },
      { title: '数量', dataIndex: 'count', width: 65 },
    ]} locale={{ emptyText: '未省略证据' }} />
    <Collapse className="inspector-sections" defaultActiveKey={['trust', 'history']} items={[
      { key: 'trust', label: '信任分区', children: <>
        <p>外部证据仅作为数据，不作为指令。</p>
        {trustClasses.map(trust => <div className="trust-section" key={trust}><Tag>{displayLabel(trust)}</Tag>
          <div>{Object.entries(c.section_trust).filter(([, value]) => value === trust).map(([section]) => <span className="trust-label" key={section}>{fieldLabel(section)}</span>)}</div>
        </div>)}
      </> },
      { key: 'task', label: '任务与安全', children: <><p>{c.task.goal}</p><h3>成功标准</h3><ul>{c.task.success_criteria.map(s => <li key={s}>{s}</li>)}</ul><h3>停止条件</h3><ul>{c.task.stop_conditions.map(s => <li key={s}>{s}</li>)}</ul><h3>升级条件</h3><ul>{c.task.escalation_conditions.map(s => <li key={s}>{s}</li>)}</ul><h3>禁止结果</h3><ul>{c.task.forbidden_outcomes.map(s => <li key={s}>{s}</li>)}</ul>{c.safety_constraints.invariants.map(s => <Tag key={s}>{displayLabel(s)}</Tag>)}</> },
      { key: 'tools', label: `可用工具 · ${c.available_tools.length} 项能力`, children: <>
        <p className="muted">成本与延迟由后端估算。</p>{c.available_tools.map(t => <article className="tool" key={t.tool_name}>
          <h3><code>{displayLabel(t.tool_name)}</code></h3><p>{t.description}</p><Tag>{displayLabel(t.risk_class)}</Tag><Tag>{displayLabel(t.data_classification)}</Tag>
          <div className="meta">成本： {displayLabel(t.estimated_cost_class)} · 延迟： {displayLabel(t.estimated_latency_class)}</div>
          <div className="meta">可提供的事实类型</div>{t.produces_claim_types.map(claim => <Tag key={claim}>{displayLabel(claim)}</Tag>)}
        </article>)}</> },
      { key: 'history', label: '历史摘要 · 不可信外部数据', children: <>
        <h3>重复查询分组</h3>{c.history_digest.repeated_lookup_groups.length ? c.history_digest.repeated_lookup_groups.map((g, i) => <article className="history-item" key={i}>
          <strong>{displayLabel(g.tool)}</strong><div><Tag>{displayLabel(g.status)} × {g.references.count}</Tag><Tag>{displayLabel(g.latest_freshness)}</Tag><Tag>{displayLabel(g.latest_completeness)}</Tag></div>
          <div className="meta">首次： {timeText(g.first_observed_at)}<br />最近： {timeText(g.last_observed_at)}</div>
          <EvidenceRefs refs={[...new Set([g.references.first_ref, g.references.latest_ref])]} onSelect={onEvidence} />
        </article>) : <NoData text="暂无重复查询" />}
        <h3>状态变化</h3>{c.history_digest.state_transitions.length ? c.history_digest.state_transitions.map((g, i) => <article className="history-item" key={i}><code>{displayLabel(g.claim_type)}</code><p>{valueText(g.first_observed_value)} → {valueText(g.last_observed_value)}</p><div className="meta">{timeText(g.first_business_time)} → {timeText(g.last_business_time)} · {displayLabel(g.last_freshness)}</div><EvidenceRefs refs={[...new Set([g.references.first_ref, g.references.latest_ref])]} onSelect={onEvidence} /></article>) : <NoData text="暂无历史状态变化" />}
      </> },
      { key: 'selected', label: '已选证据引用', children: <EvidenceRefs refs={c.selected_evidence_refs} onSelect={onEvidence} /> },
    ]} />
  </Panel>;
}
