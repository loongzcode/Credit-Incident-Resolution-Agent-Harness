import { displayLabel } from '../utils/labels';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Col, Descriptions, Form, Input, Modal, Row, Select, Space, Spin, Statistic, Table, Tag, Typography, Upload } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { errorText, useAdmin } from './api';
import type { Catalog, ChangeRequest, Detail, Impact, Inventory, Page, Permission, Preview, CompanySystemSummary, CompanySystemDetail, VersionDiff } from './types';
import { DraftEditor } from './editor';

const root = '/admin/registry';
const short = (value: string | null) => value ? value.slice(0, 12) : '尚未激活';
function useResource<T>(path: string) { const { api, dashboard } = useAdmin(); return useQuery({ queryKey: ['registry', dashboard.identity.user_id, dashboard.active_version, dashboard.inventory_revision, path], queryFn: () => api<T>(path), retry: false }); }
function Loading({ error }: { error?: unknown }) { return error ? <Alert type="error" title={errorText(error)} /> : <Spin />; }
function Failure({ error }: { error: unknown }) { return error ? <Alert type="error" showIcon title={errorText(error)} /> : null; }
function useAction<T>(run: (input: T) => Promise<unknown>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => { void client.invalidateQueries({ queryKey: ['registry'] }); void client.invalidateQueries({ queryKey: ['registry-session'] }); } });
}

export function DashboardPage() {
  const { dashboard } = useAdmin();
  return <><Typography.Title level={2}>系统清单概览</Typography.Title><Row gutter={16}>
    <Col span={8}><Card><Statistic title="公司系统主档" value={dashboard.system_count} /></Card></Col>
    <Col span={8}><Card><Statistic title="公司清单修订号" value={dashboard.inventory_revision} /></Card></Col>
    <Col span={8}><Card title="已激活版本"><span className="registry-hash">{short(dashboard.active_version)}</span></Card></Col>
  </Row><Card title="治理流程"><Typography.Paragraph>公司单点登录 → 应用权限 → 草稿 → 提交 → 独立审批 → 激活 → 运行时</Typography.Paragraph>
    <Typography.Paragraph>Excel 仅导入公司系统资料；来源权威性、工具、写入权限和协议范围必须独立配置并审核。</Typography.Paragraph>
    <Link to={root + '/change-requests'}>查看变更请求</Link></Card></>;
}

export function Systems() {
  const [page, setPage] = useState(1); const [filters, setFilters] = useState<Record<string,string>>({});
  const q = useResource<Page<CompanySystemSummary>>(`/systems?page=${page}&${new URLSearchParams(filters)}`);
  const filter = (key:string,value:string|undefined) => {setPage(1);setFilters(old=>{const next={...old};if(value)next[key]=value;else delete next[key];return next;});};
  return <><Typography.Title level={2}>公司系统清单</Typography.Title><Alert type="info" title="公司系统存在不代表智能体可以访问；能力关联必须在系统清单草稿中显式配置。" />
    <Space wrap>{[['system_code','系统简称'],['system_name','系统名称'],['system_type_label','类型'],['inventory_group','所属系统组']].map(([key,label])=>
      <Input key={key} aria-label={label} placeholder={label} allowClear value={filters[key]||''} onChange={e=>filter(key,e.target.value)} style={{width:180}}/>)}
      <Select aria-label="智能体配置状态" placeholder="智能体配置状态" allowClear style={{width:230}} options={['NOT_CONFIGURED','PARTIALLY_CONFIGURED','CONFIGURED'].map(value=>({value,label:displayLabel(value)}))} onChange={v=>filter('configuration_status',v)}/>
      <Select aria-label="是否已配置能力" placeholder="是否已配置能力" allowClear style={{width:180}} options={[{value:'true',label:'已配置能力'},{value:'false',label:'未配置能力'}]} onChange={v=>filter('has_capability',v)}/></Space>
    {q.data ? <Table rowKey="system_code" dataSource={q.data.items}
    pagination={{ current: page, total: q.data.total, pageSize: 20, onChange: setPage }} columns={[
      { title: '系统简称', dataIndex: 'system_code', render: (v: string) => <Link to={`${root}/systems/${v}`}>{v}</Link> },
      { title: '系统名称', dataIndex: 'system_name' }, { title: '类型', dataIndex: 'system_type_label' },
      { title: '所属系统组', dataIndex: 'inventory_groups', render:(v:string[])=>v.join(' / ') },
      { title: '智能体配置状态', dataIndex: 'agent_configuration_status', render: (v: string) => <Tag>{displayLabel(v)}</Tag> },
      { title: '智能体来源数', dataIndex:'agent_source_count' },{title:'能力数量',dataIndex:'capability_count'}]} /> : <Loading error={q.error} />}</>;
}

