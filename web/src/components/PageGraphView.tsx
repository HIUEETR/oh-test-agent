import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { RunTrace } from "../types";

type PageGraphViewProps = {
  runId: string;
  graph: RunTrace["graph"];
  artifactUrl: (absolutePath: string) => string;
};

type GraphNode = RunTrace["graph"]["nodes"][number];
type GraphEdge = RunTrace["graph"]["edges"][number];

type PageNodeData = {
  page: GraphNode;
  imageUrl: string;
  selected: boolean;
};

type PageFlowNode = Node<PageNodeData, "page">;

const NODE_WIDTH = 268;
const NODE_HEIGHT = 238;
const COLUMN_GAP = 92;
const ROW_GAP = 86;

const styles: Record<string, CSSProperties> = {
  root: {
    position: "relative",
    width: "100%",
    height: "100%",
    minHeight: 560,
    overflow: "hidden",
    background: "linear-gradient(145deg, rgba(18, 36, 58, 0.94), rgba(12, 26, 44, 0.94))",
  },
  empty: {
    height: "100%",
    minHeight: 560,
    display: "grid",
    placeItems: "center",
    color: "#8fa7bf",
    fontSize: 14,
  },
  node: {
    position: "relative",
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    overflow: "hidden",
    borderRadius: 14,
    border: "1px solid #345878",
    background: "linear-gradient(145deg, #17304d, #10243b)",
    boxShadow: "0 14px 34px rgba(0, 0, 0, 0.26)",
    color: "#e7f0f8",
    transition: "border-color 160ms, box-shadow 160ms, transform 160ms",
  },
  nodeSelected: {
    borderColor: "#35d6a4",
    boxShadow: "0 0 0 2px rgba(53, 214, 164, 0.3), 0 18px 42px rgba(0, 0, 0, 0.36)",
  },
  thumbnailFrame: {
    width: "100%",
    height: 142,
    overflow: "hidden",
    background: "#07121f",
    borderBottom: "1px solid #294866",
  },
  thumbnail: { width: "100%", height: "100%", display: "block", objectFit: "cover" },
  imageFallback: {
    width: "100%",
    height: "100%",
    display: "grid",
    placeItems: "center",
    color: "#7890a8",
    background: "linear-gradient(135deg, #0a1726, #122a42)",
    fontSize: 12,
  },
  nodeBody: { display: "flex", flexDirection: "column", gap: 6, padding: "11px 13px" },
  state: {
    color: "#35d6a4",
    fontFamily: '"Cascadia Code", Consolas, monospace',
    fontSize: 9,
    letterSpacing: ".1em",
  },
  title: {
    overflow: "hidden",
    color: "#e7f0f8",
    fontSize: 13,
    fontWeight: 650,
    lineHeight: 1.25,
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  count: { color: "#8da4ba", fontSize: 11 },
  panel: {
    position: "absolute",
    zIndex: 8,
    top: 16,
    right: 16,
    width: "min(360px, calc(100% - 32px))",
    maxHeight: "calc(100% - 32px)",
    overflowY: "auto",
    border: "1px solid #345878",
    borderRadius: 16,
    background: "rgba(7, 18, 31, 0.96)",
    boxShadow: "0 22px 54px rgba(0, 0, 0, 0.42)",
    color: "#dce8f4",
    backdropFilter: "blur(12px)",
  },
  panelImageFrame: {
    height: 236,
    overflow: "hidden",
    borderBottom: "1px solid #294866",
    background: "#050d17",
  },
  panelImage: { width: "100%", height: "100%", display: "block", objectFit: "contain" },
  panelBody: { padding: 16 },
  panelTitle: { margin: "0 0 12px", color: "#f2f7fb", fontSize: 16, lineHeight: 1.35 },
  facts: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 14 },
  fact: {
    padding: "9px 10px",
    border: "1px solid #263f59",
    borderRadius: 10,
    background: "rgba(18, 42, 67, 0.56)",
  },
  factLabel: { display: "block", marginBottom: 4, color: "#7890a8", fontSize: 10 },
  factValue: { color: "#dce8f4", fontSize: 12 },
  path: {
    margin: "0 0 14px",
    padding: "10px 11px",
    overflowWrap: "anywhere",
    border: "1px solid #263f59",
    borderRadius: 10,
    color: "#b8c9d9",
    background: "rgba(5, 14, 25, 0.62)",
    fontFamily: '"Cascadia Code", Consolas, monospace',
    fontSize: 11,
    lineHeight: 1.5,
  },
  edgeSection: { marginTop: 12 },
  edgeHeading: { margin: "0 0 7px", color: "#8fa7bf", fontSize: 11, fontWeight: 650 },
  actionList: { display: "flex", flexDirection: "column", gap: 6 },
  action: {
    padding: "8px 10px",
    borderLeft: "2px solid #35d6a4",
    borderRadius: "0 8px 8px 0",
    color: "#c8d7e5",
    background: "rgba(53, 214, 164, 0.08)",
    fontSize: 11,
    lineHeight: 1.4,
  },
  noAction: { color: "#667d94", fontSize: 11 },  closeButton: {
    position: "absolute",
    zIndex: 2,
    top: 10,
    right: 10,
    minHeight: 34,
    padding: "0 11px",
    border: "1px solid #456682",
    borderRadius: 9,
    color: "#eff8ff",
    background: "rgba(7, 18, 31, 0.9)",
    cursor: "pointer",
  },
};

