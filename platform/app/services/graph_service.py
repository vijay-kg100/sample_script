import os
import tempfile
from app.graph.graph_builder import (
    build_overview_graph, build_mapping_graph, build_mapplet_graph, build_flow_graph_from_dict,
    build_flow_subgraph_from_dict, export_pyvis_html, graph_to_json,
)

GRAPH_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "graphs")


def render_overview_graph(repo) -> str:
    g = build_overview_graph(repo)
    out = os.path.join(GRAPH_DIR, "overview.html")
    export_pyvis_html(g, out, hierarchical=True)
    return "graphs/overview.html"


def overview_graph_html(repo) -> str:
    """Raw markup of the rendered overview graph (same file served at
    render_overview_graph's path), for embedding elsewhere -- e.g. the
    standalone Visualization HTML export."""
    render_overview_graph(repo)
    out = os.path.join(GRAPH_DIR, "overview.html")
    with open(out, "r", encoding="utf-8") as f:
        return f.read()


def render_mapping_graph(repo, mapping_name: str) -> str:
    g = build_mapping_graph(repo, mapping_name)
    safe = "".join(c if c.isalnum() else "_" for c in mapping_name)
    out = os.path.join(GRAPH_DIR, f"mapping_{safe}.html")
    export_pyvis_html(g, out, hierarchical=True, directional=True)
    return f"graphs/mapping_{safe}.html"


def mapping_graph_html(repo, mapping_name: str) -> str:
    """Raw markup of a mapping's interactive Source->Target lineage graph,
    rendered on demand to a throwaway temp file and read back. Used by the
    standalone Visualization HTML export so every mapping's drill-down graph
    can be embedded up front, without depending on the in-app mapping detail
    page having been visited first (which is what populates static/graphs)."""
    g = build_mapping_graph(repo, mapping_name)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "mapping.html")
        export_pyvis_html(g, out, hierarchical=True, directional=True)
        with open(out, "r", encoding="utf-8") as f:
            return f.read()


def overview_graph_json(repo) -> dict:
    return graph_to_json(build_overview_graph(repo))


def mapping_graph_json(repo, mapping_name: str) -> dict:
    return graph_to_json(build_mapping_graph(repo, mapping_name))


def render_mapplet_graph(repo, mapplet_name: str) -> str:
    g = build_mapplet_graph(repo, mapplet_name)
    safe = "".join(c if c.isalnum() else "_" for c in mapplet_name)
    out = os.path.join(GRAPH_DIR, f"mapplet_{safe}.html")
    export_pyvis_html(g, out, hierarchical=True, directional=True)
    return f"graphs/mapplet_{safe}.html"


def mapplet_graph_html(repo, mapplet_name: str) -> str:
    """Raw markup of a mapplet's interactive Source->Target lineage graph,
    rendered on demand -- same purpose as mapping_graph_html, used to embed
    every mapplet's drill-down graph in the standalone Visualization HTML
    export."""
    g = build_mapplet_graph(repo, mapplet_name)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "mapplet.html")
        export_pyvis_html(g, out, hierarchical=True, directional=True)
        with open(out, "r", encoding="utf-8") as f:
            return f.read()


def mapplet_graph_json(repo, mapplet_name: str) -> dict:
    return graph_to_json(build_mapplet_graph(repo, mapplet_name))


def render_reportability_graph(graph_data: dict) -> str:
    """Renders one Reportability attribute-lineage graph (see
    app.analyzer.reportability_analyzer.build_attribute_lineage_graph) to a
    standalone interactive HTML file, same directional/hierarchical layout
    and click-bridge as a Mapping drill-down graph."""
    g = build_flow_graph_from_dict(graph_data["nodes"], graph_data["edges"])
    safe = "".join(
        c if c.isalnum() else "_"
        for c in f"{graph_data['final_mapping']}_{graph_data['final_instance']}_{graph_data['final_field']}"
    )
    out = os.path.join(GRAPH_DIR, f"reportability_{safe}.html")
    export_pyvis_html(g, out, hierarchical=True, directional=True)
    return f"graphs/reportability_{safe}.html"


def render_reportability_graph_by_mapping(graph_data: dict, grouping: dict) -> dict:
    """Renders one small PyVis HTML per Mapping panel (see
    app.analyzer.reportability_analyzer.group_by_mapping) instead of a
    single graph mixing every Mapping's components together, so the
    Reportability page can show a split, Mapping-wise screen with each
    Mapping's own components laid out separately. Returns
    {mapping_name: "graphs/..." relative path, ...} in the same order as
    grouping["order"].
    """
    safe_base = "".join(
        c if c.isalnum() else "_"
        for c in f"{graph_data['final_mapping']}_{graph_data['final_instance']}_{graph_data['final_field']}"
    )
    paths = {}
    for mapping_name in grouping["order"]:
        node_ids = grouping["panels"][mapping_name]["node_ids"]
        g = build_flow_subgraph_from_dict(graph_data["nodes"], graph_data["edges"], node_ids)
        safe_mapping = "".join(c if c.isalnum() else "_" for c in mapping_name)
        out = os.path.join(GRAPH_DIR, f"reportability_{safe_base}__{safe_mapping}.html")
        export_pyvis_html(g, out, hierarchical=True, directional=True)
        paths[mapping_name] = f"graphs/reportability_{safe_base}__{safe_mapping}.html"
    return paths
