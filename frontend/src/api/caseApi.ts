import type { Evidence, InvestigationFrame, TracePage, EvidencePage } from '../types';

declare global { interface Window { investigationIdentity?: { getAccessToken: () => Promise<string> } } }

export class ApiError extends Error {
  constructor(public status: number, public code: string) { super(code); }
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    const token = await window.investigationIdentity?.getAccessToken();
    response = await fetch(path, { method: 'GET', signal, credentials: 'same-origin',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined });
  }
  catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'NETWORK_UNAVAILABLE');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body?.detail?.code || 'HTTP_ERROR');
  }
  try { return await response.json() as T; }
  catch { throw new ApiError(502, 'INVALID_RESPONSE'); }
}

const path = (caseId: string) => `/ui/cases/${encodeURIComponent(caseId)}`;
export const caseApi = {
  frame: (id: string, signal?: AbortSignal) => read<InvestigationFrame>(`${path(id)}/frame`, signal),
  evidenceDetail: (id: string, frameId: string, evidenceId: string, signal?: AbortSignal) => read<Evidence>(`${path(id)}/evidence-items/${encodeURIComponent(evidenceId)}?frame_id=${encodeURIComponent(frameId)}`, signal),
  evidencePage: (id: string, frameId: string, cursor: string) => read<EvidencePage>(`${path(id)}/evidence-page?frame_id=${encodeURIComponent(frameId)}&cursor=${encodeURIComponent(cursor)}`),
  trace: (id: string, frameId: string, section: string, cursor: string, signal?: AbortSignal) => read<TracePage>(`${path(id)}/${section}?frame_id=${encodeURIComponent(frameId)}&cursor=${encodeURIComponent(cursor)}`, signal),
};

export function errorMessage(error: Error): { title: string; detail: string } {
  if (error instanceof ApiError) {
    if (error.status === 404) return { title: '未找到调查任务', detail: '当前访问范围内无法找到此任务，请检查任务编号。' };
    if (error.status === 403 || error.status === 401) return { title: '无权访问', detail: '请使用有效的只读控制台会话。' };
    if (error.code === 'MANDATORY_CONTEXT_OVERFLOW') return { title: '必要上下文超出预算', detail: '必要推理上下文超出安全预算，应升级至人工调查。' };
    if (error.code === 'CONTEXT_ELIGIBILITY_ERROR') return { title: '上下文可用性校验失败', detail: '必要信息不符合可用性要求，无法提供推理上下文。' };
    if (error.status === 409) return { title: '调查不可用', detail: '当前任务不满足访问权限或信息可用性策略。' };
    if (error.status === 0) return { title: '连接不可用', detail: '无法连接只读接口，请检查后端服务并刷新。' };
  }
  return { title: '服务异常', detail: '接口未能提供有效的调查视图，请刷新重试。' };
}
