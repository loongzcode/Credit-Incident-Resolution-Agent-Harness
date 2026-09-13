import { displayLabel } from '../utils/labels';
import { useMemo, useState } from 'react';
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type NodeProps, type Node } from '@xyflow/react';
import { Button, Descriptions, Drawer, Modal, Select, Tag } from 'antd';
import type { Graph, Hypothesis } from '../types';
import { EvidenceRefs, Panel, StatusTag } from './common';
import '@xyflow/react/dist/style.css';

type GraphNode = Node<{ title: string; status: string; statement: string }>;
function HypothesisNode({ data }: NodeProps<GraphNode>) {
  return <div className={`hypothesis-node status-${data.status.toLowerCase()}`}>
    <Handle type="target" position={Position.Top} /><div className="node-heading"><strong>{data.title}</strong><StatusTag value={data.status} /></div>
    <div className="node-statement">{displayLabel(data.statement)}</div><Handle type="source" position={Position.Bottom} />
  </div>;
}
const nodeTypes = { hypothesis: HypothesisNode };

export function HypothesisDetail({ graph, hypothesis, onEvidence }: { graph: Graph; hypothesis: Hypothesis; onEvidence: (id: string) => void }) {
  const definition = graph.definitions.find(d => d.hypothesis_id === hypothesis.hypothesis_id)!;
  const gaps = graph.open_gaps.filter(g => g.hypothesis_ids.includes(hypothesis.hypothesis_id));
  return <><StatusTag value={hypothesis.status} /><h3>{displayLabel(definition.statement)}</h3>
    <Descriptions column={1} size="small" items={[
      { key: 'kind', label: '类型', children: displayLabel(definition.kind) },
      { key: 'reason', label: '判断依据', children: hypothesis.reason },
      { key: 'support', label: '支持证据数', children: hypothesis.supporting_evidence_refs.length },
      { key: 'contradict', label: '反证数量', children: hypothesis.contradicting_evidence_refs.length },
    ]} />
    <h3>决定性证据引用</h3><EvidenceRefs refs={hypothesis.decisive_evidence_refs} onSelect={onEvidence} />
    <h3>支持证据引用</h3><EvidenceRefs refs={hypothesis.supporting_evidence_refs} onSelect={onEvidence} />
    <h3>反证引用</h3><EvidenceRefs refs={hypothesis.contradicting_evidence_refs} onSelect={onEvidence} />
    <h3>待补充缺口</h3>{gaps.length ? gaps.map(g => <div className="gap" key={g.gap_id}><StatusTag value={g.priority_class} /><p>{g.question}</p><code>{g.gap_id}</code></div>) : <p className="muted">暂无关联的未解决缺口</p>}
  </>;
}

export function HypothesisGraphPanel({ graph, onEvidence }: { graph: Graph; onEvidence: (id: string) => void }) {
  const [selected, setSelected] = useState<string>();
  const [expanded, setExpanded] = useState(false);
  const hypothesis = graph.hypotheses.find(h => h.hypothesis_id === selected);
  const { nodes, edges } = useMemo(() => {
    const roots = graph.definitions.filter(d => !d.parent_hypothesis_id);
    const nodes: GraphNode[] = graph.definitions.map(d => {
      const parentIndex = roots.findIndex(r => r.hypothesis_id === (d.parent_hypothesis_id || d.hypothesis_id));
      const siblings = graph.definitions.filter(r => r.parent_hypothesis_id === d.parent_hypothesis_id);
      return { id: d.hypothesis_id, type: 'hypothesis', position: {
        x: (parentIndex % 3) * 350 + (d.parent_hypothesis_id ? siblings.findIndex(s => s.hypothesis_id === d.hypothesis_id) * 320 - 160 : 0),
        y: Math.floor(parentIndex / 3) * 380 + (d.parent_hypothesis_id ? 190 : 0),
      }, data: { title: d.hypothesis_id, statement: d.statement, status: graph.hypotheses.find(h => h.hypothesis_id === d.hypothesis_id)!.status },
        ariaLabel: `${d.hypothesis_id} ${d.statement}` };
    });
    const edges: Edge[] = graph.definitions.filter(d => d.parent_hypothesis_id).map(d => ({ id: `parent-${d.hypothesis_id}`, source: d.parent_hypothesis_id!, target: d.hypothesis_id, markerEnd: { type: MarkerType.ArrowClosed }, label: '细化假设' }));
    graph.open_gaps.forEach((gap, i) => {
      nodes.push({ id: gap.gap_id, type: 'hypothesis', position: { x: 1240, y: i * 180 },
        data: { title: '证据缺口', status: gap.priority_class, statement: gap.gap_id.split(':').at(-1) || gap.question } });
      gap.hypothesis_ids.forEach(h => edges.push({ id: `${gap.gap_id}-${h}`, source: h, target: gap.gap_id, markerEnd: { type: MarkerType.ArrowClosed }, label: '需要证据' }));
    });
    return { nodes, edges };
  }, [graph]);
  return <Panel title="故障假设图谱" extra={<Tag>规则版本 v{graph.rule_version}</Tag>}>
    <div className="graph-toolbar"><span className="muted">假设层级与证据缺口</span>
      <Button size="small" onClick={() => setExpanded(true)}>展开图谱</Button>
      <Select aria-label="查看假设" placeholder="查看假设" value={selected} onChange={setSelected} options={graph.hypotheses.map(h => ({ value: h.hypothesis_id, label: `${h.hypothesis_id} · ${displayLabel(h.status)}` }))} /></div>
    <div className="graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.25} maxZoom={1.6} nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} onNodeClick={(_, node) => setSelected(node.id)}><Background gap={20} /><Controls showInteractive={false} /></ReactFlow></div>
    <div className="graph-legend">{['CONFIRMED', 'SUPPORTED', 'POSSIBLE', 'UNKNOWN', 'ELIMINATED'].map(s => <StatusTag key={s} value={s} />)}</div>
    <div className="hypothesis-index">{graph.hypotheses.map(h => <button key={h.hypothesis_id} onClick={() => setSelected(h.hypothesis_id)}><span>{h.hypothesis_id}</span><StatusTag value={h.status} /></button>)}</div>
    <Modal title="假设层级与证据缺口" open={expanded} onCancel={() => setExpanded(false)} footer={null} width="94vw" styles={{ body: { height: '75vh' } }} destroyOnHidden>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} onNodeClick={(_, node) => { if (graph.hypotheses.some(h => h.hypothesis_id === node.id)) { setExpanded(false); setSelected(node.id); } }}><Background gap={20} /><Controls showInteractive={false} /></ReactFlow>
    </Modal>
    <Drawer title={hypothesis?.hypothesis_id || '故障假设'} open={!!hypothesis} onClose={() => setSelected(undefined)} size={580}>
      {hypothesis && <HypothesisDetail graph={graph} hypothesis={hypothesis} onEvidence={onEvidence} />}
    </Drawer>
  </Panel>;
}