export function SystemDetail() {
  const { id } = useParams(); const q = useResource<CompanySystemDetail>(`/systems/${id}`);
  if (!q.data) return <Loading error={q.error} />;
  const s = q.data.company_system;
  return <><Typography.Title level={2}>{s.system_name}</Typography.Title><Card title="公司原始资料"><Descriptions bordered column={2} items={[
    {key:'code',label:'系统简称',children:s.system_code},{key:'type',label:'类型',children:s.system_type_label||'未提供'},
    {key:'groups',label:'所属系统组',children:q.data.inventory_groups.join(' / ')},
    {key:'description',label:'原始简介',children:<span style={{whiteSpace:'pre-wrap'}}>{s.description}</span>},
    {key:'functions',label:'主要功能',children:<span style={{whiteSpace:'pre-wrap'}}>{s.main_functions}</span>}]} />
    <Table rowKey={m=>m.source_metadata.source_row_ref+':'+m.source_metadata.source_file_hash} dataSource={s.memberships} pagination={false} columns={[
      {title:'所属组',dataIndex:'inventory_group'},{title:'来源文件',render:(_,m)=>m.source_metadata.source_file_name},
      {title:'来源工作表 / 行',render:(_,m)=>m.source_metadata.source_row_ref}]} /></Card>
    <Card title="智能体能力配置"><Tag>{displayLabel(q.data.agent_configuration_status)}</Tag>
    {!q.data.capability_count && <Alert type="info" title="尚未配置智能体能力" />}
    {q.data.registry_sources.map(source=><Card key={source.system_id} size="small" title={source.system_id}><Descriptions column={2} items={[
      {key:'channel',label:'来源通道',children:displayLabel(source.source_channel)},{key:'status',label:'状态',children:displayLabel(source.status)},
      {key:'credential',label:'凭据引用',children:source.credential_ref||'未绑定'},
      {key:'partner',label:'合作方',children:source.scope.partner_id||'我司内部范围'},
      {key:'product',label:'产品',children:source.scope.product_codes.join(', ')},
      {key:'protocol',label:'协议',children:source.scope.version_agnostic?'不限定协议版本':source.scope.protocol_versions.join(', ')}]} /></Card>)}
    <Table rowKey="capability_id" dataSource={q.data.capabilities} pagination={false} columns={[{title:'来源',dataIndex:'system_id'}, {title:'工具能力',dataIndex:'capability_id'}, {title:'工具',dataIndex:'tool_name'}, {title:'访问方式',dataIndex:'read_or_write'}, {title:'事实类型',render:(_, c) => c.produces_claim_types.join(', ')},
      {title:'合作方 / 产品 / 协议',render:(_,c)=>[c.scope.partner_id||'neutral',c.scope.product_codes.join(','),c.scope.version_agnostic?'不限定协议版本':c.scope.protocol_versions.join(',')].join(' / ')},
      {title:'状态',dataIndex:'status'}]} />
    <Table rowKey={r=>r.capability_id+':'+r.claim_type} dataSource={q.data.authority} pagination={false} columns={[{title:'工具能力',dataIndex:'capability_id'},{title:'事实类型',dataIndex:'claim_type'},{title:'来源权威性',dataIndex:'authority_level'}]}/></Card>
    <Alert type="info" title="已激活版本只读。如需编辑，请创建变更请求。" /></>;
}

