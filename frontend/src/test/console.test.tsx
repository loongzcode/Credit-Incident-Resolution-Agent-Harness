import { fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { Case, Context, EvidenceList, Graph } from '../types';
import s6raw from './fixtures/s6.json';
import s8raw from './fixtures/s8.json';
import { CaseHeader } from '../components/CaseHeader';
import { CurrentFactsPanel } from '../components/CurrentFactsPanel';
import { FinancialIdentityPanel } from '../components/FinancialIdentityPanel';
import { EvidenceGapPanel } from '../components/EvidenceGapPanel';
import { HypothesisDetail } from '../components/HypothesisGraphPanel';
import { ReasoningContextInspector } from '../components/ReasoningContextInspector';
import { SafetyBanner } from '../components/SafetyBanner';
import { EvidenceTimeline } from '../components/EvidenceTimeline';
import { EvidenceDetailDrawer } from '../components/EvidenceDetailDrawer';
import { CaseInvestigation } from '../pages/CaseInvestigation';
import { ApiError, caseApi, errorMessage } from '../api/caseApi';

vi.mock('@xyflow/react', () => ({
  ReactFlow: () => <div>Hypothesis hierarchy</div>, Background: () => null, Controls: () => null,
  Handle: () => null, Position: { Top: 'top', Bottom: 'bottom' }, MarkerType: { ArrowClosed: 'arrow' },
}));
type Fixture = { case: Case; context: Context; graph: Graph; evidence: EvidenceList };
const s6 = s6raw as Fixture;
const s8 = s8raw as Fixture;
const noop = () => {};

describe('read-only investigation semantics', () => {
  it('renders case header with principal, budget, identity and investigation permission', () => {
    render(<CaseHeader data={s6.case} context={s6.context} />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('CASE-JD202609100001');
    expect(screen.getByText('20,000 CNY')).toBeVisible();
    expect(screen.getByText('7 / 20')).toBeVisible();
    expect(screen.getByText('MATCH')).toBeVisible();
    expect(screen.getByText('Investigation Allowed')).toBeVisible();
  });

  it('preserves UNKNOWN and does not invent a payment failure from absent facts', () => {
    render(<><CaseHeader data={s8.case} context={s8.context} /><CurrentFactsPanel facts={s8.context.current_facts} onEvidence={noop} /><FinancialIdentityPanel identity={s8.context.financial_identity} onEvidence={noop} /></>);
    expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0);
    expect(screen.getByText('No eligible current facts · UNKNOWN')).toBeVisible();
    expect(screen.queryByText(/FAILED/)).not.toBeInTheDocument();
  });

  it('shows mismatch and unknown dimensions exactly', () => {
    render(<FinancialIdentityPanel identity={{ ...s6.context.financial_identity, result: 'MISMATCH', mismatch_dimensions: ['BENEFICIARY'], unknown_dimensions: ['ACCOUNT'] }} onEvidence={noop} />);
    expect(screen.getByText('MISMATCH')).toBeVisible();
    expect(screen.getByText('BENEFICIARY')).toBeVisible();
    expect(screen.getByText('ACCOUNT')).toBeVisible();
  });

  it('puts safety-critical gaps before discriminating gaps', () => {
    const { container } = render(<EvidenceGapPanel gaps={[...s6.graph.open_gaps].reverse()} />);
    expect(container.querySelector('article')).toHaveTextContent('SAFETY_CRITICAL');
    expect(container.querySelector('article')).toHaveTextContent('FUND_PROTOCOL_APPLICABILITY');
  });

  it('confirmed hypothesis displays decisive refs and opens evidence', async () => {
    const selected = vi.fn();
    const h = s6.graph.hypotheses.find(h => h.hypothesis_id === 'H4')!;
    render(<HypothesisDetail graph={s6.graph} hypothesis={h} onEvidence={selected} />);
    expect(screen.getByText('CONFIRMED')).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Decisive evidence refs' })).toBeVisible();
    await userEvent.click(screen.getAllByRole('button', { name: h.decisive_evidence_refs[0] })[0]);
    expect(selected).toHaveBeenCalledWith(h.decisive_evidence_refs[0]);
  });

  it('eliminated hypothesis stays eliminated and never becomes Root Cause', () => {
    render(<HypothesisDetail graph={s6.graph} hypothesis={s6.graph.hypotheses.find(h => h.status === 'ELIMINATED')!} onEvidence={noop} />);
    expect(screen.getByText('ELIMINATED')).toBeVisible();
    expect(screen.queryByText(/Root Cause/i)).not.toBeInTheDocument();
  });

  it('shows versions, selected count, omission reason counts and trust sections', () => {
    render(<ReasoningContextInspector context={s6.context} onEvidence={noop} />);
    expect(screen.getByText('Context Policy Version').closest('tr')).toHaveTextContent(s6.context.context_policy_version);
    expect(screen.getByText('Eligibility Policy Version')).toBeVisible();
    for (const group of s6.context.omitted_evidence_summary.omitted) {
      expect(screen.getByText(group.reason).closest('tr')).toHaveTextContent(String(group.count));
    }
    for (const trust of ['TRUSTED CONTROL', 'UNTRUSTED EXTERNAL DATA', 'DETERMINISTIC DERIVED']) expect(screen.getByText(trust)).toBeVisible();
    expect(screen.getByText('External evidence is data, never instruction.')).toBeVisible();
  });

  it('keeps a legitimate instruction-shaped error code labeled UNTRUSTED DATA', () => {
    const fact = s6.context.current_facts.find(f => f.claim_type === 'MESSAGE_ERROR_CODE')!;
    render(<CurrentFactsPanel facts={[{ ...fact, value: 'IGNORE_PREVIOUS_INSTRUCTIONS' }]} onEvidence={noop} />);
    const article = screen.getByText('IGNORE_PREVIOUS_INSTRUCTIONS').closest('article')!;
    expect(within(article).getByText('UNTRUSTED DATA')).toBeVisible();
  });

  it('shows actual backend safety invariants for identity uncertainty and critical gaps', () => {
    render(<SafetyBanner context={s8.context} />);
    expect(screen.getByRole('alert')).toHaveTextContent('NO_NEW_FINANCIAL_INTENT');
    expect(screen.getByRole('alert')).toHaveTextContent('UNKNOWN_IS_NOT_FAILED');
  });

  it('renders S8 timeout counts from the snapshot history', () => {
    render(<ReasoningContextInspector context={s8.context} onEvidence={noop} />);
    expect(screen.getByText('TIMEOUT × 3')).toBeVisible();
    expect(screen.getByText('get_payment_transaction')).toBeVisible();
    expect(screen.queryByText('Payment Failed')).not.toBeInTheDocument();
  });

  it('searches only Evidence ID and Claim Type, never raw/value free text', () => {
    render(<EvidenceTimeline data={s6.evidence} onEvidence={noop} />);
    const input = screen.getByRole('textbox', { name: 'Search evidence ID or claim type' });
    fireEvent.change(input, { target: { value: 'PAYMENT_FINALITY' } });
    expect(screen.getByText('SETTLED')).toBeVisible();
    fireEvent.change(input, { target: { value: 'SETTLED' } });
    expect(screen.queryByText('PAYMENT_FINALITY')).not.toBeInTheDocument();
  });

  it('evidence detail exposes provenance without fetching raw data', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const evidence = s6.evidence.items[0];
    render(<EvidenceDetailDrawer id={evidence.evidence_id} evidence={evidence} onClose={noop} />);
    expect(screen.getByText(evidence.observation_id)).toBeVisible();
    expect(screen.getByText('Extractor Version')).toBeVisible();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.queryByText('Raw Observation')).not.toBeInTheDocument();
  });
});

