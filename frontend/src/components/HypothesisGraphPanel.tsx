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
    <div className="node-statement">{data.statement}</div><Handle type="source" position={Position.Bottom} />
  </div>;
}
const nodeTypes = { hypothesis: HypothesisNode };

export function HypothesisDetail({ graph, hypothesis, onEvidence }: { graph: Graph; hypothesis: Hypothesis; onEvidence: (id: string) => void }) {
  const definition = graph.definitions.find(d => d.hypothesis_id === hypothesis.hypothesis_id)!;
  const gaps = graph.open_gaps.filter(g => g.hypothesis_ids.includes(hypothesis.hypothesis_id));
  return <><StatusTag value={hypothesis.status} /><h3>{definition.statement}</h3>
    <Descriptions column={1} size="small" items={[
      { key: 'kind', label: 'Kind', children: definition.kind },
      { key: 'reason', label: 'Reason', children: hypothesis.reason },
      { key: 'support', label: 'Supporting ref count', children: hypothesis.supporting_evidence_refs.length },
      { key: 'contradict', label: 'Contradicting ref count', children: hypothesis.contradicting_evidence_refs.length },
    ]} />
    <h3>Decisive evidence refs</h3><EvidenceRefs refs={hypothesis.decisive_evidence_refs} onSelect={onEvidence} />
    <h3>Supporting evidence refs</h3><EvidenceRefs refs={hypothesis.supporting_evidence_refs} onSelect={onEvidence} />
    <h3>Contradicting evidence refs</h3><EvidenceRefs refs={hypothesis.contradicting_evidence_refs} onSelect={onEvidence} />
    <h3>Open gaps</h3>{gaps.length ? gaps.map(g => <div className="gap" key={g.gap_id}><StatusTag value={g.priority_class} /><p>{g.question}</p><code>{g.gap_id}</code></div>) : <p className="muted">No related open gaps</p>}
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
    const edges: Edge[] = graph.definitions.filter(d => d.parent_hypothesis_id).map(d => ({ id: `parent-${d.hypothesis_id}`, source: d.parent_hypothesis_id!, target: d.hypothesis_id, markerEnd: { type: MarkerType.ArrowClosed }, label: 'specialization' }));
    graph.open_gaps.forEach((gap, i) => {
      nodes.push({ id: gap.gap_id, type: 'hypothesis', position: { x: 1240, y: i * 180 },
        data: { title: 'GAP', status: gap.priority_class, statement: gap.gap_id.split(':').at(-1) || gap.question } });
      gap.hypothesis_ids.forEach(h => edges.push({ id: `${gap.gap_id}-${h}`, source: h, target: gap.gap_id, markerEnd: { type: MarkerType.ArrowClosed }, label: 'requires evidence' }));
    });
    return { nodes, edges };
  }, [graph]);
  return <Panel title="Hypothesis Graph" extra={<Tag>RULE v{graph.rule_version}</Tag>}>
    <div className="graph-toolbar"><span className="muted">Hierarchy & evidence gaps</span>
      <Button size="small" onClick={() => setExpanded(true)}>Expand graph</Button>
      <Select aria-label="Inspect hypothesis" placeholder="Inspect hypothesis" value={selected} onChange={setSelected} options={graph.hypotheses.map(h => ({ value: h.hypothesis_id, label: `${h.hypothesis_id} · ${h.status}` }))} /></div>
    <div className="graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.25} maxZoom={1.6} nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} onNodeClick={(_, node) => setSelected(node.id)}><Background gap={20} /><Controls showInteractive={false} /></ReactFlow></div>
    <div className="graph-legend">{['CONFIRMED', 'SUPPORTED', 'POSSIBLE', 'UNKNOWN', 'ELIMINATED'].map(s => <StatusTag key={s} value={s} />)}</div>
    <div className="hypothesis-index">{graph.hypotheses.map(h => <button key={h.hypothesis_id} onClick={() => setSelected(h.hypothesis_id)}><span>{h.hypothesis_id}</span><StatusTag value={h.status} /></button>)}</div>
    <Modal title="Hypothesis hierarchy & evidence gaps" open={expanded} onCancel={() => setExpanded(false)} footer={null} width="94vw" styles={{ body: { height: '75vh' } }} destroyOnHidden>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false} edgesFocusable={false} onNodeClick={(_, node) => { if (graph.hypotheses.some(h => h.hypothesis_id === node.id)) { setExpanded(false); setSelected(node.id); } }}><Background gap={20} /><Controls showInteractive={false} /></ReactFlow>
    </Modal>
    <Drawer title={hypothesis?.hypothesis_id || 'Hypothesis'} open={!!hypothesis} onClose={() => setSelected(undefined)} size={580}>
      {hypothesis && <HypothesisDetail graph={graph} hypothesis={hypothesis} onEvidence={onEvidence} />}
    </Drawer>
  </Panel>;
}
