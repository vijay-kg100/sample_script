"""
Informatica PowerCenter (XML->JSON export) Field-Level Lineage Engine
======================================================================

Parses the repository JSON, builds a session execution order from the
workflow's WORKFLOWLINK graph, then for a user-supplied TARGET INSTANCE
name:

  1. Finds which session/mapping produces that target instance -> anchor
     execution order N.
  2. Restricts scope to sessions with execution order 1..N.
  3. For every session in that range, traces every target field back to
     its source field(s) through the mapping's CONNECTOR graph, resolving:
        a. direct pass-through ports
        b. indirect ports (value computed from other ports in the same
           transformation's EXPRESSION text)
        c. connected lookups (Lookup Procedure instance sitting inline in
           the CONNECTOR chain)
        d. unconnected lookups (":LKP.NAME(args)" called from inside an
           EXPRESSION string, with no CONNECTOR edges of its own)
  4. Stitches cross-session lineage: a row's source table/field may be a
     table/field written by an EARLIER session in range (Prev_*), and a
     row's target table/field may be read as a source by a LATER session
     in range (next_*).

Output: one row per unique (session, target field, source leaf, path).
"""

import json
import re
from collections import defaultdict, deque


# --------------------------------------------------------------------------
# 1. Loading & low-level indices
# --------------------------------------------------------------------------

def load_repo(json_path):
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def get_folders(data):
    root = data["root"]
    repo = next(c for c in root["children"] if c["tag"] == "REPOSITORY")
    return [c for c in repo["children"] if c["tag"] == "FOLDER"]


def index_folder(folder):
    """Build all folder-scoped lookups: sources, targets, reusable
    transformations, mappings, workflows."""

    sources = {}       # source_instance_def_name -> {table, fields:[names]}
    targets = {}        # target_def_name -> {table, fields:[names]}
    transformations = {}  # transformation_name -> {type, fields:{name:expr}, table_attrs:{}}
    mappings = {}       # mapping_name -> mapping node (raw)
    mapplets = {}       # mapplet_name -> mapplet node (raw); same INSTANCE/
                         # CONNECTOR/TRANSFORMATION child shape as a mapping,
                         # indexed the same way via index_mapping()
    workflows = {}       # workflow_name -> workflow node (raw)

    for ch in folder["children"]:
        tag = ch["tag"]
        attrs = ch["attributes"]
        name = attrs.get("NAME")

        if tag == "SOURCE":
            fields = [f["attributes"]["NAME"] for f in ch["children"] if f["tag"] == "SOURCEFIELD"]
            sources[name] = {"table": name, "fields": fields}

        elif tag == "TARGET":
            fields = [f["attributes"]["NAME"] for f in ch["children"] if f["tag"] == "TARGETFIELD"]
            targets[name] = {"table": name, "fields": fields}

        elif tag == "TRANSFORMATION":
            transformations[name] = _index_transformation(ch)

        elif tag == "MAPPING":
            mappings[name] = ch

        elif tag == "MAPPLET":
            mapplets[name] = ch

        elif tag == "WORKFLOW":
            workflows[name] = ch

    return {
        "sources": sources,
        "targets": targets,
        "transformations": transformations,
        "mappings": mappings,
        "mapplets": mapplets,
        "workflows": workflows,
    }


def _index_transformation(node):
    field_expr = {}
    field_porttype = {}
    field_ref = {}   # REF_FIELD (used by Router/Union-style transforms instead of EXPRESSION)
    field_group = {}  # GROUP name a field belongs to (Router/Union/HierarchicalToRelational)
    group_fields_order = defaultdict(list)  # group_name -> [field names in declared order]
    group_type = {}   # group_name -> 'INPUT' / 'OUTPUT'

    for f in node["children"]:
        if f["tag"] == "GROUP":
            a = f["attributes"]
            group_type[a["NAME"]] = a.get("TYPE", "")
        if f["tag"] == "TRANSFORMFIELD":
            a = f["attributes"]
            field_expr[a["NAME"]] = a.get("EXPRESSION") or ""
            field_porttype[a["NAME"]] = a.get("PORTTYPE", "")
            if a.get("REF_FIELD"):
                field_ref[a["NAME"]] = a["REF_FIELD"]
            grp = a.get("GROUP")
            if grp:
                field_group[a["NAME"]] = grp
                group_fields_order[grp].append(a["NAME"])

    table_attrs = {}
    for f in node["children"]:
        if f["tag"] == "TABLEATTRIBUTE":
            a = f["attributes"]
            table_attrs[a["NAME"]] = a.get("VALUE", "")
    return {
        "type": node["attributes"].get("TYPE", ""),
        "fields": field_expr,
        "porttype": field_porttype,
        "ref_field": field_ref,
        "table_attrs": table_attrs,
        "field_group": field_group,
        "group_fields_order": dict(group_fields_order),
        "group_type": group_type,
    }


def index_mapping(mapping_node):
    """Build mapping-scoped indices: instances, connectors, local (non-reusable)
    transformation defs (which override/extend folder-level reusable ones)."""

    instances = {}   # instance_name -> {type, transformation_name, transformation_type}
    connectors = []  # list of dicts
    local_transformations = {}

    for ch in mapping_node["children"]:
        tag = ch["tag"]
        if tag == "INSTANCE":
            a = ch["attributes"]
            instances[a["NAME"]] = {
                "type": a.get("TYPE"),
                "transformation_name": a.get("TRANSFORMATION_NAME"),
                "transformation_type": a.get("TRANSFORMATION_TYPE"),
            }
        elif tag == "CONNECTOR":
            connectors.append(ch["attributes"])
        elif tag == "TRANSFORMATION":
            local_transformations[ch["attributes"]["NAME"]] = _index_transformation(ch)

    # index connectors by (TOINSTANCE, TOFIELD) for O(1) backward lookup
    incoming = defaultdict(list)
    for c in connectors:
        incoming[(c["TOINSTANCE"], c["TOFIELD"])].append(c)

    return {
        "instances": instances,
        "connectors": connectors,
        "incoming": incoming,
        "local_transformations": local_transformations,
    }


# --------------------------------------------------------------------------
# 2. Session execution order (from WORKFLOWLINK topological sort)
# --------------------------------------------------------------------------

