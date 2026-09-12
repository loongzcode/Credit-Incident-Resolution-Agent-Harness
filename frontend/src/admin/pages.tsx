import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Col, Descriptions, Form, Input, Modal, Row, Select, Space, Spin, Statistic, Table, Tag, Typography, Upload } from 'antd';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { errorText, useAdmin } from './api';
import type { Catalog, ChangeRequest, Definition, Detail, Impact, Inventory, Page, Permission, Preview, SystemDefinition, VersionDiff } from './types';
import { DraftEditor } from './editor';

const root = '/admin/registry';
const short = (value: string | null) => value ? value.slice(0, 12) : '尚未激活';
function useResource<T>(path: string) { const { api, dashboard } = useAdmin(); return useQuery({ queryKey: ['registry', dashboard.identity.user_id, dashboard.active_version, path], queryFn: () => api<T>(path), retry: false }); }
function Loading({ error }: { error?: unknown }) { return error ? <Alert type="error" title={errorText(error)} /> : <Spin />; }
function Failure({ error }: { error: unknown }) { return error ? <Alert type="error" showIcon title={errorText(error)} /> : null; }
function useAction<T>(run: (input: T) => Promise<unknown>) {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => { void client.invalidateQueries({ queryKey: ['registry'] }); void client.invalidateQueries({ queryKey: ['registry-session'] }); } });
}

export function DashboardPage() {
  const { dashboard } = useAdmin();
  return <><Typography.Title level={2}>Registry Dashboard</Typography.Title><Row gutter={16}>
    <Col span={8}><Card><Statistic title="已登记系统" value={dashboard.system_count} /></Card></Col>
    <Col span={8}><Card><Statistic title="公司清单 revision" value={dashboard.inventory_revision} /></Card></Col>
    <Col span={8}><Card title="Active version"><span className="registry-hash">{short(dashboard.active_version)}</span></Card></Col>
  </Row><Card title="治理流程"><Typography.Paragraph>公司 SSO → 应用权限 → Draft → Submit → 独立 Approve → Activate → Runtime</Typography.Paragraph>
    <Typography.Paragraph>Excel 只导入公司系统资料；来源权威、Tool、WRITE 和协议范围必须通过独立配置与审核。</Typography.Paragraph>
    <Link to={root + '/change-requests'}>查看变更请求</Link></Card></>;
}

export function Systems() {
  const [page, setPage] = useState(1); const q = useResource<Page<SystemDefinition>>(`/systems?page=${page}`);
  return <><Typography.Title level={2}>系统清单</Typography.Title>{q.data ? <Table rowKey="system_id" dataSource={q.data.items}
    pagination={{ current: page, total: q.data.total, pageSize: 20, onChange: setPage }} columns={[
      { title: 'System', dataIndex: 'system_id', render: (v: string) => <Link to={`${root}/systems/${v}`}>{v}</Link> },
      { title: '业务名称', dataIndex: 'display_name' }, { title: '类型', dataIndex: 'system_type' }, { title: '接入渠道', dataIndex: 'source_channel' },
      { title: '状态', dataIndex: 'status', render: (v: string) => <Tag>{v}</Tag> }, { title: 'Owner', dataIndex: 'owner_team' }]} /> : <Loading error={q.error} />}</>;
}

export function SystemDetail() {
  const { id } = useParams(); const q = useResource<{ system: SystemDefinition; capabilities: Definition['capabilities']; authority: Definition['authority_rules'] }>(`/systems/${id}`);
  if (!q.data) return <Loading error={q.error} />;
  const s = q.data.system;
  return <><Typography.Title level={2}>{s.display_name}</Typography.Title><Descriptions bordered column={2} items={[
    { key: 'id', label: 'System ID', children: s.system_id }, { key: 'source', label: 'Source channel', children: s.source_channel },
    { key: 'status', label: 'Status', children: s.status }, { key: 'credential', label: 'Credential reference', children: s.credential_ref || '未绑定' },
    { key: 'partner', label: 'Partner', children: s.scope.partner_id || '我司内部 neutral scope' }, { key: 'protocol', label: 'Protocol', children: s.scope.version_agnostic ? 'Version agnostic' : s.scope.protocol_versions.join(', ') }]} />
    <Table rowKey="capability_id" dataSource={q.data.capabilities} pagination={false} columns={[{title:'Capability',dataIndex:'capability_id'}, {title:'Tool',dataIndex:'tool_name'}, {title:'Access',dataIndex:'read_or_write'}, {title:'Claims',render:(_, c) => c.produces_claim_types.join(', ')}]} />
    <Alert type="info" title="Active 版本只读。如需编辑，请创建变更请求。" /></>;
}

