import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ConfigProvider, Tag } from 'antd';
import { CaseInvestigation, CaseLookup } from './pages/CaseInvestigation';
import './styles.css';
import { RegistryConsole } from './admin/RegistryConsole';

function AppHeader() {
  const admin = useLocation().pathname.startsWith('/admin/registry');
  return <header className="app-header"><div className="brand-mark">{admin ? 'SR' : 'IC'}</div><strong>{admin ? 'System Registry Administration' : 'Incident Investigation Console'}</strong><span className="header-divider" /><span>{admin ? 'Platform Operations' : 'Credit Operations'}</span><Tag>{admin ? 'SEPARATE ADMIN PERMISSIONS' : 'UI-0 · READ ONLY'}</Tag></header>;
}

const client = new QueryClient();
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider theme={{ token: {
  colorPrimary: '#31596d', borderRadius: 5, fontSize: 13, colorBgLayout: '#f2f4f6', colorText: '#20313d',
  fontFamily: "Inter, 'Segoe UI', 'Microsoft YaHei', sans-serif",
} }}><QueryClientProvider client={client}><BrowserRouter>
  <AppHeader />
  <Routes><Route path="/admin/registry/*" element={<RegistryConsole />} /><Route path="/cases/:caseId" element={<CaseInvestigation />} /><Route path="/cases" element={<CaseLookup />} /><Route path="*" element={<Navigate to="/cases" replace />} /></Routes>
  <footer>Evidence-bounded investigation · All times UTC</footer>
</BrowserRouter></QueryClientProvider></ConfigProvider></React.StrictMode>);
