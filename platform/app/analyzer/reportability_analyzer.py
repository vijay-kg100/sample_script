"""Reportability lineage-graph engine.

Powers the "Reportability" download option under Informatica XML Downloads:
given a Mapping, a Target Table, and (when the table appears more than once)
a Transformation Instance, list every attribute on that table; for any one
selected attribute, build a single directed lineage graph tracing it from its
first primary Source (the earliest Mapping/Session in the chain) all the way
to the requested Mapping/Table/Instance -- crossing session boundaries via the
same "previous session's Target == this session's Source" rule used by the
Target Field Lineage feature.

Only nodes/edges actually reached by real CONNECTOR wiring are emitted, so
unconnected hops and unused transformations never appear in the graph.

Node kinds emitted (consumed by app.graph.graph_builder.build_flow_graph_from_dict
and by app/static/js/detail_panel.js's "reportability" panel mode):
  SOURCE                -- an original Source Qualifier-fed source field
  TARGET_INTERMEDIATE   -- a previous session's Target Table, itself feeding a
                            later session as a Source (i.e. a mid-chain hop)
  TARGET_FINAL          -- the exact Table/Instance/Field the user asked about
  TRANSFORMATION        -- any ordinary transformation port (Expression,
                            Lookup, Router, Aggregator, ...); the graph
                            exporter recolors this to TRANSFORMATION_PASSTHROUGH
                            or TRANSFORMATION_LOGIC based on the "passthrough"
                            flag below
  MAPPLET                -- a Mapplet boundary port
"""
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from app.analyzer import field_lineage_analyzer as fla

MAX_HOPS = fla.MAX_HOPS


def find_matching_target_instances(repo, mapping_name: str, table_name: str):
    """All TARGET instances in `mapping_name` whose underlying table matches
    `table_name` (case-insensitive). Thin, named wrapper around the same
    lookup used by Target Field Lineage, kept here so callers of this module
    don't need to reach into field_lineage_analyzer directly."""
    return fla.find_target_instances(repo, mapping_name, table_name)


def list_attributes(repo, mapping_name: str, instance_name: str) -> List[str]:
    """Every field defined on the Target Table behind this instance that has
    an actual inbound CONNECTOR wired to it in this mapping. Fields with no
    connection at all (unused columns on the target table) are left out, per
    the Reportability listing rule -- there's nothing to trace for them."""
    all_fields = fla.target_fields_for_instance(repo, mapping_name, instance_name)
    ctx = fla._build_ctx(repo, "mapping", mapping_name)
    if ctx is None:
        return []
    return [f for f in all_fields if ctx.pred_index.get((instance_name, f))]


def _find_cross_session_producer(repo, table_name: str, field_name: str, exclude_mapping: str,
                                  cross_visited: set) -> Optional[Tuple[str, Optional[str], str, str]]:
    """Best-effort resolution of the 'previous session' rule: is `table_name`
    (used here as a Source) actually a Target Table of some *other* mapping
    in this upload? If so, returns (upstream_mapping, upstream_session,
    upstream_target_instance_name, upstream_field_name) for the first match,
    searching mappings in workflow execution order (falling back to
    declaration order) so the earliest-loaded producer wins deterministically.
    Guarded against infinite loops via `cross_visited`.
    """
    key = (exclude_mapping, table_name, field_name.lower())
    if key in cross_visited:
        return None
    cross_visited.add(key)

    wf = repo.workflow
    ordered_mappings: List[str] = []
    seen = set()
    if wf is not None:
        order = wf.execution_order or []
        ordered_sessions = [t for t in order if t in wf.sessions] or list(wf.sessions.keys())
        for sname in ordered_sessions:
            mname = wf.sessions[sname].mapping_name
            if mname in repo.mappings and mname not in seen:
                ordered_mappings.append(mname)
                seen.add(mname)
    for mname in repo.mappings:
        if mname not in seen:
            ordered_mappings.append(mname)
            seen.add(mname)

    for mname in ordered_mappings:
        if mname == exclude_mapping:
            continue
        mapping = repo.mappings[mname]
        for inst in mapping.instances:
            if inst.type != "TARGET" or inst.ref_name != table_name:
                continue
            table = repo.targets.get(inst.ref_name)
            if table is None:
                continue
            match = next((f.name for f in table.fields if f.name.lower() == field_name.lower()), None)
            if match is None:
                continue
            upstream_session = fla.session_for_mapping(repo, mname)
            return mname, upstream_session, inst.name, match
    return None