export function Changes({ approvals = false }: { approvals?: boolean }) {
  const { api, dashboard } = useAdmin(); const navigate = useNavigate(); const [page, setPage] = useState(1); const [open, setOpen] = useState(false);
  const q = useResource<Page<ChangeRequest>>(`/change-requests?page=${page}${approvals ? '&status=SUBMITTED' : ''}`);
  const create = useAction(async (values: {title: string; reason: string}) => {
    const cr = await api<ChangeRequest>('/change-requests', 'POST', { ...values, base_registry_version: dashboard.active_version });
    setOpen(false); navigate(`${root}/change-requests/${cr.change_request_id}`);
  });
  return <><div className="registry-toolbar"><Typography.Title level={2}>{approvals ? '审批队列' : '变更请求'}</Typography.Title>
    {dashboard.permissions.includes('REGISTRY_EDIT') && <Button type="primary" onClick={() => setOpen(true)}>创建草稿</Button>}</div>
    <Failure error={create.error} />{q.data ? <Table rowKey="change_request_id" dataSource={q.data.items} pagination={{ current: page, total:q.data.total, pageSize:20,onChange:setPage }} columns={[
      {title:'变更请求',dataIndex:'title',render:(v:string,r:ChangeRequest)=><Link to={`${root}/change-requests/${r.change_request_id}`}>{v}</Link>},
      {title:'状态',dataIndex:'status',render:(v:string)=><Tag>{displayLabel(v)}</Tag>},{title:'类型',dataIndex:'kind'},{title:'创建人',dataIndex:'created_by'},
      {title:'基础版本',dataIndex:'base_registry_version',render:short}]} /> : <Loading error={q.error} />}
    <Modal title="从当前已激活版本创建草稿" open={open} footer={null} onCancel={() => setOpen(false)} destroyOnHidden><Form layout="vertical" onFinish={v => create.mutate(v)}>
      <Form.Item name="title" label="标题" rules={[{required:true}]}><Input maxLength={200} /></Form.Item><Form.Item name="reason" label="变更原因" rules={[{required:true}]}><Input.TextArea maxLength={2000} /></Form.Item>
      <Button htmlType="submit" type="primary" loading={create.isPending}>创建</Button></Form></Modal></>;
}

export function DiffView({ diff }: { diff: VersionDiff }) {
  const sections = [ ['系统新增', diff.systems_added], ['系统移除', diff.systems_removed], ['系统修改', diff.systems_changed.map(x => `${x.entity_id}: ${x.fields.join(', ')}`)],
    ['能力新增',diff.capabilities_added],['能力移除',diff.capabilities_removed],['能力修改',diff.capabilities_changed.map(x=>`${x.entity_id}: ${x.fields.join(', ')}`)],
    ['新增权威规则',diff.authority_added],['移除权威规则',diff.authority_removed],['修改权威规则',diff.authority_changed.map(x=>`${x.entity_id}: ${x.fields.join(', ')}`)],
    ['路由范围',diff.routing_scope_changed],['状态',diff.status_changed] ] as [string,string[]][];
  return <Card title="版本差异 · 后端确定性比较"><Alert type={diff.risk_level === 'HIGH_RISK' ? 'warning' : 'info'} title={displayLabel(diff.risk_level)} description={diff.risk_reasons.join(' · ') || '无已识别高风险维度；仍需独立审批'} />
    <Descriptions column={1} bordered items={sections.map(([label, values]) => ({ key: label, label, children: values.join(' / ') || '—' }))} />
    {[...diff.systems_changed,...diff.capabilities_changed,...diff.authority_changed].map(entry=><Card key={entry.entity_id} size="small" title={entry.entity_id}>
      <Table rowKey="field" size="small" pagination={false} dataSource={entry.fields.map(field=>({field,before:entry.before_values?.[field],after:entry.after_values?.[field]}))}
        columns={[{title:'字段',dataIndex:'field'},{title:'变更前',dataIndex:'before',render:(v:unknown)=><span className="registry-hash">{JSON.stringify(v)}</span>},{title:'变更后',dataIndex:'after',render:(v:unknown)=><span className="registry-hash">{JSON.stringify(v)}</span>}]} /></Card>)}
    </Card>;
}