def build_session_order(workflow_node):
    """Returns:
        session_to_mapping: {session_name: mapping_name}
        session_order: {session_name: 1-based execution order int}
        order_list: [(order, session_name, mapping_name), ...] sorted
    """
    task_type = {}
    for ch in workflow_node["children"]:
        if ch["tag"] == "TASKINSTANCE":
            a = ch["attributes"]
            task_type[a["NAME"]] = a.get("TASKTYPE")

    session_to_mapping = {}
    for ch in workflow_node["children"]:
        if ch["tag"] == "SESSION":
            a = ch["attributes"]
            session_to_mapping[a["NAME"]] = a.get("MAPPINGNAME")

    edges = []
    nodes = set(task_type.keys())
    for ch in workflow_node["children"]:
        if ch["tag"] == "WORKFLOWLINK":
            a = ch["attributes"]
            frm, to = a["FROMTASK"], a["TOTASK"]
            nodes.add(frm)
            nodes.add(to)
            edges.append((frm, to))

    adj = defaultdict(list)
    indeg = {n: 0 for n in nodes}
    for frm, to in edges:
        adj[frm].append(to)
        indeg[to] = indeg.get(to, 0) + 1

    # stable Kahn's algorithm, preserving file order among ties
    file_order = {n: i for i, n in enumerate(nodes)}
    queue = deque(sorted([n for n in nodes if indeg.get(n, 0) == 0],
                          key=lambda n: file_order[n]))
    visited = set()
    topo = []
    while queue:
        n = queue.popleft()
        if n in visited:
            continue
        visited.add(n)
        topo.append(n)
        nxts = sorted(adj[n], key=lambda n2: file_order[n2])
        for n2 in nxts:
            indeg[n2] -= 1
            if indeg[n2] <= 0 and n2 not in visited:
                queue.append(n2)
    # any leftover nodes (disconnected / cycles) appended in file order
    for n in sorted(nodes, key=lambda n: file_order[n]):
        if n not in visited:
            topo.append(n)
            visited.add(n)

    session_order = {}
    order_list = []
    counter = 0
    for n in topo:
        if task_type.get(n) == "Session" and n in session_to_mapping:
            counter += 1
            session_order[n] = counter
            order_list.append((counter, n, session_to_mapping[n]))

    return session_to_mapping, session_order, order_list


# --------------------------------------------------------------------------
# 3. Expression parsing helpers (for indirect fields + lookup calls)
# --------------------------------------------------------------------------

_LKP_RE = re.compile(r":LKP\.(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_SQL_KEYWORDS = {
    "IIF", "DECODE", "TRUE", "FALSE", "AND", "OR", "NOT", "NULL", "ISNULL",
    "IS", "IN", "TO_DATE", "TO_CHAR", "TO_DECIMAL", "TO_INTEGER", "TO_FLOAT",
    "SUBSTR", "TRIM", "LTRIM", "RTRIM", "UPPER", "LOWER", "CONCAT", "LENGTH",
    "ROUND", "TRUNC", "ABS", "SYSDATE", "SYSTIMESTAMP", "ADD_TO_DATE",
    "DATE_DIFF", "INSTR", "REPLACESTR", "REPLACECHR", "LPAD", "RPAD", "MAX",
    "MIN", "SUM", "COUNT", "AVG", "FIRST", "LAST", "NEXTVAL", "CURRVAL",
    "ERROR", "ABORT", "IFNULL", "COALESCE", "REG_EXTRACT", "REG_MATCH",
    "REG_REPLACE", "LKP",
}


def parse_expression_refs(expr, known_fields):
    """Return (referenced_field_names, lookup_calls) found in an expression
    string, where referenced_field_names only includes names that are
    actual ports (known_fields) of the owning transformation, and
    lookup_calls is a list of (lookup_name, raw_args_string)."""
    if not expr:
        return [], []

    lookup_calls = _LKP_RE.findall(expr)
    # strip the lkp calls out before generic token scanning so we don't
    # double count arg names (they'll be picked up as refs already, which
    # is fine/desired since args reference real ports)
    tokens = set(_TOKEN_RE.findall(expr))
    refs = [t for t in tokens if t in known_fields and t.upper() not in _SQL_KEYWORDS]
    return refs, lookup_calls


def short(expr, n=70):
    if not expr:
        return ""
    e = " ".join(expr.split())
    return e if len(e) <= n else e[: n - 3] + "..."


# --------------------------------------------------------------------------
# 4. Backward field tracer (within one mapping) - with Mapplet support
# --------------------------------------------------------------------------

# A Mapplet's interface is exposed via two special boundary transformations
# inside its own definition: an "Input Transformation" (ports fed from
# OUTSIDE the mapplet, by the enclosing mapping) and an "Output
# Transformation" (ports whose value is produced INSIDE the mapplet and
# exposed to the enclosing mapping). Both the TRANSFORMATION tag's own TYPE
# attribute and the consuming INSTANCE's TRANSFORMATION_TYPE attribute are
# checked against these known strings (Informatica exports have been seen
# to populate either one) - normalized case-insensitively.
MAPPLET_INPUT_TYPES = {"input transformation", "mapplet input"}
MAPPLET_OUTPUT_TYPES = {"output transformation", "mapplet output"}


def _normalize_ttype(s):
    return (s or "").strip().lower()


def _resolve_scope_transformation(scope_idx, folder_idx, name):
    """Resolve a transformation definition by name within a mapping OR
    mapplet scope (local override first, then folder-level reusable)."""
    if name in scope_idx.get("local_transformations", {}):
        return scope_idx["local_transformations"][name]
    return folder_idx["transformations"].get(name)


def _instance_table_name(scope_idx, folder_idx, instance_name):
    """Physical table name behind a SOURCE or TARGET instance in a scope."""
    inst = scope_idx["instances"].get(instance_name)
    if not inst:
        return instance_name
    tname = inst["transformation_name"]
    if inst["type"] == "SOURCE":
        return folder_idx["sources"].get(tname, {}).get("table", tname)
    if inst["type"] == "TARGET":
        return folder_idx["targets"].get(tname, {}).get("table", tname)
    return tname