def _break_cycles(nodes: Dict[str, Dict], edges: List[Dict]) -> List[str]:
    """Cross-session matching infers 'previous session' links purely from a
    Source Table name equalling some other mapping's Target Table name. When
    the same physical staging table is written AND read by more than one
    mapping (a common shared-staging-table pattern), that heuristic can
    legitimately chain back into a cycle across mapping boundaries. A
    lineage diagram has to render as a DAG regardless, so this runs a
    standard DFS back-edge pass over the finished node/edge set: any edge
    that would close a cycle back to a node already on the current path is
    dropped. Returns the dropped edges (as "A -> B" node-id pairs) so the
    caller can surface a transparency note instead of silently guessing."""
    adjacency: Dict[str, List[int]] = defaultdict(list)
    for idx, e in enumerate(edges):
        adjacency[e["from"]].append(idx)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    keep = [True] * len(edges)
    dropped: List[str] = []

    def dfs(u: str):
        color[u] = GRAY
        for idx in adjacency.get(u, []):
            if not keep[idx]:
                continue
            v = edges[idx]["to"]
            if color.get(v) == GRAY:
                keep[idx] = False
                dropped.append(f"{u} -> {v}")
                continue
            if color.get(v) == WHITE:
                dfs(v)
        color[u] = BLACK

    for nid in list(nodes.keys()):
        if color.get(nid) == WHITE:
            dfs(nid)

    edges[:] = [e for e, k in zip(edges, keep) if k]
    return dropped


def _node_levels(nodes: Dict[str, Dict], edges: List[Dict]) -> Dict[str, int]:
    """Longest-path level per node (source nodes at 0, everything else 1 +
    the max level of its predecessors) computed directly over the plain
    node/edge dicts -- same idea as the PyVis exporter's own level pass, but
    kept dependency-free here so the analyzer layer doesn't need networkx
    just to work out mapping panel order. Guarded against cycles (a cross-
    session shared-staging-table pattern can produce mapping-level cycles
    even after _break_cycles removes node-level ones) via a recursion-stack
    check that treats a back-edge as depth 0 rather than recursing forever.
    """
    preds: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        preds[e["to"]].append(e["from"])

    memo: Dict[str, int] = {}
    visiting: set = set()

    def level(n: str) -> int:
        if n in memo:
            return memo[n]
        if n in visiting:
            return 0
        visiting.add(n)
        ups = [p for p in preds.get(n, []) if p in nodes]
        result = 0 if not ups else 1 + max(level(p) for p in ups)
        visiting.discard(n)
        memo[n] = result
        return result

    for nid in nodes:
        level(nid)
    return memo


