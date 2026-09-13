import { displayLabel } from '../utils/labels';
import { Tag } from 'antd';
import type { Gap } from '../types';
import { NoData, Panel, StatusTag } from './common';
const priority = { SAFETY_CRITICAL: 0, DISCRIMINATING: 1, SUPPORTING: 2 };
export function EvidenceGapPanel({ gaps }: { gaps: Gap[] }) {
  return <Panel title="证据缺口" extra={<Tag>{gaps.length} 项待补充</Tag>}><div className="gaps-scroll">
    {gaps.length ? [...gaps].sort((a, b) => priority[a.priority_class] - priority[b.priority_class]).map(gap => <article className="gap" key={gap.gap_id}>
      <div><StatusTag value={gap.priority_class} /><StatusTag value={gap.status} /></div>
      <h3>{gap.question}</h3><code className="muted">{gap.gap_id}</code>
      <div className="meta">关联假设： {gap.hypothesis_ids.join(' · ')}</div>
      <div className="meta">所需事实类型</div><div>{gap.required_claim_types.map(c => <Tag key={c}>{displayLabel(c)}</Tag>)}</div>
    </article>) : <NoData text="暂无待补充的证据缺口" />}
  </div></Panel>;
}