export function Changes({ approvals = false }: { approvals?: boolean }) {
  const { api, dashboard } = useAdmin(); const navigate = useNavigate(); const [page, setPage] = useState(1); const [open, setOpen] = useState(false);
  const q = useResource<Page<ChangeRequest>>(`/change-requests?page=${page}${approvals ? '&status=SUBMITTED' : ''}`);
  const create = useAction(async (values: {title: string; reason: string}) => {
    const cr = await api<ChangeRequest>('/change-requests', 'POST', { ...values, base_registry_version: dashboard.active_version });
    setOpen(false); navigate(`${root}/change-requests/${cr.change_request_id}`);
  });
  return <><div className="registry-toolbar"><Typography.Title level={2}>{approvals ? '审批队列' : 'Change Requests'}</Typography.Title>
    {dashboard.permissions.includes('REGISTRY_EDIT') && <Button type="primary" onClick={() => setOpen(true)}>创建 Draft</Button>}</div>
    <Failure error={create.error} />{q.data ? <Table rowKey="change_request_id" dataSource={q.data.items} pagination={{ current: page, total:q.data.total, pageSize:20,onChange:setPage }} columns={[
      {title:'变更请求',dataIndex:'title',render:(v:string,r:ChangeRequest)=><Link to={`${root}/change-requests/${r.change_request_id}`}>{v}</Link>},
      {title:'状态',dataIndex:'status',render:(v:string)=><Tag>{v}</Tag>},{title:'类型',dataIndex:'kind'},{title:'创建人',dataIndex:'created_by'},
      {title:'Base version',dataIndex:'base_registry_version',render:short}]} /> : <Loading error={q.error} />}
    <Modal title="从当前 Active 创建 Draft" open={open} footer={null} onCancel={() => setOpen(false)} destroyOnHidden><Form layout="vertical" onFinish={v => create.mutate(v)}>
      <Form.Item name="title" label="标题" rules={[{required:true}]}><Input maxLength={200} /></Form.Item><Form.Item name="reason" label="变更原因" rules={[{required:true}]}><Input.TextArea maxLength={2000} /></Form.Item>
      <Button htmlType="submit" type="primary" loading={create.isPending}>创建</Button></Form></Modal></>;
}

export function DiffView({ diff }: { diff: VersionDiff }) {
  const sections = [ ['系统新增', diff.systems_added], ['系统移除', diff.systems_removed], ['系统修改', diff.systems_changed.map(x => `${x.entity_id}: ${x.fields.join(', ')}`)],
    ['能力新增',diff.capabilities_added],['能力移除',diff.capabilities_removed],['能力修改',diff.capabilities_changed.map(x=>`${x.entity_id}: ${x.fields.join(', ')}`)],
    ['Authority 新增',diff.authority_added],['Authority 移除',diff.authority_removed],['Authority 修改',diff.authority_changed.map(x=>`${x.entity_id}: ${x.fields.join(', ')}`)],
    ['Routing scope',diff.routing_scope_changed],['Status',diff.status_changed] ] as [string,string[]][];
  return <Card title="Version Diff · 后端确定性比较"><Alert type={diff.risk_level === 'HIGH_RISK' ? 'warning' : 'info'} title={diff.risk_level} description={diff.risk_reasons.join(' · ') || '无已识别高风险维度；仍需独立审批'} />
    <Descriptions column={1} bordered items={sections.map(([label, values]) => ({ key: label, label, children: values.join(' / ') || '—' }))} />
    {[...diff.systems_changed,...diff.capabilities_changed,...diff.authority_changed].map(entry=><Card key={entry.entity_id} size="small" title={entry.entity_id}>
      <Table rowKey="field" size="small" pagination={false} dataSource={entry.fields.map(field=>({field,before:entry.before_values?.[field],after:entry.after_values?.[field]}))}
        columns={[{title:'字段',dataIndex:'field'},{title:'Before',dataIndex:'before',render:(v:unknown)=><span className="registry-hash">{JSON.stringify(v)}</span>},{title:'After',dataIndex:'after',render:(v:unknown)=><span className="registry-hash">{JSON.stringify(v)}</span>}]} /></Card>)}
    </Card>;
}