def group_by_mapping(repo, graph_data: Dict) -> Dict:
    """Splits one Reportability attribute-lineage graph into per-Mapping
    panels (improvement: 'split the screen mapping wise, place the
    components in the respective mappings' instead of one graph with every
    Mapping's components mixed together in a single view).

    Returns:
      {
        "order": [mapping_name, ...],   # Session execution order, with the
                                          # Mapping the user asked about
                                          # forced onto the last tab
        "panels": {
          mapping_name: {
            "node_ids": [...],           # this Mapping's own components
            "node_count": int,
            "incoming": [{"mapping": other, "count": n}, ...],  # cross-
                         # Mapping edges flowing IN from an upstream Mapping
            "outgoing": [{"mapping": other, "count": n}, ...],  # cross-
                         # Mapping edges flowing OUT to a downstream Mapping
          }, ...
        },
      }

    Mapping order follows the Workflow's actual Session execution order
    (repo.workflow.execution_order) rather than the graph's longest-path
    level, since two Mappings that both feed the chain at "level 0" (e.g.
    two independent staging loads) can still run in a specific, meaningful
    sequence that the level number alone doesn't capture. Whatever Mapping
    the user actually asked about (graph_data["final_mapping"]) is always
    pinned to the last tab, since that's the Mapping/Table/Instance the
    whole lineage was built to explain -- even if, for some reason, its
    session were to run earlier than an upstream one. Any Mapping with no
    resolvable session (or no workflow at all) falls back to its longest-
    path level so the ordering degrades gracefully instead of breaking.
    """
    nodes, edges = graph_data["nodes"], graph_data["edges"]
    levels = _node_levels(nodes, edges)

    node_ids_by_mapping: Dict[str, List[str]] = {}
    for nid, n in nodes.items():
        node_ids_by_mapping.setdefault(n["mapping"], []).append(nid)

    min_level = {m: min(levels.get(nid, 0) for nid in ids) for m, ids in node_ids_by_mapping.items()}

    wf = repo.workflow if hasattr(repo, "workflow") else None
    session_rank: Dict[str, int] = {}
    if wf is not None:
        exec_order = wf.execution_order or []
        ordered_session_names = [t for t in exec_order if t in wf.sessions] or list(wf.sessions.keys())
        for rank, sname in enumerate(ordered_session_names):
            mname = wf.sessions[sname].mapping_name
            if mname not in session_rank:  # first (earliest) session wins if a mapping is reused
                session_rank[mname] = rank

    final_mapping = graph_data.get("final_mapping")
    no_session_rank = len(session_rank) + len(node_ids_by_mapping) + 1  # sorts after every real session

    def sort_key(m: str):
        is_final = (m == final_mapping)
        rank = session_rank.get(m, no_session_rank + min_level[m])
        # The user-requested Mapping always lands on the last tab, regardless
        # of where its own session falls in the execution order.
        return (1 if is_final else 0, rank, m)

    order = sorted(node_ids_by_mapping.keys(), key=sort_key)

    cross_in: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cross_out: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in edges:
        from_mapping = nodes[e["from"]]["mapping"]
        to_mapping = nodes[e["to"]]["mapping"]
        if from_mapping != to_mapping:
            cross_out[from_mapping][to_mapping] += 1
            cross_in[to_mapping][from_mapping] += 1

    panels = {}
    for m in order:
        panels[m] = {
            "node_ids": node_ids_by_mapping[m],
            "node_count": len(node_ids_by_mapping[m]),
            "incoming": [{"mapping": om, "count": c} for om, c in cross_in.get(m, {}).items()],
            "outgoing": [{"mapping": om, "count": c} for om, c in cross_out.get(m, {}).items()],
        }

    return {"order": order, "panels": panels}


