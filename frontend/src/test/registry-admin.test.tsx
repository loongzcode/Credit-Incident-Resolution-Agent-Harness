import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AdminApiError, AdminContext, type AdminRequest, transport } from '../admin/api';
import { CapabilityEditor } from '../admin/editor';
import { ChangeDetail, Changes, DiffPage, ImpactPage, ImportSystems, SystemDetail, Systems } from '../admin/pages';
import type { Catalog, Dashboard, Definition, Detail, Impact, Permission, VersionDiff } from '../admin/types';

const scope = { tenant_id:'demo',business_domain:'PERSONAL_CREDIT',environment:'SIMULATOR',partner_role:null,partner_id:null,product_codes:['CONSUMER_LOAN'],protocol_versions:[],version_agnostic:true };
const definition:Definition = {tenant_id:'demo', systems:[{system_id:'sim-fund',display_name:'我司资金接入系统',system_type:'FUNDING_INTEGRATION',source_channel:'PARTNER_OFFICIAL_API',owner_team:'platform',status:'ACTIVE',scope,effective_from:'2026-01-01T00:00:00Z',effective_until:null,draining_since:null,credential_ref:'[MASKED]'}],capabilities:[{capability_id:'cap-fund',system_id:'sim-fund',capability_type:'READ_FUND_STATE',tool_name:'get_fund_order',read_or_write:'READ',produces_claim_types:['FUND_BUSINESS_STATUS'],contributes_requirements:[],supports_lookup:true,authority_level:'AUTHORITATIVE',scope,adapter_id:'synthetic-fund',query_contract_version:'1',response_contract_version:'1',status:'ACTIVE',effective_from:'2026-01-01T00:00:00Z',effective_until:null,draining_since:null,recovery:{supports_status_lookup:false,not_found_proves_no_effect:false,resolver_contract_version:'1'}}],authority_rules:[{capability_id:'cap-fund',claim_type:'FUND_BUSINESS_STATUS',authority_level:'AUTHORITATIVE',subject_binding_required:true,identity_binding_required:false,freshness_requirement:'CURRENT',completeness_requirement:'COMPLETE'}]};
const diff:VersionDiff={systems_added:[],systems_removed:[],systems_changed:[{entity_id:'sim-fund',fields:['display_name'],before_values:{display_name:'旧名称'},after_values:{display_name:'新名称'}}],capabilities_added:[],capabilities_removed:[],capabilities_changed:[],authority_added:[],authority_removed:[],authority_changed:[],routing_scope_changed:[],status_changed:[],risk_level:'HIGH_RISK',risk_reasons:['PAYMENT_FINALITY_SOURCE_CHANGE']};
const detail:Detail={change_request:{change_request_id:'CR-1',tenant_id:'demo',base_registry_version:'a'.repeat(64),proposed_registry_version:'b'.repeat(64),revision:3,kind:'CONFIGURATION',status:'SUBMITTED',title:'支付来源变更',reason:'synthetic test',created_by:'editor',created_at:'2026-01-01T00:00:00Z',submitted_by:'editor',approved_by:null,activated_by:null},definition,diff};
const impact:Impact={affected_active_case_count:2,affected_case_ids_digest:'digest-only',affected_partner_products:[],changed_capability_types:['READ_FUND_STATE'],invalidated_snapshot_count:3,affected_pending_work_count:1,case_page:{items:['CASE-1'],total:2},runtime_revalidation_scope:'ALL_ACTIVE_REGISTERED_CASES',base_is_current:true};
const catalog:Catalog={system_types:['FUNDING_INTEGRATION'],source_channels:['PARTNER_OFFICIAL_API'],capability_types:['READ_FUND_STATE'],partner_roles:['FUNDING'],authority_levels:['AUTHORITATIVE','DIAGNOSTIC'],tools:[{tool_name:'get_fund_order',claims:['FUND_BUSINESS_STATUS'],requirements:[]}]};
const company={system_code:'sim-fund',system_name:'Synthetic 公司系统',system_type_label:'测试基础类型',description:'Synthetic original summary',main_functions:'Synthetic original functions',memberships:['九里云系统','融担系统'].map(inventory_group=>({system_code:'sim-fund',inventory_group,source_metadata:{source_file_name:'synthetic.xlsx',source_row_ref:inventory_group+'!2',source_file_hash:'c'.repeat(64),imported_by:'editor',imported_at:'2026-01-01T00:00:00Z'}}))};
const companySummary={system_code:company.system_code,system_name:company.system_name,system_type_label:company.system_type_label,inventory_groups:['九里云系统','融担系统'],agent_configuration_status:'CONFIGURED',agent_source_count:1,capability_count:1};
const companyDetail={company_system:company,...companySummary,registry_sources:definition.systems,capabilities:definition.capabilities,authority:definition.authority_rules};
const importData={preview_id:'IMP-1',source_rows:66,unique_systems:64,shared_systems:['aut','sso'],groups:['九里云系统','融担系统'],added:['PAY'],changed:[],unchanged:[],invalid:[],conflicts:[],rows:[company]};
function setup(element:React.ReactNode,path='/',permissions:Permission[]=['REGISTRY_VIEW'],override?:(path:string,method?:string)=>unknown) {
  const request=vi.fn(async(path:string,method?:string)=>{
    const custom=override?.(path,method); if(custom!==undefined)return custom;
    if(path.includes('/impact'))return impact;
    if(path.includes('/diff/'))return diff;
    if(path==='/catalog')return catalog;
    if(path==='/inventory')return {items:[],total:0};
    if(path==='/systems/sim-fund')return companyDetail;
    if(path.startsWith('/systems?'))return {items:[companySummary],total:1};
    if(path.startsWith('/change-requests?'))return {items:[detail.change_request],total:1};
    return detail;
  });
  const dashboard:Dashboard={active_version:'a'.repeat(64),inventory_revision:0,system_count:1,permissions,roles:[],identity:{user_id:'approver',display_name:'Test Approver'}};
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
  render(<QueryClientProvider client={client}><AdminContext.Provider value={{api:request as AdminRequest,dashboard}}><MemoryRouter initialEntries={[path]}><Routes><Route path={path.includes('CR-1')?'/change-requests/:id':path.includes('/diff/')?'/versions/:a/diff/:b':path.includes('sim-fund')?'/systems/:id':path} element={element}/></Routes></MemoryRouter></AdminContext.Provider></QueryClientProvider>);
  return request;
}

