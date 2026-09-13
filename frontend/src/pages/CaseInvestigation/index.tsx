import { displayLabel, fieldLabel } from '../../utils/labels';
import { useState } from 'react';
import { Alert, Button, Input, Skeleton, Space, Switch, Tag, Tabs, Descriptions } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useInvestigation } from '../../hooks/useInvestigation';
import { CaseHeader } from '../../components/CaseHeader';
import { CurrentFactsPanel } from '../../components/CurrentFactsPanel';
import { FinancialIdentityPanel } from '../../components/FinancialIdentityPanel';
import { HypothesisGraphPanel } from '../../components/HypothesisGraphPanel';
import { FrameEvidenceBrowser } from '../../components/FrameEvidenceBrowser';
import { EvidenceDetailDrawer } from '../../components/EvidenceDetailDrawer';
import { EvidenceGapPanel } from '../../components/EvidenceGapPanel';
import { TracePanel } from '../../components/TracePanel';
import { ErrorPanel, Panel, StatusTag } from '../../components/common';
import { timeText } from '../../utils/format';
import { useQuery } from '@tanstack/react-query';
import { caseApi } from '../../api/caseApi';

export function CaseInvestigation() {
  const { caseId = '' } = useParams();
  const [polling, setPolling] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<string>();
  const q = useInvestigation(caseId, polling);
  const frame = q.isError ? undefined : q.data;
  const detail = useQuery({queryKey:['frame-evidence', caseId, frame?.frame_id, selectedEvidence],
    queryFn: ({signal}) => caseApi.evidenceDetail(caseId, frame!.frame_id, selectedEvidence!, signal),
    enabled: !!frame && !!selectedEvidence && !frame.evidence.items.some(e => e.evidence_id === selectedEvidence), retry:false});
  return <main>
    <div className="page-toolbar"><Link to="/cases">← 查询调查任务</Link><Space><Tag>只读</Tag><span>每 5 秒自动刷新</span><Switch aria-label="每 5 秒自动刷新" checked={polling} onChange={setPolling} /><Button autoInsertSpace={false} onClick={() => void q.refetch()} loading={q.isFetching}>刷新</Button></Space></div>
    {q.error && <><Alert type="warning" title="安全上下文不可用" description="当前调查快照不可用，历史资金结论不会作为当前结论展示。" /><ErrorPanel error={q.error} /></>}
    {q.isPending && <Skeleton active />}
    {frame && <div key={frame.frame_id}>
      <CaseHeader data={frame.case_summary} identity={frame.financial_identity.result} investigationAllowed={frame.investigation_allowed} />
      <div className="frame-watermark"><strong>调查快照</strong><code>{frame.frame_id.slice(0, 16)}</code><span>任务修订时间 {timeText(frame.case_revision)}</span><span>系统清单版本 {frame.registry_version?.slice(0, 12) || '未登记'}</span><span>路由版本 {frame.route_revision?.slice(0, 12) || '未登记'}</span><span>组装时间 {timeText(frame.assembled_at)}</span></div>
      <Descriptions size="small" items={frame.route_summary.map(f => ({key:f.name,label:fieldLabel(f.name),children:displayLabel(f.value)}))} />
      {q.isFetching && <Alert type="info" title="正在刷新 · 新快照就绪前保留上一份完整快照" />}
      <Alert className="safety-banner" type={frame.financial_identity.result === 'MATCH' ? 'info' : 'warning'} showIcon title={frame.financial_identity.result === 'MATCH' ? '支付身份一致' : `支付身份${displayLabel(frame.financial_identity.result)} / 未确认`} description="未知 ≠ 失败 · 工具调用成功 ≠ 业务成功 · 禁止创建新的资金意图 · 已应用 ≠ 已验证" />
      <section className="truth-grid" aria-label="资金事实">
        {frame.financial_truth.map(t => <article className="truth-card" key={t.claim_type}><div className="eyebrow">{displayLabel(t.claim_type)}</div><StatusTag value={String(t.value)} /><small>{t.status === 'UNKNOWN' ? '未知 / 尚未获得充分证据' : displayLabel(t.status)}</small><div>{t.evidence_refs.slice(0, 3).map(ref => <Button key={ref} size="small" type="link" onClick={() => setSelectedEvidence(ref)}>{ref.slice(0, 15)}…</Button>)}</div></article>)}
      </section>
      <div className="investigation-grid">
        <div><FinancialIdentityPanel identity={frame.financial_identity} onEvidence={setSelectedEvidence} /><Descriptions size="small" column={2} items={frame.financial_identity_dimensions.map(d => ({key:d.name,label:displayLabel(d.name),children:<StatusTag value={d.value} />}))} /></div>
        <Panel title="待补充证据"><EvidenceGapPanel gaps={frame.hypotheses.open_gaps} /><Descriptions size="small" column={1} items={frame.gap_capabilities.map(g => ({key:g.gap_id, label:displayLabel(g.gap_id.split(':').at(-1) || g.gap_id), children:g.available_tools.map(displayLabel).join('、') || displayLabel(g.unavailable_reason || 'UNKNOWN')}))} /></Panel>
      </div>
      <HypothesisGraphPanel graph={frame.hypotheses} onEvidence={setSelectedEvidence} />
      <Panel title="统一调查时间线"><TracePanel page={frame.timeline} frame={frame} section="timeline" onEvidence={setSelectedEvidence} /></Panel>
      <Tabs defaultActiveKey="planner-runs" items={[
        { key:'evidence', label:'当前证据', children:<><p>{frame.current_evidence.length} / {frame.current_evidence_total} 条当前事实 · 其余事实可在证据分页中查看</p><CurrentFactsPanel facts={frame.current_evidence} onEvidence={setSelectedEvidence} /><FrameEvidenceBrowser frame={frame} onEvidence={setSelectedEvidence} /></> },
        ...([['规划决策记录', 'planner-runs', frame.planner_trace], ['工具调用记录', 'tools', frame.tool_trace], ['系统来源', 'sources', frame.registry_source_trace], ['路由版本记录', 'routes', frame.route_trace], ['知识检索', 'knowledge', frame.knowledge_retrieval_trace], ['任务编排', 'work', frame.work_trace], ['副作用记录', 'effects', frame.side_effect_trace], ['故障恢复', 'recovery', frame.recovery_trace], ['独立评估', 'evaluations', frame.evaluation_trace], ['结案记录', 'closure', frame.closure_trace]] as const).map(([label, section, page]) => ({key:section, label, children:<TracePanel page={page} frame={frame} section={section} onEvidence={setSelectedEvidence} />}))
      ]} />
      {detail.error && <ErrorPanel error={detail.error} />}
      <EvidenceDetailDrawer id={selectedEvidence} evidence={frame.evidence.items.find(e => e.evidence_id === selectedEvidence) || detail.data} onClose={() => setSelectedEvidence(undefined)} />
    </div>}
  </main>;
}

export function CaseLookup() {
  const [id, setId] = useState('CASE-JD202609100001');
  const navigate = useNavigate();
  return <main className="lookup"><Panel title="查询调查任务" extra={<Tag>只读</Tag>}><p>输入调查任务编号，查看资金事实、证据、故障假设与调查记录。</p>
    <form onSubmit={e => { e.preventDefault(); if (id.trim()) navigate(`/cases/${encodeURIComponent(id.trim())}`); }}><Space.Compact block><Input aria-label="调查任务编号" value={id} onChange={e => setId(e.target.value)} /><Button htmlType="submit" type="primary">打开任务</Button></Space.Compact></form>
  </Panel></main>;
}