function ImageWithFallback({ src, alt, large = false }: { src: string; alt: string; large?: boolean }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [src]);

  if (!src || failed) {
    return <div style={styles.imageFallback}>图片加载失败</div>;
  }

  return (
    <img
      src={src}
      alt={alt}
      draggable={false}
      style={large ? styles.panelImage : styles.thumbnail}
      onError={() => setFailed(true)}
    />
  );
}

function PageNode({ data }: NodeProps<PageFlowNode>) {
  return (
    <div style={{ ...styles.node, ...(data.selected ? styles.nodeSelected : {}) }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={styles.thumbnailFrame}>
        <ImageWithFallback src={data.imageUrl} alt={`${data.page.title} 页面截图`} />
      </div>
      <div style={styles.nodeBody}>
        <span style={styles.state}>STATE {data.page.discovered_order}</span>
        <strong title={data.page.title} style={styles.title}>{data.page.title}</strong>
        <span style={styles.count}>{data.page.element_count} 个元素</span>
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { page: PageNode };

function initialPosition(index: number) {
  return {
    x: (index % 3) * (NODE_WIDTH + COLUMN_GAP),
    y: Math.floor(index / 3) * (NODE_HEIGHT + ROW_GAP),
  };
}

function mergeNodes(
  graphNodes: GraphNode[],
  currentNodes: PageFlowNode[],
  selectedId: string | null,
  artifactUrl: PageGraphViewProps["artifactUrl"],
): PageFlowNode[] {
  const previousById = new Map(currentNodes.map((node) => [node.id, node]));
  const merged = graphNodes.map((page, index) => {
    const previous = previousById.get(page.node_id);
    const imageUrl = page.image_path ? artifactUrl(page.image_path) : "";
    const selected = page.node_id === selectedId;
    if (
      previous
      && previous.selected === selected
      && previous.data.selected === selected
      && previous.data.imageUrl === imageUrl
      && previous.data.page.snapshot_id === page.snapshot_id
      && previous.data.page.title === page.title
      && previous.data.page.page_path === page.page_path
      && previous.data.page.image_path === page.image_path
      && previous.data.page.element_count === page.element_count
      && previous.data.page.discovered_order === page.discovered_order
    ) {
      return previous;
    }
    return {
      id: page.node_id,
      type: "page" as const,
      position: previous?.position ?? initialPosition(index),
      width: previous?.width,
      height: previous?.height,
      selected,
      ariaLabel: `${page.title}，STATE ${page.discovered_order}，${page.element_count} 个元素`,
      data: { page, imageUrl, selected },
    };
  });
  return merged.length === currentNodes.length && merged.every((node, index) => node === currentNodes[index])
    ? currentNodes
    : merged;
}
function actionText(edge: GraphEdge, direction: "in" | "out", nodesById: Map<string, GraphNode>) {
  const otherId = direction === "in" ? edge.source : edge.target;
  const other = nodesById.get(otherId);
  const relation = direction === "in" ? "来自" : "前往";
  const action = edge.action || "未命名动作";
  const description = edge.target_description ? ` · ${edge.target_description}` : "";
  return `${relation} ${other?.title ?? otherId}：${action}${description}`;
}

export default function PageGraphView({ runId, graph, artifactUrl }: PageGraphViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<PageFlowNode>([]);
  const instanceRef = useRef<ReactFlowInstance<PageFlowNode, Edge> | null>(null);
  const fittedRunRef = useRef<string | null>(null);
  const previousNodeCountRef = useRef(0);
  const previousRunIdRef = useRef(runId);
  const latestNodeCountRef = useRef(graph.nodes.length);

  useEffect(() => {
    latestNodeCountRef.current = graph.nodes.length;
  }, [graph.nodes.length]);

  useEffect(() => {
    setSelectedId((current) => current && graph.nodes.some((node) => node.node_id === current) ? current : null);
  }, [graph.nodes]);

  useEffect(() => {
    setNodes((current) => mergeNodes(graph.nodes, current, selectedId, artifactUrl));
  }, [artifactUrl, graph.nodes, selectedId, setNodes]);

  useEffect(() => {
    if (previousRunIdRef.current !== runId) {
      previousRunIdRef.current = runId;
      fittedRunRef.current = null;
      previousNodeCountRef.current = graph.nodes.length;
      if (instanceRef.current) {
        fittedRunRef.current = runId;
        requestAnimationFrame(() => {
          void instanceRef.current?.fitView({ padding: 0.16 });
        });
      }
      return;
    }

    const previousCount = previousNodeCountRef.current;
    const nextCount = graph.nodes.length;
    previousNodeCountRef.current = nextCount;
    if (instanceRef.current && nextCount > previousCount && previousCount > 0) {
      requestAnimationFrame(() => {
        void instanceRef.current?.fitView({ padding: 0.16, duration: 300 });
      });
    }
  }, [graph.nodes.length, runId]);

  const nodesById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.node_id, node])),
    [graph.nodes],
  );
  const selectedPage = selectedId ? nodesById.get(selectedId) ?? null : null;
  const incoming = useMemo(
    () => selectedId ? graph.edges.filter((edge) => edge.target === selectedId) : [],
    [graph.edges, selectedId],
  );
  const outgoing = useMemo(
    () => selectedId ? graph.edges.filter((edge) => edge.source === selectedId) : [],
    [graph.edges, selectedId],
  );

  const edges = useMemo<Edge[]>(() => graph.edges.map((edge) => {
    const connected = selectedId !== null && (edge.source === selectedId || edge.target === selectedId);
    return {
      id: edge.edge_id,
      source: edge.source,
      target: edge.target,
      label: edge.target_description ? `${edge.action}: ${edge.target_description}` : edge.action,
      animated: connected,
      zIndex: connected ? 2 : 0,
      style: {
        stroke: connected ? "#35d6a4" : "#41617e",
        strokeWidth: connected ? 3 : 1.5,
        opacity: selectedId && !connected ? 0.28 : 0.9,
      },
      labelStyle: {
        fill: connected ? "#bdf8e5" : "#91a8bd",
        fontSize: 11,
        fontWeight: connected ? 650 : 400,
      },
      labelBgStyle: { fill: "#0d2033", fillOpacity: 0.88 },
      labelBgPadding: [5, 3],
      labelBgBorderRadius: 5,
    };
  }), [graph.edges, selectedId]);

  const handleInit = useCallback((instance: ReactFlowInstance<PageFlowNode, Edge>) => {
    instanceRef.current = instance;
    if (fittedRunRef.current !== runId) {
      fittedRunRef.current = runId;
      previousNodeCountRef.current = latestNodeCountRef.current;
      requestAnimationFrame(() => {
        void instance.fitView({ padding: 0.16 });
      });
    }
  }, [runId]);

  if (!graph.nodes.length) {
    return <div style={styles.empty}>页面图尚未生成</div>;
  }

  return (
    <section style={styles.root} aria-label="页面状态图">
      <ReactFlow<PageFlowNode, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onNodeClick={(_, node) => setSelectedId(node.id)}
        onPaneClick={() => setSelectedId(null)}
        onInit={handleInit}
        minZoom={0.25}
        maxZoom={1.8}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        panOnDrag
        zoomOnScroll
        zoomOnPinch
        zoomOnDoubleClick
      >
        <Background color="#253957" gap={22} />
        <Controls position="bottom-right" />
      </ReactFlow>

      {selectedPage && (
        <aside className="page-graph-detail" style={styles.panel} role="region" aria-live="polite" aria-label={`${selectedPage.title} 详情`}>
          <button type="button" style={styles.closeButton} aria-label="关闭页面详情" onClick={() => setSelectedId(null)}>关闭</button>
          <div className="page-graph-detail-image" style={styles.panelImageFrame}>
            <ImageWithFallback
              src={selectedPage.image_path ? artifactUrl(selectedPage.image_path) : ""}
              alt={`${selectedPage.title} 页面大图`}
              large
            />
          </div>
          <div style={styles.panelBody}>
            <h3 style={styles.panelTitle}>{selectedPage.title}</h3>
            <div style={styles.facts}>
              <div style={styles.fact}>
                <span style={styles.factLabel}>发现顺序</span>
                <span style={styles.factValue}>STATE {selectedPage.discovered_order}</span>
              </div>
              <div style={styles.fact}>
                <span style={styles.factLabel}>元素数量</span>
                <span style={styles.factValue}>{selectedPage.element_count}</span>
              </div>
            </div>
            <p style={styles.path}>{selectedPage.page_path || "未记录页面路径"}</p>

            <div style={styles.edgeSection}>
              <h4 style={styles.edgeHeading}>入边动作（{incoming.length}）</h4>
              <div style={styles.actionList}>
                {incoming.length
                  ? incoming.map((edge) => <div key={edge.edge_id} style={styles.action}>{actionText(edge, "in", nodesById)}</div>)
                  : <span style={styles.noAction}>无入边动作</span>}
              </div>
            </div>

            <div style={styles.edgeSection}>
              <h4 style={styles.edgeHeading}>出边动作（{outgoing.length}）</h4>
              <div style={styles.actionList}>
                {outgoing.length
                  ? outgoing.map((edge) => <div key={edge.edge_id} style={styles.action}>{actionText(edge, "out", nodesById)}</div>)
                  : <span style={styles.noAction}>无出边动作</span>}
              </div>
            </div>
          </div>
        </aside>
      )}
    </section>
  );
}

