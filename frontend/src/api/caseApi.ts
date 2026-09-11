import type { Case, Context, EvidenceList, Graph } from '../types';

export class ApiError extends Error {
  constructor(public status: number, public code: string) { super(code); }
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try { response = await fetch(path, { method: 'GET', signal, credentials: 'same-origin' }); }
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
  case: (id: string, signal?: AbortSignal) => read<Case>(path(id), signal),
  evidence: (id: string, signal?: AbortSignal) => read<EvidenceList>(`${path(id)}/evidence`, signal),
  graph: (id: string, signal?: AbortSignal) => read<Graph>(`${path(id)}/hypotheses`, signal),
  context: (id: string, signal?: AbortSignal) => read<Context>(`${path(id)}/reasoning-context`, signal),
};

export function errorMessage(error: Error): { title: string; detail: string } {
  if (error instanceof ApiError) {
    if (error.status === 404) return { title: 'Case not found', detail: 'This case is unavailable within your access scope. Check the Case ID.' };
    if (error.status === 403 || error.status === 401) return { title: 'Access denied', detail: 'A valid read-only console session is required.' };
    if (error.code === 'MANDATORY_CONTEXT_OVERFLOW') return { title: 'Context mandatory overflow', detail: 'Reasoning Context exceeds safe mandatory budget. Investigation should escalate.' };
    if (error.code === 'CONTEXT_ELIGIBILITY_ERROR') return { title: 'Context eligibility failure', detail: 'Reasoning Context unavailable because mandatory information is not eligible.' };
    if (error.status === 409) return { title: 'Investigation unavailable', detail: 'The current case does not satisfy the required access or eligibility policy.' };
    if (error.status === 0) return { title: 'Connection unavailable', detail: 'The read-only API could not be reached. Check the backend and refresh.' };
  }
  return { title: 'Server error', detail: 'The API could not provide a valid investigation view. Refresh to retry.' };
}