export function ChangeDetail() {
  const { id } = useParams(); const { api, dashboard } = useAdmin(); const q = useResource<Detail>(`/change-requests/${id}`); const catalog = useResource<Catalog>('/catalog');
  const mutation = useAction(async ({ action, body }: { action: string; body?: unknown }) => api(`/change-requests/${id}${action ? '/' + action : ''}`, action ? 'POST' : 'PATCH', body || {expected_revision:q.data?.change_request.revision}));
  const [confirm, setConfirm] = useState<string | null>(null);
  if (!q.data) return <Loading error={q.error} />;
  const { change_request: cr, definition, diff } = q.data;
  const can = (p: Permission) => dashboard.permissions.includes(p);
  const self = [cr.created_by, cr.submitted_by].includes(dashboard.identity.user_id);
  const actions = [ ...(cr.status === 'DRAFT' && can('REGISTRY_SUBMIT') ? ['submit'] : []),
    ...(cr.status === 'SUBMITTED' && can('REGISTRY_APPROVE') && !self ? ['approve','reject'] : []),
    ...(cr.status === 'APPROVED' && can('REGISTRY_ACTIVATE') ? ['activate'] : []),
    ...(['DRAFT','SUBMITTED'].includes(cr.status) && can('REGISTRY_EDIT') ? ['cancel'] : []),
    ...(['DRAFT','SUBMITTED','APPROVED'].includes(cr.status) && can('REGISTRY_EDIT') ? ['supersede'] : []) ];
  const labels: Record<string,string> = {submit:'提交审核',approve:'批准',reject:'拒绝',activate:'激活版本',cancel:'取消',supersede:'标记被替代'};
  return <><Typography.Title level={2}>{cr.title}</Typography.Title><Tag>{cr.status}</Tag><Tag>{cr.kind}</Tag><p>{cr.reason}</p>
    <Descriptions items={[{key:'creator',label:'Creator',children:cr.created_by},{key:'submitter',label:'Submitter',children:cr.submitted_by || '—'},
      {key:'approver',label:'Approver',children:cr.approved_by || '—'},{key:'base',label:'Base',children:short(cr.base_registry_version)},
      {key:'candidate',label:'Candidate',children:short(cr.proposed_registry_version)},{key:'revision',label:'CR revision',children:cr.revision}]} />
    <Failure error={mutation.error} /><Space wrap>{actions.map(a => <Button key={a} type={a === 'activate' ? 'primary' : 'default'} onClick={()=>setConfirm(a)}>{labels[a]}</Button>)}
      <Button onClick={()=>void q.refetch()}>刷新</Button></Space>
    {self && cr.status === 'SUBMITTED' && <Alert type="info" title="四眼原则：创建人和提交人不能审批本变更。" />}
    <DiffView diff={diff} /><ImpactPanel id={id!} />
    {cr.kind === 'INVENTORY' && <Card title="待审核公司资料（不会改变 Agent Capability）"><Table rowKey="system_code" dataSource={q.data.inventory_changes} columns={[
      {title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'简介',dataIndex:'description'},{title:'功能',dataIndex:'main_functions'},
      {title:'来源',render:(_,r)=>`${r.source_metadata.source_file_name} / ${r.source_metadata.source_row_ref}`}]} /></Card>}
    {cr.status === 'DRAFT' && cr.kind === 'CONFIGURATION' && can('REGISTRY_EDIT') && catalog.data && <DraftEditor key={cr.revision} initial={definition} catalog={catalog.data} loading={mutation.isPending}
      onSave={d => mutation.mutate({action:'',body:{expected_revision:cr.revision,title:cr.title,reason:cr.reason,definition:d}})} />}
    <Modal title={confirm ? labels[confirm] : ''} open={!!confirm} confirmLoading={mutation.isPending} onCancel={()=>setConfirm(null)}
      onOk={()=>{ if(confirm) mutation.mutate({action:confirm},{onSuccess:()=>setConfirm(null),onError:()=>setConfirm(null)}); }}>
      <p>请确认已审核上方差异及影响分析。操作绑定当前 CR revision {cr.revision}，后端会重新检查权限、状态和 Base version。</p></Modal></>;
}