export function ChangeDetail() {
  const { id } = useParams(); const { api, dashboard } = useAdmin(); const q = useResource<Detail>(`/change-requests/${id}`); const catalog = useResource<Catalog>('/catalog');
  const mutation = useAction(async ({ action, body }: { action: string; body?: unknown }) => api(`/change-requests/${id}${action ? '/' + action : ''}`, action ? 'POST' : 'PATCH', body || {expected_revision:q.data?.change_request.revision}));
  const [confirm, setConfirm] = useState<string | null>(null);
  const [decisionComment, setDecisionComment] = useState('');
  if (!q.data) return <Loading error={q.error} />;
  const { change_request: cr, definition, diff } = q.data;
  const can = (p: Permission) => dashboard.permissions.includes(p);
  const self = [cr.created_by, cr.submitted_by].includes(dashboard.identity.user_id);
  const actions = [ ...(cr.status === 'DRAFT' && can('REGISTRY_SUBMIT') ? ['submit'] : []),
    ...(cr.status === 'SUBMITTED' && can('REGISTRY_APPROVE') && !self ? ['approve','reject'] : []),
    ...(cr.status === 'APPROVED' && can('REGISTRY_ACTIVATE') ? ['activate'] : []),
    ...(['DRAFT','SUBMITTED'].includes(cr.status) && can('REGISTRY_EDIT') ? ['cancel'] : []),
    ...(['DRAFT','SUBMITTED','APPROVED'].includes(cr.status) && can('REGISTRY_EDIT') ? ['supersede'] : []) ];
  const labels: Record<string,string> = {submit:'提交审核',approve:'批准',reject:'拒绝',activate:cr.kind==='INVENTORY'?'激活公司资料':'激活版本',cancel:'取消',supersede:'标记被替代'};
  return <><Typography.Title level={2}>{cr.title}</Typography.Title><Tag>{displayLabel(cr.status)}</Tag><Tag>{displayLabel(cr.kind)}</Tag><p>{cr.reason}</p>
    <Descriptions items={[{key:'creator',label:'创建人',children:cr.created_by},{key:'submitter',label:'提交人',children:cr.submitted_by || '—'},
      {key:'approver',label:'审批人',children:cr.approved_by || '—'},{key:'base',label:'基础版本',children:short(cr.base_registry_version)},
      {key:'candidate',label:'候选版本',children:short(cr.proposed_registry_version)},{key:'revision',label:'变更修订号',children:cr.revision},
      {key:'approval-comment',label:'审批意见',children:cr.approval_comment||'—'},{key:'rejection',label:'拒绝原因',children:cr.rejection_reason||'—'}]} />
    {cr.kind==='INVENTORY'&&<Alert type="info" title={`仅更新公司清单修订号 ${cr.base_inventory_revision ?? 0} → ${(cr.base_inventory_revision ?? 0)+1}；系统清单配置版本不变`} description={cr.status==='ACTIVATED'?'公司资料已激活':'审批通过后仅激活公司资料，不重新发布系统清单配置。'}/>}
    <Failure error={mutation.error} /><Space wrap>{actions.map(a => <Button key={a} type={a === 'activate' ? 'primary' : 'default'} onClick={()=>{setDecisionComment('');setConfirm(a);}}>{labels[a]}</Button>)}
      <Button onClick={()=>void q.refetch()}>刷新</Button></Space>
    {self && cr.status === 'SUBMITTED' && <Alert type="info" title="四眼原则：创建人和提交人不能审批本变更。" />}
    <DiffView diff={diff} /><ImpactPanel id={id!} />
    {cr.kind === 'INVENTORY' && <Card title="待审核公司资料（不会改变智能体能力）"><Table rowKey="system_code" dataSource={q.data.inventory_changes} columns={[
      {title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'简介',dataIndex:'description'},{title:'功能',dataIndex:'main_functions'},
      {title:'类型',dataIndex:'system_type_label'},
      {title:'来源',render:(_,r)=>r.memberships.map(m=>`${m.inventory_group} / ${m.source_metadata.source_file_name} / ${m.source_metadata.source_row_ref}`).join('; ')}]} /></Card>}
    {cr.status === 'DRAFT' && cr.kind === 'CONFIGURATION' && can('REGISTRY_EDIT') && catalog.data && <DraftEditor key={cr.revision} initial={definition} catalog={catalog.data} loading={mutation.isPending}
      onSave={d => mutation.mutate({action:'',body:{expected_revision:cr.revision,title:cr.title,reason:cr.reason,definition:d}})} />}
    <Modal title={confirm ? labels[confirm] : ''} open={!!confirm} confirmLoading={mutation.isPending} onCancel={()=>setConfirm(null)}
      okButtonProps={{disabled:(confirm==='reject'&&!decisionComment.trim())||/[<>]/.test(decisionComment)}}
      onOk={()=>{ if(confirm) mutation.mutate({action:confirm,body:{expected_revision:cr.revision,...(['approve','reject'].includes(confirm)?{decision_comment:decisionComment.trim()||null}:{})}},{onSuccess:()=>setConfirm(null),onError:()=>setConfirm(null)}); }}>
      <p>请确认已审核上方差异及影响分析。操作绑定当前变更修订号 {cr.revision}，后端会重新检查权限、状态和 基础版本。</p>
      {['approve','reject'].includes(confirm||'')&&<label>{confirm==='reject'?'拒绝原因（必填）':'审批意见（可选）'}<Input.TextArea aria-label="决策意见" value={decisionComment} maxLength={2000} showCount onChange={e=>setDecisionComment(e.target.value)}/><p>仅限纯文本，不接受 HTML。</p></label>}</Modal></>;
}

