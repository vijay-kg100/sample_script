"""Builds NetworkX graphs from the domain model and exports them to
standalone interactive HTML via PyVis, for embedding in an iframe in the
Overview tab and the Mapping drill-down page.
"""
import os
import networkx as nx
from pyvis.network import Network

from app.models.domain import RepositoryModel
from app.analyzer.workflow_analyzer import compute_execution_order


def build_overview_graph(repo: RepositoryModel) -> nx.DiGraph:
    g = nx.DiGraph()
    wf = repo.workflow
    if wf is None:
        return g
    ti_by_name = {t.name: t for t in wf.task_instances}
    for ti in wf.task_instances:
        if ti.task_type == "Session":
            g.add_node(ti.name, label=ti.name, kind="Session")
        else:
            g.add_node(ti.name, label=f"{ti.name}\n[{ti.task_type}]", kind=ti.task_type)
    for link in wf.links:
        if link.from_task in ti_by_name and link.to_task in ti_by_name:
            g.add_edge(link.from_task, link.to_task, label=link.condition or "")
    return g


def _build_instance_graph(instances, connectors) -> nx.DiGraph:
    """Shared node/edge builder for a Mapping or Mapplet canvas -- both
    are just a list of Instances wired together by Connectors."""
    g = nx.DiGraph()
    for inst in instances:
        g.add_node(inst.name, label=inst.ref_name or inst.name, kind=inst.type)
    for c in connectors:
        if g.has_edge(c.from_instance, c.to_instance):
            continue
        g.add_edge(c.from_instance, c.to_instance)
    return g


def build_mapping_graph(repo: RepositoryModel, mapping_name: str) -> nx.DiGraph:
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return nx.DiGraph()
    return _build_instance_graph(mapping.instances, mapping.connectors)


def build_mapplet_graph(repo: RepositoryModel, mapplet_name: str) -> nx.DiGraph:
    """Same Source->Target lineage graph as build_mapping_graph, but for a
    Mapplet's own canvas (minor improvement-2: Mapplets get their own
    Visual Link in Table View, just like Mappings)."""
    mapplet = repo.mapplets.get(mapplet_name)
    if mapplet is None:
        return nx.DiGraph()
    return _build_instance_graph(mapplet.instances, mapplet.connectors)


_KIND_COLOR = {
    "Session": "#2E5395", "Start": "#7F7F7F", "Command": "#B58900",
    "SOURCE": "#2E8B57", "TARGET": "#B22222", "TRANSFORMATION": "#2E5395", "MAPPLET": "#8E44AD",
    # Reportability lineage graph (app/analyzer/reportability_analyzer.py) kinds:
    "TARGET_FINAL": "#B22222",              # the requested Table/Instance/Field itself
    "TARGET_INTERMEDIATE": "#A0522D",       # a previous session's Target Table used as a source further down
    "TRANSFORMATION_PASSTHROUGH": "#3C8DBC",  # direct pass-through, no business logic on this port
    "TRANSFORMATION_LOGIC": "#E67E22",        # real business logic (expression/condition) on this port
}

# Text color chosen per background so labels stay legible on every node color.
_KIND_FONT = {
    "Session": "#ffffff", "Start": "#ffffff", "Command": "#1a1a1a",
    "SOURCE": "#ffffff", "TARGET": "#ffffff", "TRANSFORMATION": "#ffffff", "MAPPLET": "#ffffff",
    "TARGET_FINAL": "#ffffff", "TARGET_INTERMEDIATE": "#ffffff",
    "TRANSFORMATION_PASSTHROUGH": "#ffffff", "TRANSFORMATION_LOGIC": "#ffffff",
}

# Edge color by lineage-hop classification (app/analyzer/reportability_analyzer.py).
# Edges built elsewhere never set "style", so they fall back to "flow" -- the same
# grey used before this map existed, i.e. fully backward compatible.
_EDGE_STYLE_COLOR = {
    "passthrough": "#3C8DBC",
    "logic": "#E67E22",
    "flow": "#8a94a6",
}


def _wrap_label(label: str, width: int = 16) -> str:
    """Inserts line breaks into long single-line labels so the node box can
    grow vertically instead of being squeezed/clipped horizontally."""
    parts = label.split("\n")
    wrapped_parts = []
    for part in parts:
        words = part.split("_") if "_" in part and " " not in part else part.split(" ")
        sep = "_" if "_" in part and " " not in part else " "
        line, lines = "", []
        for w in words:
            candidate = (line + sep + w) if line else w
            if len(candidate) > width and line:
                lines.append(line)
                line = w
            else:
                line = candidate
        if line:
            lines.append(line)
        wrapped_parts.append("\n".join(lines) if lines else part)
    return "\n".join(wrapped_parts)