export function ImpactPanel({ id }: { id: string }) {
  const [page,setPage]=useState(1); const q=useResource<Impact>(`/change-requests/${id}/impact?page=${page}`);
  if(!q.data) return <Loading error={q.error} />;
  const d=q.data;
  return <Card title="Impact Analysis · 只读估计"><Alert type={d.base_is_current?'info':'error'} title={d.base_is_current?'按 Registry 全局版本重验边界保守计算':'Base version 已过期，需重新创建变更'} />
    <Row gutter={16}><Col span={8}><Statistic title="受影响 Active Case" value={d.affected_active_case_count} /></Col><Col span={8}><Statistic title="将失效的快照" value={d.invalidated_snapshot_count} /></Col><Col span={8}><Statistic title="Pending Work" value={d.affected_pending_work_count} /></Col></Row>
    <Typography.Paragraph className="registry-hash">Case IDs digest: {d.affected_case_ids_digest}</Typography.Paragraph>
    <p>{d.affected_partner_products.map(p=>[p.asset_partner,p.funding_partner,p.product_code].filter(Boolean).join(' / ')).join('; ')}</p>
    <p>{d.affected_capability_types.join(', ')}</p>
    <Table size="small" rowKey="id" dataSource={d.case_page.items.map(id=>({id}))} columns={[{title:'受影响 Case（分页）',dataIndex:'id'}]}
      pagination={{current:page,pageSize:20,total:d.case_page.total,onChange:setPage}} /></Card>;
}
export function ImpactPage(){const {id}=useParams();return <ImpactPanel id={id!}/>;}

export function Versions() {
  const {api,dashboard}=useAdmin(); const [page,setPage]=useState(1); const q=useResource<Page<{version:string;active:boolean;activated_before:boolean;registered_at:string}>>(`/versions?page=${page}`);
  const [a,setA]=useState<string>();const [b,setB]=useState<string>();const [target,setTarget]=useState<string|null>(null);const navigate=useNavigate();
  const rollback=useAction(async(v:{title:string;reason:string})=>{const cr=await api<ChangeRequest>('/rollback','POST',{...v,target_version:target,base_registry_version:dashboard.active_version});navigate(`${root}/change-requests/${cr.change_request_id}`);});
  return <><Typography.Title level={2}>Version History</Typography.Title><Failure error={rollback.error}/>
    <Space><Select aria-label="Base version" placeholder="比较起点" style={{width:210}} options={q.data?.items.map(x=>({label:short(x.version),value:x.version}))} onChange={setA}/>
      <Select aria-label="Target version" placeholder="比较终点" style={{width:210}} options={q.data?.items.map(x=>({label:short(x.version),value:x.version}))} onChange={setB}/>
      {a&&b&&<Link to={`${root}/versions/${a}/diff/${b}`}>查看差异</Link>}</Space>
    {q.data?<Table rowKey="version" dataSource={q.data.items} pagination={{current:page,total:q.data.total,pageSize:20,onChange:setPage}} columns={[
      {title:'Version hash',dataIndex:'version',render:(v:string)=><span className="registry-hash">{v}</span>},{title:'Active',dataIndex:'active',render:(v:boolean)=>v?<Tag color="green">ACTIVE</Tag>:'历史 / Candidate'},
      {title:'登记时间',dataIndex:'registered_at'},
      {title:'操作',render:(_,r)=>dashboard.permissions.includes('REGISTRY_ROLLBACK')&&!r.active&&r.activated_before?<Button onClick={()=>setTarget(r.version)}>申请回滚</Button>:null}]} />:<Loading error={q.error}/>}
    <Modal title="创建 Rollback Change Request" open={!!target} footer={null} onCancel={()=>setTarget(null)}><Form layout="vertical" onFinish={v=>rollback.mutate(v)}>
      <Form.Item name="title" label="标题" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="reason" label="回滚原因" rules={[{required:true}]}><Input.TextArea/></Form.Item><Button htmlType="submit" loading={rollback.isPending}>生成 Draft，进入审批</Button></Form></Modal></>;
}
export function DiffPage(){const {a,b}=useParams();const q=useResource<VersionDiff>(`/versions/${a}/diff/${b}`);return q.data?<DiffView diff={q.data}/>:<Loading error={q.error}/>;}