export function ImpactPanel({ id }: { id: string }) {
  const [page,setPage]=useState(1); const q=useResource<Impact>(`/change-requests/${id}/impact?page=${page}`);
  if(!q.data) return <Loading error={q.error} />;
  const d=q.data;
  return <Card title="影响分析 · 只读估计"><Alert type={d.base_is_current?'info':'error'} title={d.base_is_current?'按系统清单全局版本的重新校验范围保守计算':'基础版本已过期，需重新创建变更'} />
    <Row gutter={16}><Col span={8}><Statistic title="受影响的活动任务" value={d.affected_active_case_count} /></Col><Col span={8}><Statistic title="将失效的快照" value={d.invalidated_snapshot_count} /></Col><Col span={8}><Statistic title="待处理任务" value={d.affected_pending_work_count} /></Col></Row>
    <Typography.Paragraph className="registry-hash">任务编号摘要： {d.affected_case_ids_digest}</Typography.Paragraph>
    <p>{d.affected_partner_products.map(p=>[p.asset_partner,p.funding_partner,p.product_code].filter(Boolean).join(' / ')).join('; ')}</p>
    <p>变更能力类型： {d.changed_capability_types.join(', ')||'无'}</p><p>运行时重新校验范围： {d.runtime_revalidation_scope}</p>
    <Table size="small" rowKey="id" dataSource={d.case_page.items.map(id=>({id}))} columns={[{title:'受影响任务（分页）',dataIndex:'id'}]}
      pagination={{current:page,pageSize:20,total:d.case_page.total,onChange:setPage}} /></Card>;
}
export function ImpactPage(){const {id}=useParams();return <ImpactPanel id={id!}/>;}

export function Versions() {
  const {api,dashboard}=useAdmin(); const [page,setPage]=useState(1); const q=useResource<Page<{version:string;active:boolean;activated_before:boolean;registered_at:string}>>(`/versions?page=${page}`);
  const [a,setA]=useState<string>();const [b,setB]=useState<string>();const [target,setTarget]=useState<string|null>(null);const navigate=useNavigate();
  const rollback=useAction(async(v:{title:string;reason:string})=>{const cr=await api<ChangeRequest>('/rollback','POST',{...v,target_version:target,base_registry_version:dashboard.active_version});navigate(`${root}/change-requests/${cr.change_request_id}`);});
  return <><Typography.Title level={2}>版本历史</Typography.Title><Failure error={rollback.error}/>
    <Space><Select aria-label="基础版本" placeholder="比较起点" style={{width:210}} options={q.data?.items.map(x=>({label:short(x.version),value:x.version}))} onChange={setA}/>
      <Select aria-label="目标版本" placeholder="比较终点" style={{width:210}} options={q.data?.items.map(x=>({label:short(x.version),value:x.version}))} onChange={setB}/>
      {a&&b&&<Link to={`${root}/versions/${a}/diff/${b}`}>查看差异</Link>}</Space>
    {q.data?<Table rowKey="version" dataSource={q.data.items} pagination={{current:page,total:q.data.total,pageSize:20,onChange:setPage}} columns={[
      {title:'版本哈希',dataIndex:'version',render:(v:string)=><span className="registry-hash">{v}</span>},{title:'是否激活',dataIndex:'active',render:(v:boolean)=>v?<Tag color="green">已激活</Tag>:'历史 / 候选版本'},
      {title:'登记时间',dataIndex:'registered_at'},
      {title:'操作',render:(_,r)=>dashboard.permissions.includes('REGISTRY_ROLLBACK')&&!r.active&&r.activated_before?<Button onClick={()=>setTarget(r.version)}>申请回滚</Button>:null}]} />:<Loading error={q.error}/>}
    <Modal title="创建回滚变更请求" open={!!target} footer={null} onCancel={()=>setTarget(null)}><Form layout="vertical" onFinish={v=>rollback.mutate(v)}>
      <Form.Item name="title" label="标题" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="reason" label="回滚原因" rules={[{required:true}]}><Input.TextArea/></Form.Item><Button htmlType="submit" loading={rollback.isPending}>生成草稿，进入审批</Button></Form></Modal></>;
}
export function DiffPage(){const {a,b}=useParams();const q=useResource<VersionDiff>(`/versions/${a}/diff/${b}`);return q.data?<DiffView diff={q.data}/>:<Loading error={q.error}/>;}