def _get_mapplet_port_maps(folder_idx, mapplet_def_name):
    """For a Mapplet definition, build the two-way mapping between the
    port name EXPOSED to any mapping/mapplet that instantiates it (used by
    CONNECTOR FROMFIELD/TOFIELD at the enclosing scope) and the port name
    used INTERNALLY by the mapplet's own Input/Output Transformation
    boundary (used by CONNECTOR edges INSIDE the mapplet's own scope).

    PowerCenter exports the exposed interface as a self-referencing
    TRANSFORMATION child of the MAPPLET node (NAME == mapplet name,
    TYPE == "Mapplet"): each TRANSFORMFIELD's NAME is the exposed name,
    REF_FIELD is the internal name, and REF_INSTANCETYPE says whether it
    belongs to the mapplet's Input or Output boundary. The two names are
    NOT guaranteed to match (Informatica auto-suffixes on collisions, e.g.
    exposed "SOURCENAME1" -> internal "SOURCENAME") - any code that crosses
    this boundary must translate through this map rather than assuming the
    names line up, or it will silently fail to find the internal port /
    silently fail to find the enclosing CONNECTOR edge.

    Returns (exposed_to_internal, internal_to_exposed):
      exposed_to_internal: {exposed_name: (internal_name, boundary)}
      internal_to_exposed: {(boundary, internal_name): exposed_name}
    where boundary is "input" or "output". Falls back to an identity
    mapping for any port not found in the shadow transformation, so
    behavior degrades gracefully if an export is missing it."""
    cache = folder_idx.setdefault("_mapplet_port_map_cache", {})
    if mapplet_def_name in cache:
        return cache[mapplet_def_name]

    exposed_to_internal = {}
    internal_to_exposed = {}
    mapplet_node = folder_idx.get("mapplets", {}).get(mapplet_def_name)
    if mapplet_node is not None:
        for ch in mapplet_node["children"]:
            if (ch["tag"] == "TRANSFORMATION"
                    and ch["attributes"].get("NAME") == mapplet_def_name
                    and _normalize_ttype(ch["attributes"].get("TYPE")) == "mapplet"):
                for f in ch["children"]:
                    if f["tag"] != "TRANSFORMFIELD":
                        continue
                    a = f["attributes"]
                    exposed = a["NAME"]
                    internal = a.get("REF_FIELD") or exposed
                    ref_type = _normalize_ttype(a.get("REF_INSTANCETYPE"))
                    boundary = ("input" if "input" in ref_type
                                else "output" if "output" in ref_type else "")
                    exposed_to_internal[exposed] = (internal, boundary)
                    if boundary:
                        internal_to_exposed[(boundary, internal)] = exposed
                break

    result = (exposed_to_internal, internal_to_exposed)
    cache[mapplet_def_name] = result
    return result


def _find_boundary_ports(scope_idx, folder_idx, boundary_types):
    """Within a mapplet's own scope, map {port_name: instance_name} for
    every port exposed by its boundary transformation(s) of the given kind
    (MAPPLET_INPUT_TYPES or MAPPLET_OUTPUT_TYPES). Matches on either the
    consuming INSTANCE's TRANSFORMATION_TYPE or the underlying
    TRANSFORMATION tag's own TYPE, whichever is populated."""
    ports = {}
    for inst_name, inst in scope_idx["instances"].items():
        if inst["type"] != "TRANSFORMATION":
            continue
        ttype_attr = _normalize_ttype(inst.get("transformation_type"))
        tdef = _resolve_scope_transformation(scope_idx, folder_idx, inst["transformation_name"])
        resolved_ttype = _normalize_ttype(tdef.get("type")) if tdef else ""
        if ttype_attr not in boundary_types and resolved_ttype not in boundary_types:
            continue
        if not tdef:
            continue
        for fname in tdef["fields"].keys():
            ports.setdefault(fname, inst_name)
    return ports


# Safety valve: for a handful of pathological fields (deep chains of
# Router/Union fan-out multiplied across several nested/reused Mapplets),
# now that mapplet-internal detail is no longer collapsed away (see
# MappingTracer._expand_one_mapplet), the number of DISTINCT contributing
# paths can be enormous even with full memoization of shared substructure
# - memoization avoids recomputing a shared sub-chain twice, but cannot
# shrink a genuine combinatorial cross-product of branches down to a small
# output. Rather than let one such field exhaust memory/time and take down
# the whole report, cap the number of _backward_trace descents per
# top-level trace_field() call; once hit, any remaining branches collapse
# into one clearly-labeled "truncated" leaf instead of expanding further,
# crashing, or (worse) being silently dropped without any indication.
_TRACE_CALL_BUDGET = 5000
# Independent cap on the number of leaves a single top-level trace_field()
# call is allowed to RETURN. Even well inside the call budget above, a
# field computed as e.g. CONCAT() of many refs, each themselves a CONCAT of
# many refs, can legitimately produce thousands of distinct contributing
# paths - which is both too much for memory at real-repository scale and
# not actually useful to a human reading the report. Kept separate from
# the call budget because the two bound different things (compute cost vs.
# output size).
_MAX_LEAVES_PER_FIELD = 300
_trace_call_counter = {"n": 0, "truncated": False}