def _compute_lineage_levels(g: nx.DiGraph) -> dict:
    """Longest-path level per node (Sugiyama-style): nodes with no
    predecessors (sources) sit at level 0; every other node's level is
    1 + the max level of its predecessors. Because targets are graph sinks
    with no outgoing edges, this naturally pushes them to the rightmost
    levels without hardcoding anything by node kind. Falls back to
    declaration order if the graph has a cycle (shouldn't happen for a
    real Informatica mapping, but keeps this from ever raising)."""
    try:
        topo = list(nx.topological_sort(g))
    except nx.NetworkXUnfeasible:
        topo = list(g.nodes())
    levels = {}
    for n in topo:
        preds = list(g.predecessors(n))
        levels[n] = 0 if not preds else 1 + max(levels.get(p, 0) for p in preds)
    return levels


def export_pyvis_html(g: nx.DiGraph, out_path: str, hierarchical: bool = True, directional: bool = False) -> str:
    """Renders an interactive graph.

    hierarchical=True, directional=False -> left-to-right execution-flow
        chain (Overview tab): fixed levels driven by vis-network's own
        directed sort, physics off.
    hierarchical=True, directional=True  -> source-to-target lineage graph
        (Mapping drill-down): levels are pinned explicitly from longest-path
        depth so SOURCE instances always land in the leftmost column and
        TARGET instances always land in the rightmost column reached by
        their own branch, with transformations naturally scattered in the
        columns between. A light hierarchical-repulsion physics pass only
        nudges nodes *within* their column so dense branches don't overlap.
    hierarchical=False -> generic force-directed scatter (fallback, no
        source/target ordering).

    All modes render nodes as auto-sizing boxes with wrapped, high-contrast
    labels so every node name stays fully visible instead of being clipped.
    """
    net = Network(height="750px", width="100%", directed=True, notebook=False, bgcolor="#ffffff")
    levels = _compute_lineage_levels(g) if directional else {}
    for n, data in g.nodes(data=True):
        kind = data.get("kind", "")
        label = _wrap_label(data.get("label", n))
        node_kwargs = dict(
            label=label,
            color={"background": _KIND_COLOR.get(kind, "#2E5395"), "border": "#13223f",
                   "highlight": {"background": "#4472C4", "border": "#13223f"}},
            title=f"{n} ({kind})" if kind else n,
            shape="box",
            shapeProperties={"borderRadius": 6},
            margin=10,
            widthConstraint={"minimum": 90, "maximum": 190},
            font={"size": 15, "color": _KIND_FONT.get(kind, "#ffffff"), "face": "arial", "multi": False,
                  "bold": {"size": 15}},
        )
        if directional:
            node_kwargs["level"] = levels.get(n, 0)
        net.add_node(n, **node_kwargs)
    for u, v, data in g.edges(data=True):
        label = data.get("label", "")
        edge_color = _EDGE_STYLE_COLOR.get(data.get("style", "flow"), _EDGE_STYLE_COLOR["flow"])
        net.add_edge(u, v, label=label, arrows="to", color={"color": edge_color, "highlight": "#2E5395"},
                     font={"size": 11, "color": "#555555", "strokeWidth": 3, "strokeColor": "#ffffff"},
                     smooth={"type": "cubicBezier", "roundness": 0.4})

    if hierarchical and directional:
        net.set_options("""
        {
          "layout": {
            "hierarchical": {
              "enabled": true, "direction": "LR", "sortMethod": "directed",
              "levelSeparation": 220, "nodeSpacing": 130, "treeSpacing": 170,
              "blockShifting": true, "edgeMinimization": true, "parentCentralization": false
            }
          },
          "physics": {
            "enabled": true,
            "solver": "hierarchicalRepulsion",
            "hierarchicalRepulsion": {"nodeDistance": 150, "centralGravity": 0,
                                       "springLength": 120, "springConstant": 0.02,
                                       "damping": 0.15, "avoidOverlap": 1},
            "stabilization": {"enabled": true, "iterations": 300, "fit": true}
          },
          "edges": {"smooth": {"type": "cubicBezier", "forceDirection": "horizontal", "roundness": 0.45}},
          "interaction": {"hover": true, "navigationButtons": true, "zoomView": true, "dragView": true, "dragNodes": true}
        }
        """)
    elif hierarchical:
        net.set_options("""
        {
          "layout": {
            "hierarchical": {
              "enabled": true, "direction": "LR", "sortMethod": "directed",
              "levelSeparation": 260, "nodeSpacing": 170, "treeSpacing": 220,
              "blockShifting": true, "edgeMinimization": true, "parentCentralization": true
            }
          },
          "physics": {"enabled": false},
          "edges": {"smooth": {"forceDirection": "horizontal"}},
          "interaction": {"hover": true, "navigationButtons": true, "zoomView": true, "dragView": true, "dragNodes": true}
        }
        """)
    else:
        net.set_options("""
        {
          "layout": {"randomSeed": 42, "improvedLayout": true},
          "physics": {
            "enabled": true,
            "solver": "forceAtlas2Based",
            "forceAtlas2Based": {"gravitationalConstant": -140, "centralGravity": 0.006,
                                  "springLength": 260, "springConstant": 0.035, "avoidOverlap": 1},
            "stabilization": {"enabled": true, "iterations": 400, "fit": true},
            "minVelocity": 0.75
          },
          "interaction": {"hover": true, "navigationButtons": true, "zoomView": true, "dragView": true, "dragNodes": true}
        }
        """)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    net.write_html(out_path, notebook=False, open_browser=False)
    _inject_click_bridge(out_path)
    return out_path