export function AuditLog(){const [page,setPage]=useState(1);const q=useResource<Page<{event:string;actor:string;time:string;request_id:string;before_version:string;after_version:string;change_request_id:string;decision_comment?:string}>>(`/audit?page=${page}`);
  return <><Typography.Title level={2}>审计日志</Typography.Title>{q.data?<Table rowKey={r=>`${r.request_id}:${r.event}:${r.change_request_id}`} dataSource={q.data.items} pagination={{current:page,pageSize:20,total:q.data.total,onChange:setPage}} columns={[
    {title:'时间',dataIndex:'time'},{title:'动作',dataIndex:'event'},{title:'操作人',dataIndex:'actor'},{title:'变更请求',dataIndex:'change_request_id'},
    {title:'变更前',dataIndex:'before_version',render:short},{title:'变更后',dataIndex:'after_version',render:short},{title:'审批意见',dataIndex:'decision_comment'},{title:'请求编号',dataIndex:'request_id'}]}/>:<Loading error={q.error}/>}</>;
}

export function ImportSystems(){const {api,dashboard}=useAdmin();const [preview,setPreview]=useState<Preview|null>(null);const navigate=useNavigate();const inventory=useResource<Page<Inventory>>('/inventory');
  const upload=useAction(async(file:File)=>{const data=new FormData();data.append('file',file);setPreview(await api<Preview>('/import/preview','POST',data));});
  const confirm=useAction(async(v:{title:string;reason:string})=>{const cr=await api<ChangeRequest>('/import/confirm','POST',{...v,preview_id:preview?.preview_id});navigate(`${root}/change-requests/${cr.change_request_id}`);});
  return <><Typography.Title level={2}>导入公司系统</Typography.Title><Alert type="info" title="Excel 仅更新公司清单，不推断工具、权威来源或写入权限。确认后生成草稿，经审核激活才更新清单。" />
    <Failure error={upload.error||confirm.error}/>{dashboard.permissions.includes('REGISTRY_EDIT')&&<Upload accept=".xlsx" showUploadList={false} beforeUpload={file=>{upload.mutate(file);return false;}}><Button loading={upload.isPending}>上传 XLSX 并预览</Button></Upload>}
    {preview&&<Card title="导入预览"><Space wrap><Tag>{preview.source_rows} 条来源</Tag><Tag>{preview.unique_systems} 个公司系统</Tag><Tag>{preview.shared_systems.length} 个共享系统</Tag><Tag>新增 {preview.added.length}</Tag><Tag>变更 {preview.changed.length}</Tag><Tag>未变更 {preview.unchanged.length}</Tag><Tag color="red">冲突 {preview.conflicts.length}</Tag><Tag color="red">无效 {preview.invalid.length}</Tag></Space>
      <p>所属系统组：{preview.groups.join(' / ')}</p><p>共享系统：{preview.shared_systems.join(', ')||'无'}</p>
      {preview.conflicts.map((r,i)=><Alert key={i} type="error" title={`${r.system_code}: ${r.code}`} description={r.row_refs.join(' / ')}/>)}
      {preview.invalid.map(r=><Alert key={r.row_ref} type="error" title={`${r.row_ref}: ${r.code}`}/>)}
      <Table rowKey="system_code" dataSource={preview.rows} columns={[{title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'类型',dataIndex:'system_type_label'},{title:'简介',dataIndex:'description'},{title:'来源行',render:(_,r)=>r.memberships.map(m=>m.source_metadata.source_row_ref).join(' / ')}]}/>
      <Form layout="vertical" onFinish={v=>confirm.mutate(v)}><Form.Item name="title" label="导入标题" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="reason" label="原因" rules={[{required:true}]}><Input.TextArea/></Form.Item>
        <Button htmlType="submit" type="primary" loading={confirm.isPending} disabled={!!preview.invalid.length||!!preview.conflicts.length||!preview.rows.length}>确认并生成草稿</Button></Form></Card>}
    <Card title="当前公司资料（与工具能力配置独立）"><Table rowKey="system_code" dataSource={inventory.data?.items} columns={[{title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'类型',dataIndex:'system_type_label'},{title:'原始文件',render:(_,r)=>[...new Set(r.memberships.map(m=>m.source_metadata.source_file_name))].join(' / ')}]}/></Card></>;
}
