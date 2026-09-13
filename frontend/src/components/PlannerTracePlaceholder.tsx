import { Tag } from 'antd';
import { Panel } from './common';
export function PlannerTracePlaceholder() {
  return <Panel title="规划决策记录" extra={<Tag>未启用</Tag>}><p>此占位组件未启用规划记录。</p><p className="muted">规划记录包含：模型建议、运行时校验、被拒候选与最终选择</p></Panel>;
}