def _backward_trace(scope_idx, folder_idx, instance, field, path, visited):
    _trace_call_counter["n"] += 1
    if _trace_call_counter["n"] > _TRACE_CALL_BUDGET:
        _trace_call_counter["truncated"] = True
        return [{
            "source_instance": None, "source_table": None, "source_field": None,
            "path": list(path) + [f"{instance}[...Trace truncated: too many contributing paths]"],
            "leaf_type": "truncated",
        }]
    """Core backward tracer, parametrized by scope (a mapping's OR a
    mapplet's own index, both built by index_mapping - the connector graph
    shape is identical). Returns a list of leaf dicts. Two special leaf
    types signal that resolution must continue in a DIFFERENT scope, and
    are only ever consumed by MappingTracer (never surfaced to callers):

      "mapplet_boundary"    - `instance` is a Mapplet instance; its ports
                               have no incoming connector HERE because the
                               value is produced by the mapplet's own
                               internals. Caller must dive into that
                               mapplet's scope.
      "scope_input_boundary" - `instance` is THIS scope's own Input
                               Transformation; the real predecessor lives
                               one level up, outside this scope. Caller
                               must bridge back out.
    """
    key = (instance, field)
    if key in visited:
        return []
    visited = visited | {key}

    edges = scope_idx["incoming"].get((instance, field), [])

    if edges:
        results = []
        for e in edges:
            fi, ff, ftype = e["FROMINSTANCE"], e["FROMFIELD"], e["FROMINSTANCETYPE"]
            if ftype == "Source Definition":
                results.append({
                    "source_instance": fi,
                    "source_table": _instance_table_name(scope_idx, folder_idx, fi),
                    "source_field": ff,
                    "path": list(path),
                    "leaf_type": "source",
                })
            else:
                label = f"{fi}[{ftype}:{ff}]"
                new_path = path + [label] if (not path or path[-1] != label) else path
                results.extend(_backward_trace(scope_idx, folder_idx, fi, ff, new_path, visited))
        return results

    # --- no incoming CONNECTOR edge in this scope ---
    inst = scope_idx["instances"].get(instance)
    if not inst:
        return [{
            "source_instance": None, "source_table": None,
            "source_field": None, "path": list(path), "leaf_type": "unmapped",
        }]

    t_name = inst.get("transformation_name")

    # Crossing INTO a Mapplet instance (detected by transformation_name
    # membership in folder_idx["mapplets"], regardless of how the INSTANCE's
    # own TYPE/TRANSFORMATION_TYPE attributes happen to be populated).
    if t_name in folder_idx.get("mapplets", {}):
        return [{"leaf_type": "mapplet_boundary", "mapplet_instance": instance,
                 "mapplet_def": t_name, "field": field, "path": list(path)}]

    ttype_attr = _normalize_ttype(inst.get("transformation_type"))
    tdef = _resolve_scope_transformation(scope_idx, folder_idx, t_name)
    resolved_ttype = _normalize_ttype(tdef.get("type")) if tdef else ""

    # Reached THIS scope's own Input Transformation boundary - i.e. we are
    # currently tracing INSIDE a mapplet and hit its interface. The real
    # predecessor is one level up, outside this scope.
    if ttype_attr in MAPPLET_INPUT_TYPES or resolved_ttype in MAPPLET_INPUT_TYPES:
        return [{"leaf_type": "scope_input_boundary", "boundary_instance": instance,
                 "boundary_field": field, "path": list(path)}]

    if inst["type"] not in ("TRANSFORMATION",):
        # dead end: e.g. a source field with genuinely no upstream,
        # or unresolved instance
        return [{
            "source_instance": None, "source_table": None,
            "source_field": None, "path": list(path), "leaf_type": "unmapped",
        }]

    if tdef is None:
        return [{
            "source_instance": None, "source_table": None,
            "source_field": None, "path": list(path), "leaf_type": "unresolved_transformation",
        }]

    # connected-lookup output port (LOOKUP/OUTPUT / RETURN) with no
    # expression -> value comes from the physical lookup table itself
    porttype = tdef["porttype"].get(field, "")
    expr = tdef["fields"].get(field, "")

    # Router/Union-style transforms: output ports point at their source
    # port via REF_FIELD rather than an EXPRESSION string
    ref_field = tdef.get("ref_field", {}).get(field)
    if ref_field and ref_field in tdef["fields"] and ref_field != field:
        label = f"{instance}[{tdef['type']}:{field}=REF({ref_field})]"
        return _backward_trace(scope_idx, folder_idx, instance, ref_field, path + [label], visited)

    # Union/Custom-transformation OUTPUT group fields: no EXPRESSION, no
    # REF_FIELD, but a same-position field exists in each INPUT group
    # (Informatica correlates union fields positionally within groups)
    field_group = tdef.get("field_group", {})
    group_order = tdef.get("group_fields_order", {})
    group_type = tdef.get("group_type", {})
    if not expr and field in field_group:
        my_group = field_group[field]
        if group_type.get(my_group) == "OUTPUT" and my_group in group_order:
            idx = group_order[my_group].index(field)
            input_groups = [g for g, t in group_type.items() if t == "INPUT"]
            branch_fields = []
            for g in input_groups:
                flist = group_order.get(g, [])
                if idx < len(flist):
                    branch_fields.append(flist[idx])
            if branch_fields:
                label = f"{instance}[{tdef['type']}:{field}<-UNION({my_group})]"
                new_path = path + [label]
                results = []
                for bf in branch_fields:
                    results.extend(_backward_trace(scope_idx, folder_idx, instance, bf, new_path, visited))
                if results:
                    return results

    if "LOOKUP" in porttype and not expr:
        lkp_table = tdef["table_attrs"].get("Lookup table name", t_name)
        label = f"{instance}[Lookup:{field}]"
        return [{
            "source_instance": instance,
            "source_table": lkp_table,
            "source_field": field,
            "path": path + [label],
            "leaf_type": "connected_lookup",
        }]

    refs, lkp_calls = parse_expression_refs(expr, set(tdef["fields"].keys()))
    label = f"{instance}[{tdef['type']}:{field}={short(expr)}]"
    new_path = path + [label]

    results = []
    for lkp_name, raw_args in lkp_calls:
        lkp_def = _resolve_scope_transformation(scope_idx, folder_idx, lkp_name)
        lkp_table = (lkp_def["table_attrs"].get("Lookup table name", lkp_name)
                     if lkp_def else lkp_name)
        lkp_label = f"{lkp_name}[Unconnected Lookup:{lkp_table}]"
        results.append({
            "source_instance": lkp_name,
            "source_table": lkp_table,
            "source_field": None,
            "path": new_path + [lkp_label],
            "leaf_type": "unconnected_lookup",
        })
        # also trace the args passed into the lookup call, since those
        # are ports of the SAME transformation feeding the lookup
        for arg_tok in _TOKEN_RE.findall(raw_args):
            if arg_tok in tdef["fields"] and arg_tok != field:
                results.extend(_backward_trace(scope_idx, folder_idx, instance, arg_tok, new_path, visited))

    for ref in refs:
        if ref != field:
            results.extend(_backward_trace(scope_idx, folder_idx, instance, ref, new_path, visited))

    if not results:
        # pure literal / constant expression (e.g. SYSDATE, a string
        # literal, or NEXTVAL handled elsewhere) - nothing further
        # upstream to trace
        results.append({
            "source_instance": None, "source_table": None,
            "source_field": None, "path": new_path, "leaf_type": "literal_or_unresolved",
        })
    return results


