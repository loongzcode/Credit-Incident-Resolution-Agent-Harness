import { useState } from 'react';
import { Alert, Button, Card, Checkbox, Input, Select, Space, Tabs, Typography } from 'antd';
import type { Authority, Capability, Catalog, Definition, Scope, SystemDefinition } from './types';
const options = (items: string[]) => items.map(value => ({ label: value, value }));

function ScopeEditor({ value, onChange }: { value: Scope; onChange: (v: Scope) => void }) {
  const set = <K extends keyof Scope>(key: K, v: Scope[K]) => onChange({ ...value, [key]: v });
  return <div className="registry-form-grid">
    <label>环境<Select value={value.environment} options={options(['SIMULATOR','TEST','PROD'])} onChange={v => set('environment', v)} /></label>
    <label>Partner role<Select allowClear value={value.partner_role} options={options(['ASSET','FUNDING','GUARANTEE'])} onChange={v => onChange({ ...value, partner_role: v || null, partner_id: v ? value.partner_id : null })} /></label>
    <label>Partner ID<Input value={value.partner_id || ''} disabled={!value.partner_role} onChange={e => set('partner_id', e.target.value || null)} /></label>
    <label>Products（回车添加）<Select mode="tags" tokenSeparators={[',']} value={value.product_codes} onChange={v => set('product_codes', v)} /></label>
    <label>协议版本（回车添加）<Select mode="tags" tokenSeparators={[',']} disabled={value.version_agnostic} value={value.protocol_versions} onChange={v => set('protocol_versions', v)} /></label>
    <Checkbox checked={value.version_agnostic} onChange={e => onChange({ ...value, version_agnostic: e.target.checked, protocol_versions: e.target.checked ? [] : value.protocol_versions })}>Version agnostic</Checkbox>
  </div>;
}

