import { createContext, useContext } from 'react';
import type { Dashboard } from './types';
export class AdminApiError extends Error { constructor(public status: number, public code: string) { super(code); } }
export type AdminRequest = <T>(path: string, method?: string, body?: unknown) => Promise<T>;
export function transport(getToken: () => Promise<string>): AdminRequest {
  return async <T,>(path: string, method = 'GET', body?: unknown): Promise<T> => {
    const headers: Record<string, string> = { Authorization: `Bearer ${await getToken()}`, 'X-Registry-Request': '1' };
    const multipart = body instanceof FormData;
    if (body && !multipart) headers['Content-Type'] = 'application/json';
    const response = await fetch(`/admin-api/registry${path}`, { method, headers, credentials: 'omit',
      body: body ? (multipart ? body : JSON.stringify(body)) : undefined });
    const data = await response.json();
    if (!response.ok) throw new AdminApiError(response.status, typeof data.detail === 'string' ? data.detail : 'INVALID_ADMIN_REQUEST');
    return data as T;
  };
}
export const AdminContext = createContext<{ api: AdminRequest; dashboard: Dashboard } | null>(null);
export function useAdmin() { const value = useContext(AdminContext); if (!value) throw new Error('Admin session required'); return value; }
export function errorText(error: unknown) {
  if (error instanceof AdminApiError) {
    if (error.status === 403) return `无权执行此操作，请联系管理员。${error.code}`;
    if (error.status === 409) return `版本或状态已变化，请刷新并重新审核。${error.code}`;
    if (error.status === 401) return '身份已失效，请重新连接公司 SSO。';
    return error.code;
  }
  return '请求失败，请稍后重试。';
}
