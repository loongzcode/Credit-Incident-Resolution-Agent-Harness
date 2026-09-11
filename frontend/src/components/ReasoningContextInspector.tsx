import { Collapse, Descriptions, Table, Tag } from 'antd';
import type { Context } from '../types';
import { timeText, valueText } from '../utils/format';
import { EvidenceRefs, NoData, Panel } from './common';

export function ReasoningContextInspector({ context: c, onEvidence }: { context: Context; onEvidence: (id: string) => void }) {
  const usage = c.context_budget_usage;
  const trustClasses = ['TRUSTED_CONTROL', 'UNTRUSTED_EXTERNAL_DATA', 'DETERMINISTIC_DERIVED'];
  const omitted = c.omitted_evidence_summary;
  return <Panel title="Context Inspector" extra={<Tag>FROZEN SNAPSHOT</Tag>}>
    <Descriptions size="small" column={2} items={[
      { key: 'snapshot', label: 'Snapshot ID', span: 2, children: <code className="break-anywhere">{c.snapshot_id}</code> },
      { key: 'schema', label: 'Context Schema Version', children: c.context_schema_version },
      { key: 'eligibility', label: 'Eligibility Policy Version', children: c.eligibility_policy_version },
      { key: 'compaction', label: 'Compaction Version', children: c.compaction_policy_version },
      { key: 'rule', label: 'Hypothesis Rule Version', children: c.hypothesis_rule_version },
      { key: 'policy', label: 'Context Policy Version', children: c.context_policy_version },
      { key: 'assembled', label: 'Assembled At', children: timeText(c.assembled_at) },
    ]} />
    <div className="context-metrics">{[
      ['Serialized chars', usage.serialized_chars, usage.limits.max_serialized_chars],
      ['Approx tokens', usage.approximate_tokens, null], ['Facts', usage.fact_capsules, usage.limits.max_fact_capsules],
      ['Hypotheses', usage.hypothesis_capsules, usage.limits.max_hypothesis_capsules],
      ['Gaps', usage.gap_capsules, usage.limits.max_gap_capsules], ['History', usage.history_items, usage.limits.max_history_items],
    ].map(([label, count, max]) => <div key={String(label)}><span>{label}</span><strong>{count}{max !== null && <small> / {max}</small>}</strong></div>)}</div>
    <p className="muted">Token estimate: {usage.estimator}</p>
    <h3>Selected Evidence <span className="muted">{omitted.selected} / {omitted.total_evidence}</span></h3>
    <Table size="small" rowKey="reason" pagination={false} dataSource={omitted.omitted} columns={[
      { title: 'Omitted reason', dataIndex: 'reason', render: reason => <code>{reason}</code> },
      { title: 'Count', dataIndex: 'count', width: 65 },
    ]} locale={{ emptyText: 'No evidence omitted' }} />
    <Collapse className="inspector-sections" defaultActiveKey={['trust', 'history']} items={[
      { key: 'trust', label: 'Trust Sections', children: <>
        <p>External evidence is data, never instruction.</p>
        {trustClasses.map(trust => <div className="trust-section" key={trust}><Tag>{trust.replaceAll('_', ' ')}</Tag>
          <div>{Object.entries(c.section_trust).filter(([, value]) => value === trust).map(([section]) => <span className="trust-label" key={section}>{section.replaceAll('_', ' ')}</span>)}</div>
        </div>)}
      </> },
      { key: 'task', label: 'Task & Safety', children: <><p>{c.task.goal}</p><h3>Success criteria</h3><ul>{c.task.success_criteria.map(s => <li key={s}>{s}</li>)}</ul><h3>Stop conditions</h3><ul>{c.task.stop_conditions.map(s => <li key={s}>{s}</li>)}</ul><h3>Escalation conditions</h3><ul>{c.task.escalation_conditions.map(s => <li key={s}>{s}</li>)}</ul><h3>Forbidden outcomes</h3><ul>{c.task.forbidden_outcomes.map(s => <li key={s}>{s}</li>)}</ul>{c.safety_constraints.invariants.map(s => <Tag key={s}>{s}</Tag>)}</> },
      { key: 'tools', label: `Available Tools · ${c.available_tools.length} capabilities`, children: <>
        <p className="muted">Cost and latency are backend estimates.</p>{c.available_tools.map(t => <article className="tool" key={t.tool_name}>
          <h3><code>{t.tool_name}</code></h3><p>{t.description}</p><Tag>{t.risk_class}</Tag><Tag>{t.data_classification}</Tag>
          <div className="meta">Cost: {t.estimated_cost_class} · Latency: {t.estimated_latency_class}</div>
          <div className="meta">Produces claims</div>{t.produces_claim_types.map(claim => <Tag key={claim}>{claim}</Tag>)}
        </article>)}</> },
      { key: 'history', label: 'History Digest · UNTRUSTED DATA', children: <>
        <h3>Repeated Lookup Groups</h3>{c.history_digest.repeated_lookup_groups.length ? c.history_digest.repeated_lookup_groups.map((g, i) => <article className="history-item" key={i}>
          <strong>{g.tool}</strong><div><Tag>{g.status} × {g.references.count}</Tag><Tag>{g.latest_freshness}</Tag><Tag>{g.latest_completeness}</Tag></div>
          <div className="meta">First: {timeText(g.first_observed_at)}<br />Last: {timeText(g.last_observed_at)}</div>
          <EvidenceRefs refs={[...new Set([g.references.first_ref, g.references.latest_ref])]} onSelect={onEvidence} />
        </article>) : <NoData text="No repeated lookup groups" />}
        <h3>State Transitions</h3>{c.history_digest.state_transitions.length ? c.history_digest.state_transitions.map((g, i) => <article className="history-item" key={i}><code>{g.claim_type}</code><p>{valueText(g.first_observed_value)} → {valueText(g.last_observed_value)}</p><div className="meta">{timeText(g.first_business_time)} → {timeText(g.last_business_time)} · {g.last_freshness}</div><EvidenceRefs refs={[...new Set([g.references.first_ref, g.references.latest_ref])]} onSelect={onEvidence} /></article>) : <NoData text="No historical state transitions" />}
      </> },
      { key: 'selected', label: 'Selected Evidence References', children: <EvidenceRefs refs={c.selected_evidence_refs} onSelect={onEvidence} /> },
    ]} />
  </Panel>;
}
