import { Tag } from 'antd';
import { Panel } from './common';
export function PlannerTracePlaceholder() {
  return <Panel title="Planner Trace" extra={<Tag>NOT ENABLED</Tag>}><p>Planner not enabled in UI-0.</p><p className="muted">Step 5 will expose: Model Proposal · Harness Validation · Rejected Candidates · Selected Action</p></Panel>;
}
