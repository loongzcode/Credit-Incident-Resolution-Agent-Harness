import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Input, Layout, Menu, Space, Spin, Tag, Typography } from 'antd';
import { Link, Route, Routes, useLocation } from 'react-router-dom';
import { AdminContext, errorText, transport } from './api';
import type { Dashboard } from './types';
import { DashboardPage, Systems, SystemDetail, Changes, ChangeDetail, Versions, DiffPage, ImpactPage, AuditLog, ImportSystems } from './pages';
import './registry.css';

declare global { interface Window { registryIdentity?: { getAccessToken: () => Promise<string> } } }

export function RegistryConsole({ getToken }: { getToken?: () => Promise<string> }) {
  const [localToken, setLocalToken] = useState('');
  const [connection, setConnection] = useState<(() => Promise<string>) | null>(() => getToken || null);
  const api = useMemo(() => transport(connection || (async () => { throw new Error('SSO required'); })), [connection]);
  const session = useQuery({ queryKey: ['registry-session', connection], queryFn: () => api<Dashboard>(''), enabled: !!connection, retry: false });
  const location = useLocation();
  if (!connection || !session.data || session.error) return <main className="registry-login"><Card title="Registry Administration · 管理身份验证">
    <Typography.Paragraph>系统清单管理使用独立的公司身份与应用权限。调查 Console 的凭据不能用于此后台。</Typography.Paragraph>
    {session.error && <Alert type="error" title={errorText(session.error)} />}
    {session.isFetching && <Spin />}
    <Button type="primary" disabled={!window.registryIdentity} onClick={() => { setConnection(() => window.registryIdentity!.getAccessToken); void session.refetch(); }}>连接公司 SSO</Button>
    {!window.registryIdentity && <Typography.Paragraph type="secondary">部署时由企业 SSO 客户端注入 Access Token Provider。</Typography.Paragraph>}
    {import.meta.env.DEV && import.meta.env.VITE_REGISTRY_LOCAL === '1' && <Space orientation="vertical">
      <Tag color="orange">仅本地 synthetic 演示</Tag><Input.Password aria-label="本地演示令牌" value={localToken} onChange={e => setLocalToken(e.target.value)} autoComplete="off" />
      <Button onClick={() => setConnection(() => async () => localToken)}>使用本地令牌连接</Button>
    </Space>}
  </Card></main>;
  const dashboard = session.data;
  if (!dashboard.permissions.includes('REGISTRY_VIEW')) return <Alert type="error" title="无 Registry 查看权限" />;
  const links = [['', '概览'], ['/systems', '系统清单'], ['/change-requests', '变更请求'], ['/versions', '版本历史'], ['/approvals', '审批队列'], ['/import', 'Excel 导入'], ['/audit', '审计日志']].filter(([path]) =>
    path !== '/audit' || dashboard.permissions.includes('REGISTRY_AUDIT')).filter(([path]) => path !== '/import' || dashboard.permissions.includes('REGISTRY_EDIT'));
  return <AdminContext.Provider value={{ api, dashboard }}><Layout className="registry-layout">
    <Layout.Sider theme="light" width={210}><div className="registry-title">SYSTEM REGISTRY<br /><small>平台配置管理</small></div>
      <Menu selectedKeys={[location.pathname]} items={links.map(([path, title]) => ({ key: '/admin/registry' + path, label: <Link to={'/admin/registry' + path}>{title}</Link> }))} />
      <div className="registry-identity">{dashboard.identity.display_name}<br />{dashboard.roles.map(r => <Tag key={r}>{r.replace('REGISTRY_', '')}</Tag>)}</div>
    </Layout.Sider>
    <Layout.Content className="registry-content"><Alert type="info" showIcon title="配置经 Draft → Submit → 独立审批 → Activate 后生效。Registry capability 不等于业务执行授权。" />
      <Routes><Route index element={<DashboardPage />} /><Route path="systems" element={<Systems />} /><Route path="systems/:id" element={<SystemDetail />} />
        <Route path="change-requests" element={<Changes />} /><Route path="change-requests/:id" element={<ChangeDetail />} /><Route path="change-requests/:id/impact" element={<ImpactPage />} />
        <Route path="versions" element={<Versions />} /><Route path="versions/:a/diff/:b" element={<DiffPage />} /><Route path="approvals" element={<Changes approvals />} />
        <Route path="audit" element={<AuditLog />} /><Route path="import" element={<ImportSystems />} /><Route path="*" element={<Alert type="warning" title="管理页面不存在" />} /></Routes>
    </Layout.Content></Layout></AdminContext.Provider>;
}