export function CapabilityEditor({ definition, catalog, onChange }: { definition: Definition; catalog: Catalog; onChange: (v: Definition) => void }) {
  const [selected, setSelected] = useState<string | undefined>(definition.capabilities[0]?.capability_id);
  const cap = definition.capabilities.find(c => c.capability_id === selected);
  function update(value: Capability) { onChange({ ...definition, capabilities: definition.capabilities.map(c => c.capability_id === selected ? value : c) }); }
  const set = <K extends keyof Capability>(key: K, value: Capability[K]) => { if (cap) update({ ...cap, [key]: value }); };
  function claims(values: string[]) {
    if (!cap) return;
    const existing = new Map(definition.authority_rules.filter(r => r.capability_id === selected).map(r => [r.claim_type, r]));
    const rules: Authority[] = values.map(claim => existing.get(claim) || { capability_id: cap.capability_id, claim_type: claim,
      authority_level: 'DIAGNOSTIC', subject_binding_required: true, identity_binding_required: false, freshness_requirement: 'CURRENT', completeness_requirement: 'COMPLETE' });
    onChange({ ...definition, capabilities: definition.capabilities.map(c => c === cap ? { ...c, produces_claim_types: values } : c),
      authority_rules: [...definition.authority_rules.filter(r => r.capability_id !== selected), ...rules] });
  }
  function add() {
    const system = definition.systems[0]; const tool = catalog.tools[0]; if (!system || !tool) return;
    const id = `cap-new-${definition.capabilities.length + 1}`;
    const c: Capability = { capability_id: id, system_id: system.system_id, capability_type: catalog.capability_types[0], tool_name: tool.tool_name,
      read_or_write: 'READ', produces_claim_types: [], contributes_requirements: [], supports_lookup: true, authority_level: 'DIAGNOSTIC',
      scope: system.scope, adapter_id: 'adapter-to-be-registered', query_contract_version: '1', response_contract_version: '1', status: 'DISABLED',
      effective_from: system.effective_from, effective_until: null, draining_since: null,
      recovery: { supports_status_lookup: false, not_found_proves_no_effect: false, resolver_contract_version: '1' } };
    onChange({ ...definition, capabilities: [...definition.capabilities, c] }); setSelected(id);
  }
  const tool = catalog.tools.find(t => t.tool_name === cap?.tool_name);
  return <Card title="Capability Editor" extra={<Button onClick={add}>新增 Capability</Button>}>
    <Select aria-label="选择 Capability" style={{ minWidth: 280 }} value={selected} options={options(definition.capabilities.map(c => c.capability_id))} onChange={setSelected} />
    {cap ? <><div className="registry-form-grid" style={{ marginTop: 16 }}>
      <label>Capability ID<Input value={cap.capability_id} onChange={e=>{ const id=e.target.value; onChange({...definition,
        capabilities:definition.capabilities.map(c=>c===cap?{...c,capability_id:id}:c),
        authority_rules:definition.authority_rules.map(r=>r.capability_id===selected?{...r,capability_id:id}:r)});setSelected(id);}} /></label>
      <label>System<Select value={cap.system_id} options={options(definition.systems.map(s => s.system_id))} onChange={v => set('system_id', v)} /></label>
      <label>Capability type<Select value={cap.capability_type} options={options(catalog.capability_types)} onChange={v => set('capability_type', v)} /></label>
      <label>Tool<Select value={cap.tool_name} options={options(catalog.tools.map(t => t.tool_name))} onChange={v => {
        update({ ...cap, tool_name: v, produces_claim_types: [], contributes_requirements: [] });
        onChange({ ...definition, capabilities: definition.capabilities.map(c => c === cap ? { ...c, tool_name: v, produces_claim_types: [], contributes_requirements: [] } : c), authority_rules: definition.authority_rules.filter(r => r.capability_id !== selected) });
      }} /></label>
      <label>Access<Select value={cap.read_or_write} options={options(['READ','WRITE'])} onChange={v => set('read_or_write', v)} /></label>
      <label>Adapter reference<Input value={cap.adapter_id} onChange={e => set('adapter_id', e.target.value)} /></label>
      <label>Status<Select value={cap.status} options={options(['ACTIVE','DRAINING','DISABLED'])} onChange={v => set('status', v)} /></label>
      <label>Query contract<Input value={cap.query_contract_version} onChange={e => set('query_contract_version', e.target.value)} /></label>
      <label>Response contract<Input value={cap.response_contract_version} onChange={e => set('response_contract_version', e.target.value)} /></label>
      <label>Effective from<Input value={cap.effective_from} onChange={e => set('effective_from', e.target.value)} /></label>
      <label>Effective until<Input value={cap.effective_until || ''} onChange={e => set('effective_until', e.target.value || null)} /></label>
      <label>Draining since<Input value={cap.draining_since || ''} onChange={e => set('draining_since', e.target.value || null)} /></label>
      <label>Claims<Select mode="multiple" value={cap.produces_claim_types} options={options(tool?.claims || [])} onChange={claims} /></label>
      <label>Requirements<Select mode="multiple" value={cap.contributes_requirements} options={options(tool?.requirements || [])} onChange={v => set('contributes_requirements', v)} /></label>
    </div><Typography.Title level={5}>Routing Scope</Typography.Title><ScopeEditor value={cap.scope} onChange={v => set('scope', v)} />
    <Typography.Title level={5}>Claim Authority（独立审核）</Typography.Title>
    {definition.authority_rules.filter(r => r.capability_id === selected).map(rule => <Space key={rule.claim_type} wrap style={{ display: 'flex', margin: '10px 0' }}>
      <span>{rule.claim_type}</span><Select style={{ width: 170 }} value={rule.authority_level} options={options(catalog.authority_levels)} onChange={value => onChange({ ...definition, authority_rules: definition.authority_rules.map(r => r === rule ? { ...r, authority_level: value } : r) })} />
      {(['subject_binding_required','identity_binding_required'] as const).map(field => <Checkbox key={field} checked={rule[field]} onChange={e => onChange({ ...definition, authority_rules: definition.authority_rules.map(r => r === rule ? { ...r, [field]: e.target.checked } : r) })}>{field}</Checkbox>)}
    </Space>)}
    <Button danger onClick={() => { onChange({ ...definition, capabilities: definition.capabilities.filter(c => c !== cap), authority_rules: definition.authority_rules.filter(r => r.capability_id !== selected) }); setSelected(undefined); }}>移除该 Capability</Button>
    </> : <Typography.Paragraph>选择或新增一项能力；新增能力默认 DISABLED，必须配置真实 Tool contract。</Typography.Paragraph>}
  </Card>;
}

