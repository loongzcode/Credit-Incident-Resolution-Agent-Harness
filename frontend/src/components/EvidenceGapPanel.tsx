import { Tag } from 'antd';
import type { Gap } from '../types';
import { NoData, Panel, StatusTag } from './common';
const priority = { SAFETY_CRITICAL: 0, DISCRIMINATING: 1, SUPPORTING: 2 };
export function EvidenceGapPanel({ gaps }: { gaps: Gap[] }) {
  return <Panel title="Evidence Gaps" extra={<Tag>{gaps.length} OPEN</Tag>}><div className="gaps-scroll">
    {gaps.length ? [...gaps].sort((a, b) => priority[a.priority_class] - priority[b.priority_class]).map(gap => <article className="gap" key={gap.gap_id}>
      <div><StatusTag value={gap.priority_class} /><StatusTag value={gap.status} /></div>
      <h3>{gap.question}</h3><code className="muted">{gap.gap_id}</code>
      <div className="meta">Related hypotheses: {gap.hypothesis_ids.join(' · ')}</div>
      <div className="meta">Required claim types</div><div>{gap.required_claim_types.map(c => <Tag key={c}>{c}</Tag>)}</div>
    </article>) : <NoData text="No open evidence gaps" />}
  </div></Panel>;
}