def build_attribute_lineage_graph(repo, mapping_name: str, session_name: Optional[str],
                                   target_instance_name: str, target_field_name: str) -> Optional[Dict]:
    """Builds the full multi-session lineage graph for one attribute.

    Returns None if the Mapping/Instance/Field cannot be resolved, otherwise:
      {
        "nodes": {node_id: {...}},
        "edges": [{"from": node_id, "to": node_id, "style": "passthrough"|"logic"|"flow"}, ...],
        "final_mapping": str, "final_session": str|None,
        "final_target_table": str, "final_instance": str, "final_field": str,
      }
    """
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return None
    target_inst = next((i for i in mapping.instances
                         if i.name == target_instance_name and i.type == "TARGET"), None)
    if target_inst is None:
        return None
    if not any(f == target_field_name for f in
               fla.target_fields_for_instance(repo, mapping_name, target_instance_name)):
        return None

    final_mapping = mapping_name
    final_session = session_name or fla.session_for_mapping(repo, mapping_name)
    final_target_table = target_inst.ref_name
    final_target_id = f"mapping:{mapping_name}::{target_instance_name}::{target_field_name}"

    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    in_progress: set = set()
    cross_visited: set = set()

    def add_edge(from_id: Optional[str], to_id: str, style: str):
        if from_id is None:
            return
        edges.append({"from": from_id, "to": to_id, "style": style})

    def process(ctx: fla._Ctx, owning_mapping: str, owning_session: Optional[str],
                instance_name: str, field_name: str, hop: int,
                feeding_into_instance: Optional[str],
                mapplet_stack: List[Tuple["fla._Ctx", str, str, Optional[str]]],
                type_hint: str = "") -> Optional[str]:
        node_id = f"{ctx.kind}:{ctx.name}::{instance_name}::{field_name}"
        if node_id in in_progress:
            # Real cycle: this node is an ancestor still being resolved higher up the
            # current call stack (e.g. a mapping reading back its own Target table as a
            # resubmission Source, chained through another mapping's identical pattern).
            # It's already in `nodes` at this point (added before its own predecessors
            # were walked), but returning it here would let the caller wire a bogus edge
            # back into an in-progress ancestor -- corrupting its classification and
            # forcing _break_cycles to drop a real downstream connector edge instead of
            # this heuristic one. Drop this hop instead; the ancestor's own real
            # predecessor chain (already in progress) completes it correctly.
            return None
        if node_id in nodes:
            return node_id
        if hop > MAX_HOPS:
            return None  # depth guard -- drop this hop, don't emit a dangling node

        # type_hint disambiguates a name collision (e.g. a SOURCE and a
        # TARGET instance both called "M2R_DTM_TRANSACTION" on the same
        # canvas) using the CONNECTOR's own FROMINSTANCETYPE/TOINSTANCETYPE
        # attribute -- the thing that was previously discarded, causing a
        # Target to be silently resolved as a dead-end Source with no
        # predecessors traced.
        inst = ctx.resolve(instance_name, type_hint)
        if inst is None:
            return None

        in_progress.add(node_id)
        try:
            # ------------------------------------------------ SOURCE -----
            if inst.type == "SOURCE":
                nodes[node_id] = {
                    "id": node_id, "kind": "SOURCE",
                    "label": f"{inst.ref_name}\n.{field_name}",
                    "mapping": owning_mapping, "session": owning_session or "",
                    "instance": instance_name, "table": inst.ref_name, "field": field_name,
                    "source_qualifier": feeding_into_instance or "",
                    "final_session": final_session or "", "final_mapping": final_mapping,
                    "final_target_table": final_target_table,
                }
                cross = _find_cross_session_producer(repo, inst.ref_name, field_name, owning_mapping,
                                                      cross_visited)
                if cross:
                    up_mapping, up_session, up_inst_name, up_field = cross
                    up_ctx = fla._build_ctx(repo, "mapping", up_mapping)
                    if up_ctx is not None:
                        # Fresh mapping context -- prefer the TARGET-typed instance for this
                        # cross-session producer lookup, since a resubmission read always
                        # points at a Target table from an earlier run, and a same-named
                        # Source elsewhere in that mapping must not be picked up instead.
                        up_id = process(up_ctx, up_mapping, up_session, up_inst_name, up_field,
                                         hop + 1, None, [], type_hint="TARGET")
                        if up_id:
                            add_edge(up_id, node_id, "flow")
                            # Mark the upstream TARGET as a mid-chain hop, and record what it
                            # feeds forward into (this Source, in this later session).
                            nodes[up_id]["kind"] = "TARGET_INTERMEDIATE"
                            nodes[up_id]["next_session"] = owning_session or "(none)"
                            nodes[up_id]["next_mapping"] = owning_mapping
                            nodes[up_id]["next_source_table"] = inst.ref_name
                return node_id

            # ----------------------------------------------- TARGET ------
            if inst.type == "TARGET":
                is_final = (node_id == final_target_id)
                nodes[node_id] = {
                    "id": node_id, "kind": "TARGET_FINAL" if is_final else "TARGET",
                    "label": f"{inst.ref_name}\n.{field_name}",
                    "mapping": owning_mapping, "session": owning_session or "",
                    "instance": instance_name, "table": inst.ref_name, "field": field_name,
                }
                preds = ctx.pred_index.get((instance_name, field_name), [])
                for (from_inst, from_field) in preds:
                    hint = ctx.edge_from_type.get((instance_name, field_name, from_inst, from_field), "")
                    child_id = process(ctx, owning_mapping, owning_session, from_inst, from_field,
                                        hop + 1, instance_name, mapplet_stack, type_hint=hint)
                    add_edge(child_id, node_id, "flow")
                return node_id

            # ---------------------------------------------- MAPPLET ------
            if inst.type == "MAPPLET":
                entry_inst_name = fla._entry_into_mapplet(repo, inst.ref_name, field_name)
                mplt_ctx = fla._build_ctx(repo, "mapplet", inst.ref_name)
                nodes[node_id] = {
                    "id": node_id, "kind": "MAPPLET",
                    "label": f"{instance_name}\n[{inst.ref_name}]",
                    "mapping": owning_mapping, "session": owning_session or "",
                    "instance": instance_name, "table": "", "field": field_name,
                    "logic": f"Enters Mapplet '{inst.ref_name}' via its output port '{field_name}'.",
                    "passthrough": True,
                }
                if mplt_ctx is not None and entry_inst_name is not None:
                    new_stack = mapplet_stack + [(ctx, instance_name, owning_mapping, owning_session)]
                    child_id = process(mplt_ctx, owning_mapping, owning_session, entry_inst_name,
                                        field_name, hop + 1, instance_name, new_stack)
                    add_edge(child_id, node_id, "flow")
                return node_id

            # ----------------------------------------- TRANSFORMATION ----
            ttype, logic_text, ref_ports, is_real_crossing = fla._resolve_port_logic(repo, ctx, inst, field_name)
            tobj = fla._get_transformation(repo, ctx, inst.ref_name)
            preds = ctx.pred_index.get((instance_name, field_name), [])
            passthrough = not is_real_crossing

            if not preds and not ref_ports:
                preds, opaque = fla.resolve_custom_transformation_predecessors(
                    ctx, instance_name, field_name, ttype, tobj)
                if preds:
                    if opaque:
                        # Black-box Custom Transformation (Java/SQL/HTTP/...): we can't say which
                        # input(s) truly feed this output, so don't claim it's a plain pass-through.
                        passthrough = False
                        logic_text = (logic_text + " " if logic_text else "") + \
                            "[Best-effort: this Custom Transformation's internal port mapping isn't " \
                            "captured in the exported metadata -- every connected input port is shown " \
                            "as a possible source.]"
                    # else: precise multi-group match (e.g. a Union) -- keep the pass-through
                    # classification, it's a genuine merge with no computation involved.

            nodes[node_id] = {
                "id": node_id, "kind": "TRANSFORMATION",
                "label": (f"{instance_name}\n[{ttype}]" if ctx.kind != "mapplet"
                          else f"{instance_name}\n[{ttype}]\n(in Mapplet {ctx.name})"),
                "mapping": owning_mapping, "session": owning_session or "",
                "instance": instance_name, "table": "", "field": field_name,
                "ttype": ttype, "logic": logic_text, "passthrough": passthrough,
                "mapplet": ctx.name if ctx.kind == "mapplet" else "",
            }
            style = "passthrough" if passthrough else "logic"

            for (from_inst, from_field) in preds:
                hint = ctx.edge_from_type.get((instance_name, field_name, from_inst, from_field), "")
                child_id = process(ctx, owning_mapping, owning_session, from_inst, from_field,
                                    hop + 1, instance_name, mapplet_stack, type_hint=hint)
                add_edge(child_id, node_id, style)
            for rf in ref_ports:
                # Same instance, different port -- type can't have changed.
                child_id = process(ctx, owning_mapping, owning_session, instance_name, rf,
                                    hop + 1, instance_name, mapplet_stack, type_hint=inst.type)
                add_edge(child_id, node_id, "logic")

            # Dead end inside a Mapplet (typically its Input Transformation boundary,
            # which has no connector of its own inside the mapplet's graph): bubble
            # back out and keep tracing from whatever feeds the enclosing MAPPLET
            # instance's own port under the same field name, per the same rule used
            # by the Target Field Lineage tracer.
            if not preds and not ref_ports and ctx.kind == "mapplet" and mapplet_stack:
                remaining = list(mapplet_stack)
                while remaining:
                    parent_ctx, parent_instance_name, parent_mapping, parent_session = remaining.pop()
                    parent_preds = parent_ctx.pred_index.get((parent_instance_name, field_name), [])
                    if parent_preds:
                        for (from_inst, from_field) in parent_preds:
                            parent_hint = parent_ctx.edge_from_type.get(
                                (parent_instance_name, field_name, from_inst, from_field), "")
                            child_id = process(parent_ctx, parent_mapping, parent_session, from_inst,
                                                from_field, hop + 1, parent_instance_name, remaining,
                                                type_hint=parent_hint)
                            add_edge(child_id, node_id, style)
                        break
            return node_id
        finally:
            in_progress.discard(node_id)

    ctx0 = fla._build_ctx(repo, "mapping", mapping_name)
    if ctx0 is None:
        return None
    # We already resolved target_inst above with an explicit TARGET filter --
    # pass that certainty through instead of letting the entry call fall back
    # to a plain name lookup that could grab a same-named Source instead.
    process(ctx0, mapping_name, final_session, target_instance_name, target_field_name, 0, None, [],
            type_hint="TARGET")
    cycle_notes = _break_cycles(nodes, edges)

    return {
        "nodes": nodes,
        "edges": edges,
        "final_mapping": final_mapping,
        "final_session": final_session,
        "final_target_table": final_target_table,
        "final_instance": target_instance_name,
        "final_field": target_field_name,
        "cycle_notes": cycle_notes,
    }
