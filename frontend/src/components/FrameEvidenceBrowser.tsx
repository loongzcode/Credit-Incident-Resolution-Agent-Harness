import { useState } from 'react';
import { Button } from 'antd';
import type { InvestigationFrame } from '../types';
import { EvidenceTimeline } from './EvidenceTimeline';
import { ErrorPanel } from './common';
import { caseApi } from '../api/caseApi';

export function FrameEvidenceBrowser({frame, onEvidence}:{frame:InvestigationFrame;onEvidence:(ref:string)=>void}) {
  const [page,setPage] = useState(frame.evidence);
  const [error,setError] = useState<Error>();
  const [loading,setLoading] = useState(false);
  async function more() {
    if (!page.next_cursor) return;
    setLoading(true);
    try {
      const result=await caseApi.evidencePage(frame.case_id,frame.frame_id,page.next_cursor);
      if(result.frame_id!==frame.frame_id) throw new Error('Frame binding mismatch');
      setPage(result);
    } catch(e) {setError(e as Error);} finally {setLoading(false);}
  }
  return <>{error && <ErrorPanel error={error}/>}<p>筛选条件仅作用于当前服务端分页。</p><EvidenceTimeline data={page} onEvidence={onEvidence}/>{page.next_cursor && <Button loading={loading} onClick={()=>void more()}>下一页证据</Button>}</>;
}