class MappingTracer:
    def __init__(self, folder_idx, mapping_name):
        self.folder_idx = folder_idx
        self.mapping_name = mapping_name
        self.mapping_node = folder_idx["mappings"][mapping_name]
        self.m = index_mapping(self.mapping_node)

    def resolve_transformation(self, name):
        return _resolve_scope_transformation(self.m, self.folder_idx, name)

    def instance_table_name(self, instance_name):
        """Physical table name behind a SOURCE or TARGET instance."""
        return _instance_table_name(self.m, self.folder_idx, instance_name)

    def _get_mapplet_scope(self, mapplet_def_name):
        """index_mapping()-shaped index for a Mapplet's own connector graph,
        cached on folder_idx (shared across every MappingTracer instance in
        this run - a mapplet definition is folder-wide, its internals don't
        vary by which mapping uses it)."""
        cache = self.folder_idx.setdefault("_mapplet_scope_cache", {})
        if mapplet_def_name not in cache:
            node = self.folder_idx.get("mapplets", {}).get(mapplet_def_name)
            cache[mapplet_def_name] = index_mapping(node) if node is not None else None
        return cache[mapplet_def_name]

    def trace_field(self, instance, field, path=None, visited=None):
        """Backward-trace one (instance, field). Returns a list of leaf dicts:
           {source_instance, source_table, source_field, path: [...], leaf_type}
        Transparently dives into and back out of any Mapplet(s) along the
        way; the full path through any Mapplet's internals is preserved in
        the returned path (the same internal detail is also captured,
        boundary-to-boundary, in Tab-3 - see build_mapplet_catalog).

        When called fresh (path and visited both omitted - the normal case
        for every top-level call from build_lineage.py), the result is
        cached per (mapping, instance, field): the set of leaves reachable
        backward from a given starting point never depends on how the
        caller got there, so without this cache the same shared upstream
        lineage (joins, reused mapplets, etc.) gets independently
        re-derived once per row that happens to share it - across a large
        repository with many target fields and reused mapplets this is
        enough duplicated work to blow up runtime/memory. Continuation
        calls (non-empty path/visited, used internally when bridging back
        out of a mapplet mid-trace) are NOT cached, since their result is
        specific to that call site's accumulated path."""
        if path is None and visited is None:
            cache = self.folder_idx.setdefault("_trace_field_cache", {})
            key = (self.mapping_name, instance, field)
            if key in cache:
                return cache[key]
            # Fresh top-level call: reset the shared trace-call budget so
            # this field gets its own full allowance rather than sharing
            # (or being starved by) whatever a previous field's trace used.
            _trace_call_counter["n"] = 0
            _trace_call_counter["truncated"] = False
            raw_leaves = _backward_trace(self.m, self.folder_idx, instance, field, [], frozenset())
            result = self._expand_boundary_leaves(raw_leaves, frozenset())
            if len(result) > _MAX_LEAVES_PER_FIELD:
                omitted = len(result) - _MAX_LEAVES_PER_FIELD
                result = result[:_MAX_LEAVES_PER_FIELD] + [{
                    "source_instance": None, "source_table": None, "source_field": None,
                    "path": [f"...[{omitted} additional contributing path(s) omitted - "
                             f"too many to list]"],
                    "leaf_type": "truncated_output",
                }]
            cache[key] = result
            return result

        if path is None:
            path = []
        if visited is None:
            visited = frozenset()
        raw_leaves = _backward_trace(self.m, self.folder_idx, instance, field, path, visited)
        return self._expand_boundary_leaves(raw_leaves, visited)

    def _expand_boundary_leaves(self, leaves, visited):
        out = []
        for leaf in leaves:
            if leaf.get("leaf_type") == "mapplet_boundary":
                out.extend(self._expand_one_mapplet(leaf, visited))
            else:
                out.append(leaf)
        return out

    def _expand_one_mapplet(self, leaf, visited):
        mapplet_def = leaf["mapplet_def"]
        mapplet_instance = leaf["mapplet_instance"]
        field = leaf["field"]  # EXPOSED output port name, per the enclosing scope's CONNECTOR

        # Guard against cross-mapplet cycles (e.g. two mapplets whose exposed
        # ports reference each other's outputs as inputs). `visited` is
        # threaded through both recursive call sites below (nested-mapplet
        # expansion and the bridge-back-out trace_field call), but neither
        # _backward_trace's own cycle check nor the inner per-mapplet-scope
        # trace (which always starts from a fresh, empty frozenset - see
        # the inner_leaves call below) can see a loop that crosses back out
        # through a DIFFERENT mapplet instance/field. Without this guard,
        # such a loop recurses until RecursionError.
        guard_key = ("mapplet_boundary", mapplet_instance, field)
        if guard_key in visited:
            return [{**leaf, "leaf_type": "cyclic_mapplet_reference",
                     "source_instance": None, "source_table": None, "source_field": None}]
        visited = visited | {guard_key}

        scope = self._get_mapplet_scope(mapplet_def)
        if scope is None:
            return [{**leaf, "leaf_type": "unresolved_mapplet",
                     "source_instance": None, "source_table": None, "source_field": None}]

        # The exposed port name (as seen on the enclosing CONNECTOR) is not
        # guaranteed to match the internal port name used by the mapplet's
        # own Output Transformation - translate via the mapplet's port map
        # before looking it up in this scope, or a mismatch here silently
        # produces an empty lineage chain (see _get_mapplet_port_maps).
        exposed_to_internal, internal_to_exposed = _get_mapplet_port_maps(self.folder_idx, mapplet_def)
        internal_out_field, _btype = exposed_to_internal.get(field, (field, "output"))

        out_ports = _find_boundary_ports(scope, self.folder_idx, MAPPLET_OUTPUT_TYPES)
        out_instance = out_ports.get(internal_out_field)
        if out_instance is None:
            return [{**leaf, "leaf_type": "unresolved_mapplet_output",
                     "source_instance": None, "source_table": None, "source_field": None}]

        # ONE collapsed hop for the outer (Tab-1) lineage chain, labeled
        # with the EXPOSED field name (matches what the enclosing
        # CONNECTOR/target actually shows). The mapplet's own internal
        # hops (il["path"] below) are folded into the SAME path rather
        # than discarded, so Tab-1 shows the full source-to-target chain
        # through the mapplet's internals; the same internal detail is
        # also captured separately, boundary-to-boundary, by
        # build_mapplet_catalog (Tab-3).
        hop_label = f"{mapplet_instance}[Mapplet:{field}]"
        outer_path = leaf["path"] + [hop_label]

        # This inner trace always starts fresh (path=[], visited=frozenset())
        # and depends only on the mapplet DEFINITION, not on which mapping or
        # upstream call reached it - the same mapplet is commonly reused many
        # times across a large repository, so memoize it per
        # (mapplet_def, out_instance, internal_out_field) rather than
        # re-walking its entire internal graph on every reuse.
        inner_cache = self.folder_idx.setdefault("_mapplet_inner_trace_cache", {})
        inner_key = (mapplet_def, out_instance, internal_out_field)
        if inner_key in inner_cache:
            inner_leaves = inner_cache[inner_key]
        else:
            inner_leaves = _backward_trace(scope, self.folder_idx, out_instance, internal_out_field, [], frozenset())
            inner_cache[inner_key] = inner_leaves
        result = []
        for il in inner_leaves:
            ilt = il.get("leaf_type")
            combined_path = outer_path + il["path"]
            if ilt == "mapplet_boundary":
                # nested mapplet-in-mapplet - expand recursively
                result.extend(self._expand_one_mapplet({**il, "path": combined_path}, visited))
            elif ilt == "scope_input_boundary":
                # bridge back out: translate the mapplet's own INTERNAL
                # Input Transformation port name back to the EXPOSED port
                # name before continuing to trace outside this scope,
                # since the enclosing scope's CONNECTORs are keyed by the
                # exposed name.
                internal_in_field = il["boundary_field"]
                exposed_in_field = internal_to_exposed.get(("input", internal_in_field), internal_in_field)
                result.extend(self.trace_field(mapplet_instance, exposed_in_field, combined_path, visited))
            else:
                result.append({**il, "path": combined_path})
        return result


# --------------------------------------------------------------------------
# 5. Lineage-chain formatting (Tab-1 "Transformation Lineage" column)
# --------------------------------------------------------------------------

_HOP_LABEL_RE = re.compile(r"^(.*?)\[(.*)\]$")


def _parse_hop_label(label):
    """Parse one raw trace-hop label into (name, type, field).

    Labels produced by MappingTracer look like:
        'JNR_INFO[Joiner:IN_FIELD]'                    -> ('JNR_INFO', 'Joiner', 'IN_FIELD')
        'EXP_DATA[Expression:OUT_FLD=expr text]'        -> ('EXP_DATA', 'Expression', 'OUT_FLD')
        'RTR_X[Router:F=REF(Y)]'                        -> ('RTR_X', 'Router', 'F')
        'RTR_X[Router:F<-UNION(g)]'                     -> ('RTR_X', 'Router', 'F')
        'LKP_A[Unconnected Lookup:tbl]'                 -> ('LKP_A', 'Unconnected Lookup', 'tbl')
        'X[Type]' (no colon, rare/fallback)             -> ('X', 'Type', '')
    """
    m = _HOP_LABEL_RE.match(label)
    if not m:
        return label, "", ""
    name, inner = m.group(1), m.group(2)
    if ":" not in inner:
        return name, inner.strip(), ""
    ttype, rest = inner.split(":", 1)
    field = rest.split("=")[0].split("<-")[0].strip()
    return name, ttype.strip(), field