export function AuditLog(){const [page,setPage]=useState(1);const q=useResource<Page<{event:string;actor:string;time:string;request_id:string;before_version:string;after_version:string;change_request_id:string}>>(`/audit?page=${page}`);
  return <><Typography.Title level={2}>Audit Trail</Typography.Title>{q.data?<Table rowKey={r=>`${r.request_id}:${r.event}:${r.change_request_id}`} dataSource={q.data.items} pagination={{current:page,pageSize:20,total:q.data.total,onChange:setPage}} columns={[
    {title:'时间',dataIndex:'time'},{title:'动作',dataIndex:'event'},{title:'Actor',dataIndex:'actor'},{title:'Change request',dataIndex:'change_request_id'},
    {title:'Before',dataIndex:'before_version',render:short},{title:'After',dataIndex:'after_version',render:short},{title:'Request ID',dataIndex:'request_id'}]}/>:<Loading error={q.error}/>}</>;
}

export function ImportSystems(){const {api,dashboard}=useAdmin();const [preview,setPreview]=useState<Preview|null>(null);const navigate=useNavigate();const inventory=useResource<Page<Inventory>>('/inventory');
  const upload=useAction(async(file:File)=>{const data=new FormData();data.append('file',file);setPreview(await api<Preview>('/import/preview','POST',data));});
  const confirm=useAction(async(v:{title:string;reason:string})=>{const cr=await api<ChangeRequest>('/import/confirm','POST',{...v,preview_id:preview?.preview_id});navigate(`${root}/change-requests/${cr.change_request_id}`);});
  return <><Typography.Title level={2}>Import Company Systems</Typography.Title><Alert type="info" title="Excel 仅更新公司清单。不会推断 Tool、权威来源或 WRITE 权限；确认后生成 Draft，审核激活后才更新清单。" />
    <Failure error={upload.error||confirm.error}/>{dashboard.permissions.includes('REGISTRY_EDIT')&&<Upload accept=".xlsx" showUploadList={false} beforeUpload={file=>{upload.mutate(file);return false;}}><Button loading={upload.isPending}>上传 XLSX 并预览</Button></Upload>}
    {preview&&<Card title="Import Preview"><Space><Tag>Added {preview.added.length}</Tag><Tag>Changed {preview.changed.length}</Tag><Tag>Unchanged {preview.unchanged.length}</Tag><Tag color="red">Invalid {preview.invalid.length}</Tag></Space>
      {preview.invalid.map(r=><Alert key={r.row_ref} type="error" title={`${r.row_ref}: ${r.code}`}/>)}
      <Table rowKey="system_code" dataSource={preview.rows} columns={[{title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'简介',dataIndex:'description'},{title:'来源行',render:(_,r)=>r.source_metadata.source_row_ref}]}/>
      <Form layout="vertical" onFinish={v=>confirm.mutate(v)}><Form.Item name="title" label="导入标题" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="reason" label="原因" rules={[{required:true}]}><Input.TextArea/></Form.Item>
        <Button htmlType="submit" type="primary" loading={confirm.isPending} disabled={!!preview.invalid.length||!preview.rows.length}>确认并生成 Draft</Button></Form></Card>}
    <Card title="当前公司资料（与 capability 配置独立）"><Table rowKey="system_code" dataSource={inventory.data?.items} columns={[{title:'编码',dataIndex:'system_code'},{title:'名称',dataIndex:'system_name'},{title:'类别',dataIndex:'system_category'},{title:'原始文件',render:(_,r)=>r.source_metadata.source_file_name}]}/></Card></>;
}
