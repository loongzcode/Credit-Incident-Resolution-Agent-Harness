import { Alert, Tag } from 'antd';
import type { Context } from '../types';

export function SafetyBanner({ context }: { context: Context }) {
  const alert = context.financial_identity.result !== 'MATCH' || context.open_evidence_gaps.some(g => g.priority === 'SAFETY_CRITICAL' && g.status === 'OPEN');
  if (!alert) return null;
  return <Alert className="safety-banner" showIcon type="warning" title="Safety Alert · Backend safety invariants" description={
    <div>{context.safety_constraints.invariants.map(invariant => <Tag key={invariant}>{invariant}</Tag>)}</div>
  } />;
}
