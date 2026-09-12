// 页面关系图：React Flow 画布 + 自定义玻璃节点，展示探索发现的页面状态与跳转边。
// 选中节点时下方给出大图与出入边明细；画布在节点数量增长时自动取景。

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background, BackgroundVariant, Controls, Handle, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { GitBranch } from "lucide-react";
import { EmptyState } from "../../components/ui/primitives";
import { artifactUrl } from "../../utils/artifact";
import type { RunTrace } from "../../api/types";

type PageGraph = RunTrace["graph"];

type PageNodeData = {
  label: string;
  order: number;
  elementCount: number;
  imageSrc: string;
  pagePath: string;
};

function PageNode({ data, selected }: NodeProps<Node<PageNodeData>>) {
  return (
    <div className={`graph-node${selected ? " selected" : ""}`}>
      {data.imageSrc
        ? <img src={data.imageSrc} alt="" loading="lazy" />
        : <div style={{ height: 92, background: "#0e1620" }} />}
      <div className="graph-node-body">
        <span className="graph-node-order">STATE {data.order}</span>
        <strong title={data.label}>{data.label}</strong>
        <small>{data.elementCount} 元素{data.pagePath ? ` · ${data.pagePath}` : ""}</small>
      </div>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} isConnectable={false} />
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { page: PageNode };

/** 简单三列网格布局：按发现顺序排布（探索图本身即近似广度优先）。 */
function layout(graph: PageGraph, runId: string): Node<Node<PageNodeData>["data"]>[] {
  return graph.nodes.map((node) => {
    const column = node.discovered_order % 3;
    const row = Math.floor(node.discovered_order / 3);
    return {
      id: node.node_id,
      type: "page",
      position: { x: column * 250 + 24, y: row * 240 + 24 },
      data: {
        label: node.title || node.page_path || node.node_id,
        order: node.discovered_order,
        elementCount: node.element_count,
        imageSrc: artifactUrl(runId, node.artifact_path || node.image_path || ""),
        pagePath: node.page_path,
      },
    } satisfies Node<Node<PageNodeData>["data"]>;
  });
}

function toEdges(graph: PageGraph, selected: string | null): Edge[] {
  return graph.edges.map((edge) => ({
    id: edge.edge_id,
    source: edge.source,
    target: edge.target,
    label: edge.target_description.length > 26 ? `${edge.target_description.slice(0, 26)}…` : edge.target_description,
    animated: selected !== null && (edge.source === selected || edge.target === selected),
    style: selected !== null && (edge.source === selected || edge.target === selected)
      ? { stroke: "#0a59f7", strokeWidth: 2 }
      : { stroke: "#9db1c6" },
    labelStyle: { fill: "#46586c", fontSize: 10 },
    labelBgStyle: { fill: "rgba(255,255,255,0.9)" },
  }));
}

export function PageGraphView({ runId, graph }: { runId: string; graph: PageGraph }) {
  const [selected, setSelected] = useState<string | null>(null);
  const nodes = useMemo(() => layout(graph, runId), [graph, runId]);
  const edges = useMemo(() => toEdges(graph, selected), [graph, selected]);

  // 新运行或节点增多时重新取景，保证最新页面可见。
  useEffect(() => {
    setSelected(null);
  }, [runId]);

  const selectedNode = graph.nodes.find((node) => node.node_id === selected) ?? null;
  const incoming = graph.edges.filter((edge) => edge.target === selected);
  const outgoing = graph.edges.filter((edge) => edge.source === selected);

  const onNodeClick = useCallback((_: unknown, node: Node) => {
    setSelected((current) => (current === node.id ? null : node.id));
  }, []);

  if (graph.nodes.length === 0) {
    return <EmptyState title="页面图尚未生成" hint="探索运行发现页面状态后会自动绘制" icon={<GitBranch size={38} />} />;
  }

  return (
    <>
      <div className="graph-canvas">
        <ReactFlow
          key={runId}
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.2}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} color="#b9c8da" />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      {selectedNode && (
        <div className="graph-detail">
          <img src={artifactUrl(runId, selectedNode.artifact_path || selectedNode.image_path || "")} alt="选中页面大图" />
          <div>
            <h4>{selectedNode.title || selectedNode.page_path}</h4>
            <small>发现顺序 {selectedNode.discovered_order} · {selectedNode.element_count} 元素 · {selectedNode.page_path}</small>
            <h4 style={{ marginTop: 10 }}>进入动作</h4>
            <ul>{incoming.length ? incoming.map((edge) => <li key={edge.edge_id}>{edge.target_description}</li>) : <li>（入口页面）</li>}</ul>
            <h4 style={{ marginTop: 10 }}>离开动作</h4>
            <ul>{outgoing.length ? outgoing.map((edge) => <li key={edge.edge_id}>{edge.target_description}</li>) : <li>（暂无）</li>}</ul>
          </div>
        </div>
      )}
    </>
  );
}