def _inject_click_bridge(out_path: str):
    """PyVis doesn't expose click events to the embedding page by default.
    This appends a small script that forwards vis-network node clicks to the
    parent window via postMessage, which base template's detail_panel.js
    listens for to open the right-side info panel (TDD Section 4.2.2)."""
    bridge = """
<script>
  (function () {
    function attach() {
      if (typeof network === "undefined" || !network) { return setTimeout(attach, 50); }
      network.on("click", function (params) {
        if (params.nodes && params.nodes.length > 0) {
          window.parent.postMessage({ type: "node-click", id: params.nodes[0] }, "*");
        }
      });
    }
    attach();
  })();
</script>
"""
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(bridge)


def build_flow_graph_from_dict(nodes: dict, edges: list) -> nx.DiGraph:
    """Converts the {node_id: {...}} / [{from, to, style}, ...] shape produced by
    app.analyzer.reportability_analyzer.build_attribute_lineage_graph into an
    nx.DiGraph ready for export_pyvis_html(..., directional=True), reusing the
    same directional/hierarchical layout and click-bridge as a Mapping graph.
    """
    g = nx.DiGraph()
    for node_id, data in nodes.items():
        kind = data.get("kind", "")
        if kind == "TRANSFORMATION":
            kind = "TRANSFORMATION_PASSTHROUGH" if data.get("passthrough") else "TRANSFORMATION_LOGIC"
        g.add_node(node_id, label=data.get("label", node_id), kind=kind)
    for e in edges:
        u, v = e.get("from"), e.get("to")
        if not u or not v or not g.has_node(u) or not g.has_node(v) or g.has_edge(u, v):
            continue
        g.add_edge(u, v, style=e.get("style", "flow"))
    return g


def build_flow_subgraph_from_dict(nodes: dict, edges: list, node_ids) -> nx.DiGraph:
    """Same node/edge shape as build_flow_graph_from_dict, but restricted to
    one Mapping's own components -- used to render each per-Mapping panel of
    a split Reportability lineage graph (see
    app.analyzer.reportability_analyzer.group_by_mapping). Edges are kept
    only when both endpoints are in `node_ids`; edges that cross out of this
    subset (into another Mapping's panel) are intentionally dropped here --
    those are summarized as "incoming"/"outgoing" badges in the template
    instead of being drawn inline, since the other endpoint doesn't exist in
    this panel's own graph.
    """
    allowed = set(node_ids)
    sub_nodes = {nid: data for nid, data in nodes.items() if nid in allowed}
    sub_edges = [e for e in edges if e.get("from") in allowed and e.get("to") in allowed]
    return build_flow_graph_from_dict(sub_nodes, sub_edges)


def graph_to_json(g: nx.DiGraph) -> dict:
    """JSON payload alternative to the PyVis HTML embed (used by /graph API)."""
    nodes = [{"id": n, "label": data.get("label", n), "kind": data.get("kind", "")} for n, data in g.nodes(data=True)]
    edges = [{"from": u, "to": v, "label": data.get("label", "")} for u, v, data in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges}
