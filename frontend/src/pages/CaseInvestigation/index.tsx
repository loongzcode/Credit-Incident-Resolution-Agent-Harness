import { useState } from 'react';
import { Alert, Button, Input, Skeleton, Space, Switch, Tag } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useInvestigation } from '../../hooks/useInvestigation';
import { CaseHeader } from '../../components/CaseHeader';
import { SafetyBanner } from '../../components/SafetyBanner';
import { CurrentFactsPanel } from '../../components/CurrentFactsPanel';
import { FinancialIdentityPanel } from '../../components/FinancialIdentityPanel';
import { HypothesisGraphPanel } from '../../components/HypothesisGraphPanel';
import { EvidenceTimeline } from '../../components/EvidenceTimeline';
import { EvidenceDetailDrawer } from '../../components/EvidenceDetailDrawer';
import { EvidenceGapPanel } from '../../components/EvidenceGapPanel';
import { ReasoningContextInspector } from '../../components/ReasoningContextInspector';
import { PlannerTracePlaceholder } from '../../components/PlannerTracePlaceholder';
import { ErrorPanel, Panel, QueryPanel } from '../../components/common';

export function CaseInvestigation() {
  const { caseId = '' } = useParams();
  const [polling, setPolling] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<string>();
  const q = useInvestigation(caseId, polling);
  // A failed refresh must never leave an old financial conclusion visible as current.
  const context = q.context.isError ? undefined : q.context.data;
  const graph = q.graph.isError ? undefined : q.graph.data;
  const evidence = q.evidence.isError ? undefined : q.evidence.data;
  return <main>
    <div className="page-toolbar"><Link to="/cases">← Case lookup</Link><Space><span>Poll every 5s</span><Switch aria-label="Poll every 5 seconds" checked={polling} onChange={setPolling} /><Button onClick={() => void q.refresh()} loading={q.refreshing}>Refresh</Button></Space></div>
    {q.caseQuery.error ? <ErrorPanel error={q.caseQuery.error} /> : q.caseQuery.data ? <CaseHeader data={q.caseQuery.data} context={context} /> : <Panel title="Case Header"><Skeleton active /></Panel>}
    {q.refreshing && context && <div className="refresh-notice" role="status">Refreshing investigation · showing the previous snapshot until requests complete</div>}
    {context ? <SafetyBanner context={context} /> : q.context.error && <Alert className="safety-banner" type="warning" showIcon title="Safety context unavailable" description="Current identity and safety constraints cannot be displayed until a valid context is available." />}
    <div className="investigation-grid">
      <div className="investigation-column">
      <QueryPanel title="Current Facts" pending={q.context.isPending} error={q.context.error}>{context && <CurrentFactsPanel facts={context.current_facts} onEvidence={setSelectedEvidence} />}</QueryPanel>
      <QueryPanel title="Evidence Timeline" pending={q.evidence.isPending} error={q.evidence.error}>{evidence && <EvidenceTimeline data={evidence} onEvidence={setSelectedEvidence} />}</QueryPanel>
      <QueryPanel title="Financial Identity" pending={q.context.isPending} error={q.context.error}>{context && <FinancialIdentityPanel identity={context.financial_identity} onEvidence={setSelectedEvidence} />}</QueryPanel>
      </div>
      <div className="investigation-column">
      <QueryPanel title="Hypothesis Graph" pending={q.graph.isPending} error={q.graph.error}>{graph && <HypothesisGraphPanel graph={graph} onEvidence={setSelectedEvidence} />}</QueryPanel>
      <QueryPanel title="Evidence Gaps" pending={q.graph.isPending} error={q.graph.error}>{graph && <EvidenceGapPanel gaps={graph.open_gaps} />}</QueryPanel>
      <QueryPanel title="Context Inspector" pending={q.context.isPending} error={q.context.error}>{context && <ReasoningContextInspector context={context} onEvidence={setSelectedEvidence} />}</QueryPanel>
      </div>
    </div>
    <PlannerTracePlaceholder />
    <EvidenceDetailDrawer id={selectedEvidence} evidence={evidence?.items.find(e => e.evidence_id === selectedEvidence)} onClose={() => setSelectedEvidence(undefined)} />
  </main>;
}

export function CaseLookup() {
  const [id, setId] = useState('CASE-JD202609100001');
  const navigate = useNavigate();
  return <main className="lookup"><Panel title="Open an investigation" extra={<Tag>READ ONLY</Tag>}><p>Enter a Case ID to inspect facts, evidence, hypotheses and reasoning context.</p>
    <form onSubmit={e => { e.preventDefault(); if (id.trim()) navigate(`/cases/${encodeURIComponent(id.trim())}`); }}><Space.Compact block><Input aria-label="Case ID" value={id} onChange={e => setId(e.target.value)} /><Button htmlType="submit" type="primary">Open Case</Button></Space.Compact></form>
  </Panel></main>;
}