def _group_hops(path):
    """Collapse a raw trace 'path' (target-first order) into one entry per
    physical transformation instance, dropping the internal-derivation
    sub-hops that happen when a value is computed via a variable/expression
    chain within the SAME instance.

    Within a run of consecutive hops for the same (name, type), the FIRST
    one encountered in target-first order is kept - that's always the hop
    that was added when CROSSING INTO this instance from downstream (i.e.
    the field that actually hands off to the next transformation in the
    chain), which is the attribute worth showing. Any subsequent hops in
    that run are purely internal derivation detail and are dropped.

    Returns a list of (name, type, field) in target-first order.
    """
    groups = []
    for label in path:
        name, ttype, field = _parse_hop_label(label)
        base_key = (name, ttype)
        if groups and groups[-1][0] == base_key:
            continue
        groups.append((base_key, name, ttype, field))
    return [(name, ttype, field) for _, name, ttype, field in groups]


def format_lineage_chain(path):
    """Turn a MappingTracer leaf 'path' (built target-side-first while
    tracing backward) into the display string, source -> target order:

        'SQ_X.SRC_FLD[Source Qualifier] -> EXP_X.OUT_FLD[Expression] -> RTR_X.F[Router]'

    Each hop shows the transformation instance name AND the specific
    attribute/port involved at that hop, so clicking a hop unambiguously
    identifies which Tab-2 row it corresponds to."""
    if not path:
        return ""
    groups = _group_hops(path)
    display = [
        f"{name}.{field}[{ttype}]" if field else f"{name}[{ttype}]"
        for name, ttype, field in reversed(groups)
    ]
    return " -> ".join(display)


def format_lineage_chain_data(path, mapping_name=""):
    """Companion to format_lineage_chain. Returns a JSON string of structured
    dicts [{name, type, field, mapping}, ...] mapping 1:1 with the hops shown
    in format_lineage_chain, so UI clicks know exactly which instance, field,
    AND mapping to look for in Tab 2. The mapping is required for correct
    navigation: the same instance name can exist with completely different
    logic in a different mapping."""
    return json.dumps(_lineage_chain_hops(path, {"mapping": mapping_name, "scope": "mapping"}))


def _lineage_chain_hops(path, extra=None):
    """Shared helper behind format_lineage_chain_data (Tab-1) and the Tab-3
    mapplet-internal chain builder: turns a raw trace path into the ordered
    list of hop dicts (source -> target), each hop's own name/type/field
    merged with whatever fixed context fields the caller passes in `extra`
    (mapping/mapplet name, "scope" routing hint, etc.) - one call site per
    provenance of the hops, so a chain that mixes hops from more than one
    scope can be assembled by calling this once per segment."""
    if not path:
        return []
    groups = _group_hops(path)
    extra = extra or {}
    return [
        {"name": name, "type": ttype, "field": field, **extra}
        for name, ttype, field in reversed(groups)
    ]


# --------------------------------------------------------------------------
# 6. Transformation catalog (Tab-2)
# --------------------------------------------------------------------------

# Columns the user actually sees (Excel sheet / HTML table), in this exact
# order. Internal bookkeeping fields (_Mapping, _Port) ride along in the same
# row dicts for unambiguous click-to-navigate matching, but are NEVER
# rendered as columns - callers must select CATALOG_DISPLAY_COLS explicitly
# before writing to Excel/CSV.
CATALOG_DISPLAY_COLS = [
    "Transformation Name",
    "Transformation Type",
    "Business Logic",
    "Additional Informations",
    "Input Ports",
    "Output Ports",
    "Custom / Variable Ports",
]


def _build_instance_catalog_rows(scope_items, folder_idx, scope_bookkeeping_col,
                                  visible_scope_col=None):
    """Shared implementation behind both the Tab-2 "Transformations" catalog
    (scope = mapping) and the Tab-4 "Mapplet_Transformations" catalog
    (scope = mapplet). Structurally identical either way: a MAPPING and a
    MAPPLET are indexed the same way via index_mapping(), so "scope" here
    just means "whichever of the two we were handed".

    Returns one row per (scope, instance, output/variable port) - i.e. one
    row per distinct piece of business logic. Instance names are only unique
    WITHIN a single scope (the same instance name, especially for
    non-reusable / locally-overridden transformations, can legitimately
    carry completely different logic in a different mapping/mapplet), so
    rows are never collapsed across scopes - each occurrence gets its own
    row with its own Input/Output/Custom-Variable ports.

    Pure pass-through INPUT ports (no expression, nothing computed) don't
    get their own row - they only show up in the "Input Ports" list of
    whichever output/variable row actually consumes them.

    scope_items: iterable of (scope_name, scope_node), e.g.
                 folder_idx["mappings"].items() or folder_idx["mapplets"].items()
    scope_bookkeeping_col: bookkeeping column name the scope name is stored
                 under on every row ("_Mapping" for Tab-2, "_Mapplet" for
                 Tab-4) - used together with "_Port" for unambiguous
                 click-to-navigate matching.
    visible_scope_col: if given, the scope name is ALSO stored under this
                 user-facing display column name (Tab-4's "Mapplet Name").
                 Tab-2 leaves this None, matching its existing behaviour of
                 not surfacing a visible mapping column.
    """

    rows = []

    for scope_name, scope_node in scope_items:
        m = index_mapping(scope_node)
        for inst_name, inst in m["instances"].items():
            if inst["type"] != "TRANSFORMATION":
                continue

            ttype = inst.get("transformation_type") or ""

            tdef = m["local_transformations"].get(inst["transformation_name"])
            if tdef is None:
                tdef = folder_idx["transformations"].get(inst["transformation_name"])
            if not tdef:
                continue

            known_fields = set(tdef["fields"].keys())

            def classify(p):
                p_pt = tdef["porttype"].get(p, "").upper()
                p_expr = tdef["fields"].get(p, "")
                is_in = "INPUT" in p_pt
                is_out = "OUTPUT" in p_pt
                is_var = "VARIABLE" in p_pt or p.lower().startswith(("v_", "i_", "o_"))
                if not (is_in or is_out or is_var):
                    if p_expr:
                        is_var = True
                    else:
                        is_in = True
                return is_in, is_out, is_var, p_pt, p_expr

            # One row per port that actually carries logic (OUTPUT or
            # VARIABLE). Plain pass-through INPUT ports are skipped here -
            # they'll appear as "Input Ports" on whichever row uses them.
            for port_name in known_fields:
                own_is_in, own_is_out, own_is_var, own_pt, own_expr = classify(port_name)
                if own_is_in and not own_is_out and not own_is_var:
                    continue  # pure pass-through input, no logic to report on its own

                in_ports, var_ports, out_ports = set(), set(), set()
                logic_lines = []
                visited = set()

                def trace(p):
                    if p in visited:
                        return
                    visited.add(p)
                    is_in, is_out, is_var, p_pt, p_expr = classify(p)

                    if is_out and p == port_name:
                        out_ports.add(p)
                    elif is_var or (is_out and p != port_name):
                        var_ports.add(p)
                    elif is_in and not p_expr:
                        in_ports.add(p)

                    if p_expr:
                        logic_lines.append(f"{p} = {p_expr}")
                        refs, _ = parse_expression_refs(p_expr, known_fields)
                        for ref in refs:
                            trace(ref)
                    elif is_out and is_in:
                        in_ports.add(p)

                trace(port_name)
                logic_lines.reverse()  # dependencies first, target field's own logic last

                addl_info = []
                if "LOOKUP" in own_pt and not own_expr:
                    addl_info.append("Connected Lookup output (value sourced from lookup table)")
                if own_is_var and not own_is_out:
                    addl_info.append("Local variable / intermediate port")
                if own_is_out and own_is_in:
                    addl_info.append("Pass-through port")

                row = {
                    "Transformation Name": inst_name,
                    "Transformation Type": ttype,
                    "Business Logic": "\n".join(logic_lines),
                    "Additional Informations": "; ".join(addl_info),
                    "Input Ports": ", ".join(sorted(in_ports)),
                    "Output Ports": ", ".join(sorted(out_ports)),
                    "Custom / Variable Ports": ", ".join(sorted(var_ports)),
                    scope_bookkeeping_col: scope_name,
                    "_Port": port_name,
                }
                if visible_scope_col:
                    row[visible_scope_col] = scope_name
                rows.append(row)

    return rows


