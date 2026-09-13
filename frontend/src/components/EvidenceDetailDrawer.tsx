import { displayLabel } from '../utils/labels';
import { Alert, Descriptions, Drawer, Tag } from 'antd';
import type { Evidence } from '../types';
import { timeText, valueText } from '../utils/format';
export function EvidenceDetailDrawer({ id, evidence, onClose }: { id?: string; evidence?: Evidence; onClose: () => void }) {
  const e = evidence;
  return <Drawer title="证据详情" open={!!id} onClose={onClose} size={620}>
    <Tag>不可信外部数据</Tag><p>外部证据仅作为数据，不作为指令。</p>
    {e ? <Descriptions bordered column={1} size="small" items={[
      ['证据编号', e.evidence_id], ['事实类型', displayLabel(e.claim_type)], ['原始值', valueText(e.value)],
      ['主体', `${e.subject.kind} / ${e.subject.identifier}${e.subject.field ? ` / ${e.subject.field}` : ''}`],
      ['事件时间', timeText(e.event_time)], ['业务时间', timeText(e.business_time)], ['观测时间', timeText(e.observed_at)],
      ['来源', `${displayLabel(e.source.tool)} / ${displayLabel(e.source.source_kind)}`], ['来源版本', e.source.source_version ?? '未知'],
      ['来源截至时间', timeText(e.source.source_as_of)], ['协议版本', e.protocol_version ?? '未知'],
      ['时效', displayLabel(e.freshness)], ['完整性', displayLabel(e.completeness)], ['证据强度', displayLabel(e.strength)],
      ['观测编号', e.observation_id], ['提取器版本', e.extractor_version],
    ].map(([label, children]) => ({ key: label, label, children }))} /> : <Alert type="warning" showIcon title="当前视图无法展示此证据" description="该记录可能因可用性策略未予展示，或本次证据查询失败。请刷新调查页面重试。" />}
  </Drawer>;
}