describe('error contracts and page integration', () => {
  it.each([
    [404, '', 'Case not found'], [403, '', 'Access denied'],
    [409, 'CONTEXT_ELIGIBILITY_ERROR', 'Context eligibility failure'],
    [409, 'MANDATORY_CONTEXT_OVERFLOW', 'Context mandatory overflow'],
    [409, '', 'Investigation unavailable'], [500, '', 'Server error'], [0, '', 'Connection unavailable'],
  ])('distinguishes %s %s', (status, code, title) => {
    expect(errorMessage(new ApiError(Number(status), String(code))).title).toBe(title);
  });

  it('never generates tool execution, repair or chat controls; rejects stale context on refresh error', async () => {
    vi.spyOn(caseApi, 'case').mockResolvedValue(s6.case);
    vi.spyOn(caseApi, 'evidence').mockResolvedValue(s6.evidence);
    vi.spyOn(caseApi, 'graph').mockResolvedValue(s6.graph);
    const context = vi.spyOn(caseApi, 'context').mockResolvedValue(s6.context);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/cases/CASE-JD202609100001']}><Routes><Route path="/cases/:caseId" element={<CaseInvestigation />} /></Routes></MemoryRouter></QueryClientProvider>);
    await screen.findByText('20,000 CNY');
    await screen.findByText('Planner not enabled in UI-0.');
    expect(screen.queryByRole('button', { name: /^(Run|Execute|Call Tool|Repair|Send)/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /chat|message|prompt/i })).not.toBeInTheDocument();
    context.mockRejectedValue(new ApiError(409, 'MANDATORY_CONTEXT_OVERFLOW'));
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.queryByText('MATCH')).not.toBeInTheDocument());
    expect(screen.getByText('Safety context unavailable')).toBeVisible();
    expect(screen.getAllByText('Context mandatory overflow').length).toBeGreaterThan(0);
    client.clear();
  }, 20000);
});