def build_transformation_catalog(folder_idx):
    """Folder-wide catalog of every transformation INSTANCE, one row per
    (mapping, instance, output/variable port). See
    _build_instance_catalog_rows for the shared row-building logic."""

    rows = _build_instance_catalog_rows(
        folder_idx["mappings"].items(), folder_idx, scope_bookkeeping_col="_Mapping",
    )

    # De-duplicate identical rows (improvement-3). Dedup key includes the
    # bookkeeping _Mapping/_Port columns so legitimately distinct
    # occurrences (same instance name reused across mappings, or a
    # different port on the same instance) are never collapsed together.
    dedup_cols = CATALOG_DISPLAY_COLS + ["_Mapping", "_Port"]
    seen = set()
    deduped = []
    for r in rows:
        key = tuple(r[c] for c in dedup_cols)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    # Sort by Transformation name, then mapping, then the specific port -
    # keeps every mapping's occurrence of a reused instance name grouped
    # together but still distinct.
    return sorted(deduped, key=lambda r: (r["Transformation Name"], r["_Mapping"], r["_Port"]))


# --------------------------------------------------------------------------
# 6b. Mapplet-internal transformation catalog (Tab-4)
# --------------------------------------------------------------------------

# Same shape as CATALOG_DISPLAY_COLS (Tab-2), plus a leading "Mapplet Name"
# column so rows from different mapplet definitions are distinguishable -
# Tab-3 already surfaces "Mapplet_name" per row, so Tab-4 carries the same
# label through for consistency.
MAPPLET_TRANSFORM_CATALOG_DISPLAY_COLS = [
    "Mapplet Name",
    "Transformation Name",
    "Transformation Type",
    "Business Logic",
    "Additional Informations",
    "Input Ports",
    "Output Ports",
    "Custom / Variable Ports",
]


def build_mapplet_transformation_catalog(folder_idx):
    """Tab-4: catalog of every transformation INSTANCE that lives INSIDE a
    Mapplet definition (as opposed to Tab-2, which catalogs transformation
    instances inside Mappings). One row per (mapplet, instance,
    output/variable port) - same "same format as Tab-2" structure, just
    scoped by mapplet instead of by mapping, with the mapplet name carried
    as a visible column so rows are distinguishable across mapplets.

    A mapplet definition is parsed exactly once here regardless of how many
    mappings drop it in as an instance (matches Tab-2's Mapping-level
    granularity: Tab-2 doesn't duplicate itself per session either).
    """

    rows = _build_instance_catalog_rows(
        folder_idx.get("mapplets", {}).items(), folder_idx,
        scope_bookkeeping_col="_Mapplet", visible_scope_col="Mapplet Name",
    )

    # De-duplicate identical rows (improvement-3), mirroring Tab-2's dedup:
    # keyed on the bookkeeping _Mapplet/_Port columns too, so legitimately
    # distinct occurrences are never collapsed together.
    dedup_cols = MAPPLET_TRANSFORM_CATALOG_DISPLAY_COLS + ["_Mapplet", "_Port"]
    seen = set()
    deduped = []
    for r in rows:
        key = tuple(r[c] for c in dedup_cols)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return sorted(deduped, key=lambda r: (r["Mapplet Name"], r["Transformation Name"], r["_Port"]))


# --------------------------------------------------------------------------
# 7. Mapplet catalog (Tab-3)
# --------------------------------------------------------------------------

# Column names copied verbatim from the requirement (columns 6-9 keep their
# inconsistent underscore/slash spelling on purpose, to match exactly).
MAPPLET_CATALOG_DISPLAY_COLS = [
    "Mapping_Name",
    "Mapplet_name",
    "Input Transformation Field",
    "Transformation_lineage",
    "Output Transformation Field",
    "Upstream HOP/Prev_Transformation_name",
    "Upstream_HOP/Prev_Transformation_attribute",
    "Downstream_HOP/Next_Transformation_name",
    "Downstream_HOP/next_Transformation_attribute",
]