describe('Registry Administration',()=>{
  it('system list shows legitimate integration meaning',async()=>{setup(<Systems/>);expect(await screen.findByText('Synthetic 公司系统')).toBeVisible();expect(screen.getByText('测试基础类型')).toBeVisible();});
  it('system detail masks credentials and shows capabilities',async()=>{setup(<SystemDetail/>,'/systems/sim-fund');expect(await screen.findByText('[MASKED]')).toBeVisible();expect(screen.getByText('get_fund_order')).toBeVisible();expect(document.body.textContent).not.toContain('vault://');});
  it('capability editor uses typed tool claims',()=>{setup(<CapabilityEditor definition={definition} catalog={catalog} onChange={vi.fn()}/>);expect(screen.getByText('事实权威性（独立审核）')).toBeVisible();expect(screen.getByText('新增工具能力')).toBeVisible();});
  it('diff page displays backend diff and before/after values',async()=>{setup(<DiffPage/>,'/versions/a/diff/b');expect(await screen.findByText('高风险')).toBeVisible();expect(screen.getByText('"旧名称"')).toBeVisible();expect(screen.getByText('"新名称"')).toBeVisible();});
  it('approval queue requests only submitted changes',async()=>{const api=setup(<Changes approvals/>);expect(await screen.findByText('支付来源变更')).toBeVisible();expect(api).toHaveBeenCalledWith(expect.stringContaining('status=SUBMITTED'));});
  it('impact page shows digest and paginated case scope',async()=>{setup(<ImpactPage/>,'/change-requests/CR-1');expect(await screen.findByText('CASE-1')).toBeVisible();expect(screen.getByText(/digest-only/)).toBeVisible();expect(screen.getByText('待处理任务')).toBeVisible();});
  it('permission-hidden actions keep viewer read only',async()=>{setup(<Changes/>);await screen.findByText('支付来源变更');expect(screen.queryByText('创建草稿')).not.toBeInTheDocument();});
  it('server 403 is displayed without exposing data',async()=>{setup(<Systems/>,'/',['REGISTRY_VIEW'],()=>{throw new AdminApiError(403,'FORBIDDEN');});expect(await screen.findByText(/无权执行此操作/)).toBeVisible();});
  it('stale version conflict requires refresh and never claims activation',async()=>{
    setup(<ChangeDetail/>,'/change-requests/CR-1',['REGISTRY_VIEW','REGISTRY_APPROVE'],(path,method)=>{if(method==='POST')throw new AdminApiError(409,'STALE_CHANGE_REQUEST');});
    fireEvent.click(await screen.findByRole('button',{name:/批\s*准/}));
    fireEvent.click(screen.getByRole('button',{name:/OK/}));
    expect(await screen.findByText(/版本或状态已变化/)).toBeVisible();expect(screen.queryByText('ACTIVATED')).not.toBeInTheDocument();
  });
  it('import preview shows additions and never automatically activates',async()=>{
    const api=setup(<ImportSystems/>,'/',['REGISTRY_VIEW','REGISTRY_EDIT'],(path)=>path==='/import/preview'?importData:undefined);
    const input=document.querySelector('input[type=file]')!;fireEvent.change(input,{target:{files:[new File(['fake-xlsx'],'systems.xlsx')]}});
    expect(await screen.findByText('新增 1')).toBeVisible();expect(screen.getByText('确认并生成草稿')).toBeVisible();expect(api.mock.calls.some(([p])=>p.includes('/activate'))).toBe(false);
  });
  it('transport uses explicit bearer and CSRF header without cookies',async()=>{
    const fetch=vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:true,json:async()=>({})} as Response);
    await transport(async()=> 'synthetic-token')('/change-requests','POST',{});
    expect(fetch).toHaveBeenCalledWith('/admin-api/registry/change-requests',expect.objectContaining({credentials:'omit',headers:expect.objectContaining({Authorization:'Bearer synthetic-token','X-Registry-Request':'1'})}));
  });
  it('real schema preview distinguishes source rows, unique and shared systems',async()=>{
    setup(<ImportSystems/>,'/',['REGISTRY_VIEW','REGISTRY_EDIT'],path=>path==='/import/preview'?importData:undefined);
    fireEvent.change(document.querySelector('input[type=file]')!,{target:{files:[new File(['synthetic'],'synthetic.xlsx')]}});
    expect(await screen.findByText('66 条来源')).toBeVisible();expect(screen.getByText('64 个公司系统')).toBeVisible();
    expect(screen.getByText('2 个共享系统')).toBeVisible();expect(screen.getByText(/共享系统：aut, sso/)).toBeVisible();
  });
  it('unconfigured company systems remain visible in master list',async()=>{
    setup(<Systems/>,'/',['REGISTRY_VIEW'],path=>path.startsWith('/systems?')?{items:[{...companySummary,agent_configuration_status:'NOT_CONFIGURED',agent_source_count:0,capability_count:0}],total:1}:undefined);
    expect(await screen.findByText('未配置')).toBeVisible();expect(screen.getByText('Synthetic 公司系统')).toBeVisible();
  });
  it('company details show original info and explicit Agent configuration separately',async()=>{
    setup(<SystemDetail/>,'/systems/sim-fund');
    expect(await screen.findByText('公司原始资料')).toBeVisible();expect(screen.getByText('Synthetic original summary')).toBeVisible();
    expect(screen.getByText('Synthetic original functions')).toBeVisible();expect(screen.getByText('智能体能力配置')).toBeVisible();
    expect(screen.getByText('get_fund_order')).toBeVisible();expect(screen.getByText('合作方官方接口')).toBeVisible();
  });
  it('unconfigured detail states no Agent capability',async()=>{
    setup(<SystemDetail/>,'/systems/sim-fund',['REGISTRY_VIEW'],path=>path==='/systems/sim-fund'?{...companyDetail,registry_sources:[],capabilities:[],authority:[],capability_count:0,agent_configuration_status:'NOT_CONFIGURED'}:undefined);
    expect(await screen.findByText('尚未配置智能体能力')).toBeVisible();expect(screen.getByText('Synthetic original summary')).toBeVisible();
  });
  it('approval sends a bounded plain decision comment',async()=>{
    const api=setup(<ChangeDetail/>,'/change-requests/CR-1',['REGISTRY_VIEW','REGISTRY_APPROVE']);
    fireEvent.click(await screen.findByRole('button',{name:/批\s*准/}));
    fireEvent.change(screen.getByRole('textbox',{name:'决策意见'}),{target:{value:'Synthetic approval comment'}});
    fireEvent.click(screen.getByRole('button',{name:/OK/}));
    await waitFor(()=>expect(api).toHaveBeenCalledWith('/change-requests/CR-1/approve','POST',{expected_revision:3,decision_comment:'Synthetic approval comment'}));
  });
  it('reject requires reason before submission',async()=>{
    const api=setup(<ChangeDetail/>,'/change-requests/CR-1',['REGISTRY_VIEW','REGISTRY_APPROVE']);
    fireEvent.click(await screen.findByRole('button',{name:/拒\s*绝/}));
    expect(screen.getByRole('button',{name:/OK/})).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox',{name:'决策意见'}),{target:{value:'Synthetic rejection'}});
    fireEvent.click(screen.getByRole('button',{name:/OK/}));
    await waitFor(()=>expect(api).toHaveBeenCalledWith('/change-requests/CR-1/reject','POST',{expected_revision:3,decision_comment:'Synthetic rejection'}));
  });
  it('inventory activation explicitly keeps Registry Version unchanged',async()=>{
    setup(<ChangeDetail/>,'/change-requests/CR-1',['REGISTRY_VIEW','REGISTRY_ACTIVATE'],path=>path==='/change-requests/CR-1'?{...detail,change_request:{...detail.change_request,kind:'INVENTORY',status:'APPROVED',base_inventory_revision:2},inventory_changes:[company]}:undefined);
    expect(await screen.findByText(/仅更新公司清单修订号 2 → 3；系统清单配置版本不变/)).toBeVisible();
    expect(screen.getByRole('button',{name:'激活公司资料'})).toBeVisible();
  });
  it('conflicting import cannot be confirmed',async()=>{
    setup(<ImportSystems/>,'/',['REGISTRY_VIEW','REGISTRY_EDIT'],path=>path==='/import/preview'?{...importData,conflicts:[{system_code:'aut',code:'CONFLICTING_SYSTEM_DEFINITION',row_refs:['九里云系统!2','融担系统!2']}]}:undefined);
    fireEvent.change(document.querySelector('input[type=file]')!,{target:{files:[new File(['synthetic'],'synthetic.xlsx')]}});
    expect(await screen.findByText('aut: CONFLICTING_SYSTEM_DEFINITION')).toBeVisible();expect(screen.getByRole('button',{name:'确认并生成草稿'})).toBeDisabled();
  });
});
