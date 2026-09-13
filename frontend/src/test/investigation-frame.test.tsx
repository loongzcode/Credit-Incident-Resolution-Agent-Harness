import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TracePanel } from '../components/TracePanel';
import type { InvestigationFrame, TracePage } from '../types';
import fixture from './fixtures/s6.json';

const frame = fixture.frame as InvestigationFrame;
const noop = () => {};
function trace(status: string, warning: string, historical = false, fields: {name:string;value:string}[] = []): TracePage {
  return {frame_id:frame.frame_id,total:1,next_cursor:null,items:[{trace_id:'TRACE-TEST',kind:'Evaluation',status,
    occurred_at:frame.assembled_at,fields,evidence_refs:[],related_refs:[],warning,historical}]};
}

describe('production trace semantics', () => {
  it('keeps historical guidance visibly separate from current evidence', () => {
    render(<TracePanel frame={frame} section="knowledge" page={trace('RECORDED','HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE',true)} onEvidence={noop} />);
    expect(screen.getByText('历史资料 · 不属于当前证据')).toBeVisible();
  });
  it('shows retrieval degradation and latency without a vector viewer', () => {
    render(<TracePanel frame={frame} section="knowledge" page={trace('RECORDED','Not current Evidence',true,[{name:'degradation',value:'VECTOR_UNAVAILABLE'},{name:'latency_ms',value:'12'}])} onEvidence={noop} />);
    expect(screen.getByText('向量检索不可用')).toBeVisible();
    expect(screen.queryByRole('button', {name:/embedding/i})).not.toBeInTheDocument();
  });
  it('shows protocol route revision and source proof', () => {
    render(<TracePanel frame={frame} section="routes" page={trace('VERIFIED','Protocol verified',false,[{name:'protocol_version',value:'2.3'},{name:'change_reason',value:'PROTOCOL_VERIFIED'}])} onEvidence={noop} />);
    expect(screen.getByText('2.3')).toBeVisible();
    expect(screen.getByText('协议已验证')).toBeVisible();
  });
  it('shows UNKNOWN side effect without retry control', () => {
    render(<TracePanel frame={frame} section="effects" page={trace('UNKNOWN','结果未知，不允许盲目重试')} onEvidence={noop} />);
    expect(screen.getByText('结果未知，不允许盲目重试')).toBeVisible();
    expect(screen.queryByRole('button',{name:/retry/i})).not.toBeInTheDocument();
  });
  it('APPLIED does not present VERIFIED', () => {
    render(<TracePanel frame={frame} section="effects" page={trace('APPLIED','APPLIED ≠ VERIFIED')} onEvidence={noop} />);
    expect(screen.getByText('已应用 ≠ 已验证')).toBeVisible();
    expect(screen.queryByText('已验证结案')).not.toBeInTheDocument();
  });
  it('displays evaluator INCONCLUSIVE and missing requirements', () => {
    render(<TracePanel frame={frame} section="evaluations" page={trace('INCONCLUSIVE','Current evaluation',false,[{name:'requirements',value:'POST_EFFECT_MESSAGE_STATUS'}])} onEvidence={noop} />);
    expect(screen.getByText('尚无定论')).toBeVisible();
    expect(screen.getByText('执行后的消息状态')).toBeVisible();
  });
  it('attributes CLOSED_VERIFIED to evaluator and CAS, never agent', () => {
    render(<TracePanel frame={frame} section="closure" page={trace('CLOSED_VERIFIED','IndependentEvaluator PASS + VerifiedClosure CAS')} onEvidence={noop} />);
    expect(screen.getByText('已验证结案')).toBeVisible();
    expect(screen.getByText('独立评估通过，并完成结案版本校验')).toBeVisible();
  });
});