function SystemEditor({ definition, catalog, onChange }: { definition: Definition; catalog: Catalog; onChange: (v: Definition) => void }) {
  const [index, setIndex] = useState(0); const system = definition.systems[index];
  const update = <K extends keyof SystemDefinition>(key: K, value: SystemDefinition[K]) => onChange({ ...definition,
    systems: definition.systems.map((s, i) => i === index ? { ...s, [key]: value } : s) });
  return <Card title="系统配置" extra={<Button onClick={() => { const id = `system-new-${definition.systems.length + 1}`;
    const value: SystemDefinition = { system_id: id, display_name: '新系统', system_type: catalog.system_types[0], source_channel: 'INTERNAL_SYSTEM', owner_team: 'platform', status: 'DISABLED',
      effective_from: new Date().toISOString(), effective_until: null, draining_since: null, credential_ref: null,
      scope: { tenant_id: definition.tenant_id, business_domain: 'PERSONAL_CREDIT', environment: 'SIMULATOR', partner_role: null, partner_id: null, product_codes: ['CONSUMER_LOAN'], protocol_versions: [], version_agnostic: true } };
    onChange({ ...definition, systems: [...definition.systems, value] }); setIndex(definition.systems.length);
  }}>新增系统</Button>}>
    <Select aria-label="选择系统" style={{ minWidth: 280 }} value={index} options={definition.systems.map((s, i) => ({ label: s.system_id, value: i }))} onChange={setIndex} />
    {system && <><div className="registry-form-grid" style={{ marginTop: 16 }}>
      <label>System ID<Input value={system.system_id} disabled={!!system.credential_ref} onChange={e=>{const id=e.target.value;onChange({...definition,
        systems:definition.systems.map(s=>s===system?{...s,system_id:id}:s),capabilities:definition.capabilities.map(c=>c.system_id===system.system_id?{...c,system_id:id}:c)});}} /></label>
      <label>系统名称<Input aria-label="系统名称" value={system.display_name} onChange={e => update('display_name', e.target.value)} /></label>
      <label>公司系统简称（显式绑定）<Input aria-label="公司系统简称绑定" value={system.company_system_code||''} maxLength={100} placeholder="留空表示未绑定；须填写已激活主档简称" onChange={e=>update('company_system_code',e.target.value||null)}/></label>
      <label>System type<Select value={system.system_type} options={options(catalog.system_types)} onChange={v => update('system_type', v)} /></label>
      <label>Source channel<Select value={system.source_channel} options={options(catalog.source_channels)} onChange={v => update('source_channel', v)} /></label>
      <label>Owner team<Input value={system.owner_team} onChange={e => update('owner_team', e.target.value)} /></label>
      <label>Status<Select value={system.status} options={options(['ACTIVE','DRAINING','DISABLED'])} onChange={v => update('status', v)} /></label>
      <label>Effective from<Input value={system.effective_from} onChange={e => update('effective_from', e.target.value)} /></label>
      <label>Effective until<Input value={system.effective_until || ''} onChange={e => update('effective_until', e.target.value || null)} /></label>
      <label>Draining since<Input value={system.draining_since || ''} onChange={e => update('draining_since', e.target.value || null)} /></label>
      <label>Credential reference（服务器管理）<Input disabled value={system.credential_ref || '未绑定'} /></label>
    </div><Typography.Title level={5}>Routing Scope</Typography.Title><ScopeEditor value={system.scope} onChange={v => update('scope', v)} />
    <Button danger disabled={definition.capabilities.some(c => c.system_id === system.system_id)} onClick={() => { onChange({ ...definition, systems: definition.systems.filter(s => s !== system) }); setIndex(0); }}>移除系统（须先移除关联能力）</Button></>}
  </Card>;
}

export function DraftEditor({ initial, catalog, onSave, loading }: { initial: Definition; catalog: Catalog; onSave: (d: Definition) => void; loading: boolean }) {
  const [definition, setDefinition] = useState(initial);
  return <><Alert type="warning" title="仅编辑 Draft。输入不会直接修改 ACTIVE 版本；新增 adapter 仍需服务器可信绑定。" />
    <Tabs items={[{ key: 'systems', label: '系统配置', children: <SystemEditor definition={definition} catalog={catalog} onChange={setDefinition} /> },
      { key: 'capabilities', label: 'Capability Editor', children: <CapabilityEditor definition={definition} catalog={catalog} onChange={setDefinition} /> }]} />
    <Button type="primary" loading={loading} onClick={() => onSave(definition)}>保存 Draft</Button></>;
}