def build_mapplet_catalog(folder_idx):
    """Tab-3: one row per (mapping, mapplet instance, input-field ->
    output-field) path discovered INSIDE a Mapplet, stitched to whatever
    real transformation feeds the mapplet's input port and whatever
    consumes its output port at the enclosing mapping level.

    A single output field can legitimately be fed by more than one input
    field (e.g. CONCAT(a, b) inside the mapplet) - each contributing input
    field gets its own row, same "n ways to reach one field" pattern as
    Tab-1.

    Relies on the same Mapplet boundary convention documented on
    _find_boundary_ports / _backward_trace: an "Input Transformation" /
    "Output Transformation" pair exposing the mapplet's interface, with
    port names identical to the mapplet instance's exposed port names in
    the enclosing mapping.
    """
    rows = []
    mapplet_scope_cache = {}

    def get_scope(mapplet_def_name):
        if mapplet_def_name not in mapplet_scope_cache:
            node = folder_idx.get("mapplets", {}).get(mapplet_def_name)
            mapplet_scope_cache[mapplet_def_name] = index_mapping(node) if node is not None else None
        return mapplet_scope_cache[mapplet_def_name]

    for mapping_name, mapping_node in folder_idx["mappings"].items():
        m = index_mapping(mapping_node)

        # reverse (outgoing) connector index for THIS mapping - needed to
        # find whatever consumes a mapplet instance's output port
        outgoing = defaultdict(list)
        for c in m["connectors"]:
            outgoing[(c["FROMINSTANCE"], c["FROMFIELD"])].append(c)

        for inst_name, inst in m["instances"].items():
            mapplet_def = inst.get("transformation_name")
            if mapplet_def not in folder_idx.get("mapplets", {}):
                continue

            scope = get_scope(mapplet_def)
            if scope is None:
                continue

            # The exposed port names used by THIS mapping's CONNECTORs
            # (up_edges / down_edges below) are not guaranteed to match the
            # internal port names used by the mapplet's own Input/Output
            # Transformation boundary (see _get_mapplet_port_maps) -
            # translate both ways or the Upstream/Downstream HOP columns
            # silently come back empty even though the edges exist.
            exposed_to_internal, internal_to_exposed = _get_mapplet_port_maps(folder_idx, mapplet_def)
            out_ports = _find_boundary_ports(scope, folder_idx, MAPPLET_OUTPUT_TYPES)

            for internal_out_field, out_instance in out_ports.items():
                exposed_out_field = internal_to_exposed.get(("output", internal_out_field), internal_out_field)
                inner_leaves = _backward_trace(scope, folder_idx, out_instance, internal_out_field, [], frozenset())

                for leaf in inner_leaves:
                    # Tab-3 documents input->output field lineage through
                    # the mapplet, so only paths that genuinely reach the
                    # mapplet's own input boundary are reportable here.
                    if leaf.get("leaf_type") != "scope_input_boundary":
                        continue

                    internal_in_field = leaf["boundary_field"]
                    exposed_in_field = internal_to_exposed.get(("input", internal_in_field), internal_in_field)
                    in_instance = leaf["boundary_instance"]
                    internal_path = leaf["path"]

                    prev_name = prev_field = prev_type = ""
                    up_edges = m["incoming"].get((inst_name, exposed_in_field), [])
                    if up_edges:
                        e = up_edges[0]
                        prev_name, prev_field, prev_type = e["FROMINSTANCE"], e["FROMFIELD"], e["FROMINSTANCETYPE"]

                    next_name = next_field = next_type = ""
                    down_edges = outgoing.get((inst_name, exposed_out_field), [])
                    if down_edges:
                        e = down_edges[0]
                        next_name = e["TOINSTANCE"]
                        next_field = e["TOFIELD"]
                        next_type = e.get("TOINSTANCETYPE", "")

                    # internal_path always starts (source-first, once reversed
                    # by format_lineage_chain) with the Input Transformation
                    # hop itself - do NOT also prepend it manually here, or it
                    # is duplicated. Only fall back to building it by hand in
                    # the (practically unreachable) case of an empty chain.
                    internal_chain = format_lineage_chain(internal_path)
                    if internal_chain:
                        parts = [internal_chain]
                        # scope="mapplet": these hops live INSIDE mapplet_def,
                        # so a UI click on one of them must jump to Tab-4
                        # (mapplet-internal transformation catalog), not Tab-2.
                        internal_hops = _lineage_chain_hops(
                            internal_path, {"mapplet": mapplet_def, "scope": "mapplet"})
                    else:
                        parts = [f"{in_instance}.{internal_in_field}[Input Transformation]"]
                        internal_hops = [{
                            "name": in_instance, "type": "Input Transformation",
                            "field": internal_in_field, "mapplet": mapplet_def, "scope": "mapplet",
                        }]
                    parts.append(f"{out_instance}.{internal_out_field}[Output Transformation]")
                    internal_hops.append({
                        "name": out_instance, "type": "Output Transformation",
                        "field": internal_out_field, "mapplet": mapplet_def, "scope": "mapplet",
                    })
                    lineage_str = " -> ".join(parts)
                    hops_data = list(internal_hops)
                    if prev_name:
                        lineage_str = f"{prev_name}.{prev_field}[{prev_type}] -> " + lineage_str
                        # scope="mapping": this hop sits OUTSIDE the mapplet,
                        # at the enclosing mapping level, so it routes to
                        # Tab-2 instead - same convention Tab-1 already uses.
                        hops_data.insert(0, {
                            "name": prev_name, "type": prev_type, "field": prev_field,
                            "mapping": mapping_name, "scope": "mapping",
                        })
                    if next_name:
                        lineage_str = lineage_str + f" -> {next_name}.{next_field}[{next_type}]"
                        hops_data.append({
                            "name": next_name, "type": next_type, "field": next_field,
                            "mapping": mapping_name, "scope": "mapping",
                        })

                    rows.append({
                        "Mapping_Name": mapping_name,
                        "Mapplet_name": mapplet_def,
                        "Input Transformation Field": exposed_in_field,
                        "Transformation_lineage": lineage_str,
                        "Output Transformation Field": exposed_out_field,
                        "Upstream HOP/Prev_Transformation_name": prev_name,
                        "Upstream_HOP/Prev_Transformation_attribute": prev_field,
                        "Downstream_HOP/Next_Transformation_name": next_name,
                        "Downstream_HOP/next_Transformation_attribute": next_field,
                        # bookkeeping only, not a display column - lets the
                        # HTML report jump straight from a Tab-1 "Mapplet"
                        # hop (which carries the mapplet INSTANCE name) to
                        # its Tab-3 rows.
                        "_Mapplet_Instance": inst_name,
                        # bookkeeping only, not a display column - feeds the
                        # HTML report's per-hop click routing (Tab-4 for
                        # scope="mapplet" hops, Tab-2 for scope="mapping"
                        # hops), mirroring Tab-1's "Transformation Lineage
                        # Data" column.
                        "_Transformation_lineage_data": json.dumps(hops_data),
                    })

    # De-duplicate identical rows (improvement-3): can arise when more than
    # one internal trace path coincidentally produces the same displayed
    # chain, or the same instance/field combination is reachable more than
    # once. Dedup key includes the bookkeeping _Mapplet_Instance column so
    # legitimately distinct mapplet instances are never collapsed together.
    dedup_cols = MAPPLET_CATALOG_DISPLAY_COLS + ["_Mapplet_Instance"]
    seen = set()
    deduped = []
    for r in rows:
        key = tuple(r[c] for c in dedup_cols)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return sorted(deduped, key=lambda r: (
        r["Mapping_Name"], r["Mapplet_name"],
        r["Output Transformation Field"], r["Input Transformation Field"],
    ))