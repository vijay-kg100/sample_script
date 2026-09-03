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
    # Per-group metadata (Router "Group Filter Condition" etc). Different
    # PowerCenter exports place this either as an extra attribute directly
    # on the GROUP tag, or as a nested TABLEATTRIBUTE child of GROUP - both
    # are captured here under the same group name so callers don't have to
    # care which shape a given export uses.
    group_attrs = defaultdict(dict)
    # Per-port metadata beyond EXPRESSION/PORTTYPE (Sorter "Sort Direction",
    # Rank "Group By" etc). Same dual-shape capture as group_attrs: extra
    # TRANSFORMFIELD attributes and/or nested PORTATTRIBUTE children.
    field_portattrs = defaultdict(dict)

    for f in node["children"]:
        if f["tag"] == "GROUP":
            a = f["attributes"]
            gname = a.get("NAME")
            group_type[gname] = a.get("TYPE", "")
            for k, v in a.items():
                if k not in ("NAME", "TYPE") and v:
                    group_attrs[gname][k] = v
            for gc in f.get("children", []) or []:
                if gc["tag"] in ("TABLEATTRIBUTE", "GROUPATTRIBUTE"):
                    ga = gc["attributes"]
                    group_attrs[gname][ga["NAME"]] = ga.get("VALUE", "")
        if f["tag"] == "TRANSFORMFIELD":
            a = f["attributes"]
            fname = a["NAME"]
            field_expr[fname] = a.get("EXPRESSION") or ""
            field_porttype[fname] = a.get("PORTTYPE", "")
            if a.get("REF_FIELD"):
                field_ref[fname] = a["REF_FIELD"]
            grp = a.get("GROUP")
            if grp:
                field_group[fname] = grp
                group_fields_order[grp].append(fname)
            for k, v in a.items():
                if k not in ("NAME", "EXPRESSION", "PORTTYPE", "REF_FIELD", "GROUP") and v:
                    field_portattrs[fname][k] = v
            for pc in f.get("children", []) or []:
                if pc["tag"] in ("PORTATTRIBUTE", "FIELDATTRIBUTE"):
                    pa = pc["attributes"]
                    field_portattrs[fname][pa["NAME"]] = pa.get("VALUE", "")

    table_attrs = {}
    for f in node["children"]:
        if f["tag"] == "TABLEATTRIBUTE":
            a = f["attributes"]
            table_attrs[a["NAME"]] = a.get("VALUE", "")
    return {
        "type": node["attributes"].get("TYPE", ""),
        "template_name": node["attributes"].get("TEMPLATENAME", ""),
        "fields": field_expr,
        "porttype": field_porttype,
        "ref_field": field_ref,
        "table_attrs": table_attrs,
        "field_group": field_group,
        "group_fields_order": dict(group_fields_order),
        "group_type": group_type,
        "group_attrs": dict(group_attrs),
        "field_portattrs": dict(field_portattrs),
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


# Transformation types whose field-level dependencies are implemented in
# opaque procedural code (Java/user code, an HTTP call, a SQL string built
# at runtime, an external web service, ...) rather than in a per-port
# EXPRESSION, REF_FIELD, or a Union-style positionally-correlated GROUP.
# PowerCenter's export simply has NOTHING at the port level for these -
# an OUTPUT port's TRANSFORMFIELD has a blank EXPRESSION and no REF_FIELD,
# because the real dependency lives inside code text the exporter doesn't
# attempt to parse into port references. A Java Transformation in
# particular is exported with TYPE == "Custom Transformation" (same as a
# structural Custom transform like Union) and only distinguishable by its
# TEMPLATENAME, so both the direct TYPE and the TEMPLATENAME must be
# checked.
_OPAQUE_CODE_TYPES = {
    "java transformation", "http transformation",
    "web services consumer transformation", "sql transformation",
}


def _is_opaque_code_transform(resolved_ttype, tdef):
    """True when `tdef` is a transformation whose per-port lineage cannot
    be derived from EXPRESSION/REF_FIELD/GROUP metadata (see
    _OPAQUE_CODE_TYPES above), so the backward tracer must fall back to
    treating every one of its INPUT ports as a contributor to every one
    of its OUTPUT ports rather than reporting a dead end."""
    if resolved_ttype in _OPAQUE_CODE_TYPES:
        return True
    if resolved_ttype == "custom transformation":
        tmpl = (tdef.get("template_name") or "").strip().lower()
        if "java" in tmpl:
            return True
    return False


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
    # (Informatica correlates union fields positionally within groups).
    # This positional-index correlation is only valid for STRUCTURAL
    # multi-branch transforms like Union, where each branch is a
    # parallel, position-aligned copy of the same field list. It must
    # NOT apply to opaque-code transforms (Java, etc.) even though those
    # also use an INPUT/OUTPUT GROUP shape - a Java transformation's
    # Nth output port has no reason to correspond to its Nth input port
    # (users add/remove/reorder ports independently of each other), so
    # applying positional correlation there would silently attribute a
    # field to the WRONG upstream port whenever the counts happen to
    # line up, which is worse than reporting no lineage at all. Opaque
    # code transforms are handled separately, below.
    field_group = tdef.get("field_group", {})
    group_order = tdef.get("group_fields_order", {})
    group_type = tdef.get("group_type", {})
    is_opaque = _is_opaque_code_transform(resolved_ttype, tdef)
    if not expr and field in field_group and not is_opaque:
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

    # Opaque/procedural transformations (Java Transformation, HTTP
    # Transformation, Web Services Consumer, SQL Transformation, ...):
    # the value on an OUTPUT port here is computed by user code the
    # export doesn't capture at the port level, so there is no
    # EXPRESSION and no REF_FIELD. A Java Transformation DOES reliably
    # carry a GROUP="INPUT"/"OUTPUT" split (exactly two GROUP nodes,
    # every TRANSFORMFIELD tagged into one or the other) - but, per the
    # note above, position within those groups carries no meaning, so
    # every INPUT-group port is treated as a possible contributor to
    # every OUTPUT-group port instead of guessing a 1:1 pairing.
    # Previously this fell straight through to "literal_or_unresolved"
    # below, which silently reported the field as having NO upstream
    # dependency at all - effectively stopping the lineage chain right
    # at the Java instance. Clearly labeled as code-derived so a reader
    # knows the exact port-to-port mapping comes from the
    # transformation's Java code, not from parseable metadata.
    if not expr and not ref_field and is_opaque:
        my_group = field_group.get(field)
        if my_group and group_type.get(my_group) == "OUTPUT":
            input_groups = [g for g, t in group_type.items() if t == "INPUT"]
            input_ports = [f for g in input_groups for f in group_order.get(g, []) if f != field]
        else:
            # GROUP metadata absent/unexpected for this export - fall back
            # to PORTTYPE so behavior still degrades gracefully instead of
            # silently doing nothing.
            input_ports = [f for f, pt in tdef["porttype"].items()
                            if "INPUT" in (pt or "").upper() and "OUTPUT" not in (pt or "").upper()
                            and f != field]
        if input_ports:
            label = f"{instance}[{tdef['type']}:{field}<-CODE(all input ports)]"
            new_path = path + [label]
            results = []
            for ip in input_ports:
                results.extend(_backward_trace(scope_idx, folder_idx, instance, ip, new_path, visited))
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

            # Collapse leaves that share the same real source identity but
            # arrived via different Router/Mapplet-fanout routes - the path
            # variation is combinatorial noise, not distinct lineage.
            if len(result) > _MAX_LEAVES_PER_FIELD:
                by_source = {}
                for leaf in result:
                    dedup_key = (leaf.get("source_instance"), leaf.get("source_table"),
                                 leaf.get("source_field"), leaf.get("leaf_type"))
                    if dedup_key not in by_source:
                        by_source[dedup_key] = {**leaf, "_path_count": 1}
                    else:
                        by_source[dedup_key]["_path_count"] += 1
                result = list(by_source.values())

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
    "PORT_NAME",
    "Transformation Type",
    "Business Logic",
    "Additional Informations",
    "Category",
    "Input Ports",
    "Output Ports",
    "Custom / Variable Ports",
]


def _first_attr(attrs_dict, *candidates):
    """Case-insensitive, whitespace-tolerant lookup: return the first
    non-empty value found in attrs_dict for any of the candidate
    TABLEATTRIBUTE/PORTATTRIBUTE names. PowerCenter exports are not fully
    consistent about exact spelling/casing of these attribute names, so
    every per-type lookup below tries a short list of likely spellings
    rather than a single hardcoded key."""
    if not attrs_dict:
        return ""
    lower_map = {str(k).strip().lower(): v for k, v in attrs_dict.items() if v}
    for c in candidates:
        v = lower_map.get(c.strip().lower())
        if v:
            return v
    return ""


def _category_for_port(own_expr):
    """Shared Category rule for every transformation type: a port whose
    own EXPRESSION is non-blank has logic implemented on it ("Involves
    Derivation"); a bare pass-through port ("Direct Pass through")."""
    return "Involves Derivation" if own_expr else "Direct Pass through"


def _business_logic_and_additional_info(ttype, tdef, port_name, existing_logic_str):
    """Per-transformation-type Business Logic / Additional Informations,
    per the Tab-2/Tab-4 spec. Types without a special case below (or an
    export missing the expected attribute) fall back to the existing
    generic per-port EXPRESSION-trace text already computed by the caller,
    so the columns are never silently blanked out.
    """
    ta = tdef.get("table_attrs", {})
    t = (ttype or "").upper()

    def attr(*names):
        return _first_attr(ta, *names)

    # Expression / Aggregator: keep the existing computed value as-is.
    if "EXPRESSION" in t or "AGGREGATOR" in t:
        return existing_logic_str, ""

    if "FILTER" in t:
        return attr("Filter Condition") or existing_logic_str, ""

    if "JOINER" in t:
        return (attr("Join Condition") or existing_logic_str,
                attr("Join Type"))

    if "LOOKUP" in t:
        cond = attr("Lookup Condition")
        sql = attr("Lookup Sql Override", "Lookup Sql Overide")
        table = attr("Lookup table name", "Lookup Table Name")
        parts = []
        if cond:
            parts.append(f"Lookup Condition: {cond}")
        if sql:
            parts.append(f"Lookup Sql Override: {sql}")
        if table:
            parts.append(f"Lookup Table Name: {table}")
        return ("\n".join(parts) if parts else existing_logic_str,
                attr("Connection Information"))

    if "ROUTER" in t:
        grp = tdef.get("field_group", {}).get(port_name, "")
        grp_attrs = tdef.get("group_attrs", {}).get(grp, {})
        cond = _first_attr(grp_attrs, "Group Filter Condition", "Filter Condition", "Condition")
        biz_parts = [f"Group Name: {grp}"] if grp else []
        if cond:
            biz_parts.append(f"Group Filter Condition: {cond}")
        return ("\n".join(biz_parts) if biz_parts else existing_logic_str, "")

    if "SEQUENCE" in t:
        biz = attr("Current Value")
        addl_parts = []
        for label in ("Start Value", "Increment Value", "End Value"):
            v = attr(label)
            if v:
                addl_parts.append(f"{label}: {v}")
        return (biz or existing_logic_str, "; ".join(addl_parts))

    if "SORTER" in t:
        port_attrs = tdef.get("field_portattrs", {}).get(port_name, {})
        direction = _first_attr(port_attrs, "Sort Direction") or attr("Sort Direction")
        scope = attr("Transformation Scope")
        return (direction or existing_logic_str, scope)

    if "SOURCE QUALIFIER" in t or t == "SQ":
        biz = attr("Sql Query", "Sql Override")
        addl_parts = []
        uj = attr("User Defined Join")
        sf = attr("Source Filter")
        assoc = attr("Associated Source Instance", "Associated Source Definitions", "Source Table Name")
        if uj:
            addl_parts.append(f"User Defined Join: {uj}")
        if sf:
            addl_parts.append(f"Source Filter: {sf}")
        if assoc:
            addl_parts.append(f"Associated source definitions: {assoc}")
        return (biz or existing_logic_str, "; ".join(addl_parts))

    if "STORED PROCEDURE" in t:
        biz = attr("Stored Procedure Name")
        addl_parts = [
            f"{k}: {v}" for k, v in ta.items()
            if v and k.strip().lower() != "stored procedure name"
        ]
        return (biz or existing_logic_str, "; ".join(addl_parts))

    if "CUSTOM TRANSFORMATION" in t:
        tmpl = (tdef.get("template_name") or "").strip()
        if "java" in tmpl.lower():
            # PowerCenter exports a Java Transformation's actual source as
            # a compiled plugin, not as text - there's no "Java Code"
            # table attribute to surface (confirmed against a real
            # export). What IS available is the plugin Class Name plus a
            # handful of behavioral flags, so put those in Business
            # Logic/Additional Info instead of leaving both blank.
            class_name = attr("Class Name")
            biz = (f"Logic implemented in Java code (class: {class_name})"
                   if class_name else "Logic implemented in Java code (not parseable from metadata)")
            addl_parts = [f"{k}: {v}" for k, v in ta.items()
                          if v and k.strip().lower() not in ("class name", "language")]
            return biz, "; ".join(addl_parts)
        return existing_logic_str, tmpl

    if "TRANSACTION" in t:
        return attr("Transaction Control Condition") or existing_logic_str, ""

    if "UPDATE STRATEGY" in t:
        return attr("Update Strategy Expression") or existing_logic_str, ""

    if "RANK" in t:
        top_bottom = attr("Top/Bottom", "Top Bottom")
        num_ranks = attr("Number Of Ranks", "Number of Ranks")
        if top_bottom or num_ranks:
            biz = f"Rank={top_bottom}, number of Ranks={num_ranks}"
        else:
            biz = existing_logic_str
        addl = attr("Case-Sensitive String Comparison", "Case Sensitive String Comparison")
        return biz, addl

    # Unrecognized/unlisted transformation type: keep existing behaviour.
    return existing_logic_str, ""


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

                generic_business_logic = "\n".join(logic_lines)
                business_logic, additional_info = _business_logic_and_additional_info(
                    ttype, tdef, port_name, generic_business_logic)
                category = _category_for_port(own_expr)

                row = {
                    "Transformation Name": inst_name,
                    "PORT_NAME": port_name,
                    "Transformation Type": ttype,
                    "Business Logic": business_logic,
                    "Additional Informations": additional_info,
                    "Category": category,
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
    "PORT_NAME",
    "Transformation Type",
    "Business Logic",
    "Additional Informations",
    "Category",
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


# --------------------------------------------------------------------------
# 8. Eligibility Rules catalog (new tab)
# --------------------------------------------------------------------------
#
# Scans the same per-instance catalogs already built for Tab-2 (mapping
# level) and Tab-4 (mapplet-internal level) and pulls out just the rows
# that look like eligibility/qualification logic, per the "Where to look"
# spec:
#   Filter            -> filter condition
#   Router            -> each group's condition
#   Expression        -> IIF/DECODE/CASE-like logic on flag/indicator ports
#   Lookup            -> lookup override SQL / lookup condition
#   Update Strategy   -> conditional insert/update/reject logic
#   Source Qualifier  -> SQL override / source filter WHERE clause
#   Mapplets          -> same checks, recursively, on the mapplet's own
#                        internal transformations (Tab-4), attributed back
#                        to every mapping that calls the mapplet.
#
# Filter/Router/Lookup/Update Strategy/Source Qualifier are inherently
# row-gating constructs, so any non-blank logic on them is taken as an
# eligibility-rule candidate. Expression (and Joiner, which can implicitly
# restrict which rows survive a join) are far noisier - most Expression
# ports have nothing to do with eligibility - so those are only pulled in
# when the port name or its logic text hits an eligibility-flavoured
# keyword below.

ELIGIBILITY_GATING_TTYPES = (
    "FILTER", "ROUTER", "LOOKUP", "UPDATE STRATEGY", "SOURCE QUALIFIER",
)
ELIGIBILITY_CONDITIONAL_TTYPES = ("EXPRESSION", "JOINER")

ELIGIBILITY_KEYWORDS = (
    "ELIGIB", "QUALIF", "ENTITL", "VALID", "CRITERIA", "INCLUDE", "EXCLUDE",
    "REJECT", "DISQUALIF", "APPROV", "DENY", "ACCEPT", "ACTIVE", "STATUS",
    "IND", "FLAG",
)

ELIGIBILITY_CATALOG_DISPLAY_COLS = [
    "Session",
    "Mapping/Mapplet",
    "Transformation Name",
    "Transformation Type",
    "Eligibility Rule/Logic (Technical)",
    "Eligibility Rule/Logic (Plain Language)",
    "Source (Excel/XML)",
]

ELIGIBILITY_SUMMARY_DISPLAY_COLS = [
    "Session",
    "Mapping/Mapplet",
    "Eligibility Rules/Logics",
]


def _is_eligibility_row(ttype, port_name, business_logic):
    if not business_logic or not business_logic.strip():
        return False
    t = (ttype or "").upper()
    if any(g in t for g in ELIGIBILITY_GATING_TTYPES):
        return True
    if any(g in t for g in ELIGIBILITY_CONDITIONAL_TTYPES):
        haystack = f"{port_name or ''} {business_logic or ''}".upper()
        return any(kw in haystack for kw in ELIGIBILITY_KEYWORDS)
    return False


# Best-effort, rule-based technical -> plain-language rewrite. This is NOT
# a semantic parser - it just expands common PowerCenter expression tokens
# into words so a non-technical reader gets the gist. Anything it can't
# confidently rewrite is left as-is; always keep the Technical column as
# the source of truth.
_PLAIN_LANG_REPLACEMENTS = [
    (re.compile(r"\bIIF\s*\(", re.IGNORECASE), "IF ("),
    (re.compile(r"\bISNULL\s*\(", re.IGNORECASE), "IS NULL("),
    (re.compile(r"\bIS_SPACES\s*\(", re.IGNORECASE), "IS BLANK("),
    (re.compile(r"!=\s*"), " is not equal to "),
    (re.compile(r"<>\s*"), " is not equal to "),
    (re.compile(r"\bAND\b", re.IGNORECASE), "AND"),
    (re.compile(r"\bOR\b", re.IGNORECASE), "OR"),
    (re.compile(r"\bNOT\b", re.IGNORECASE), "NOT"),
    # Multi-char comparison operators MUST be replaced before the bare
    # "=", ">", "<" patterns below, or e.g. ">=" gets its "=" consumed
    # first and misreads as "greater than" + "equals" (two phrases
    # instead of one "at least").
    (re.compile(r">="), " is at least "),
    (re.compile(r"<="), " is at most "),
    (re.compile(r"=(?!=)"), " equals "),
    (re.compile(r"(?<![<>=!])>(?!=)"), " is greater than "),
    (re.compile(r"(?<![<>=!])<(?!=)"), " is less than "),
]


def plain_language_translate(expr):
    """Rule-based translation of a technical condition/expression into a
    rough plain-English rendering. Best-effort only - review before
    treating as a business-approved definition."""
    if not expr:
        return ""
    text = expr
    for pattern, repl in _PLAIN_LANG_REPLACEMENTS:
        text = pattern.sub(repl, text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def find_mappings_using_mapplet(folder_idx, mapplet_def_name):
    """Every mapping (by name) that drops in an INSTANCE of the given
    mapplet definition. A mapplet can legitimately be called from several
    mappings, so this returns a (possibly multi-item) sorted list."""
    callers = set()
    for mapping_name, mapping_node in folder_idx["mappings"].items():
        for ch in mapping_node["children"]:
            if ch["tag"] == "INSTANCE":
                a = ch["attributes"]
                if a.get("TRANSFORMATION_NAME") == mapplet_def_name and \
                        mapplet_def_name in folder_idx.get("mapplets", {}):
                    callers.add(mapping_name)
                    break
    return sorted(callers)


def build_eligibility_catalog(folder_idx, session_to_mapping, source_label="XML"):
    """Folder-wide eligibility-rules catalog, per the 7-column spec.

    session_to_mapping: {session_name: mapping_name} - used only to map a
    mapping back to the session(s) that run it. Pass the FULL session map
    (not just an anchor-restricted subset) if eligibility documentation
    should cover the whole repository regardless of any single target's
    lineage scope.
    source_label: value written into "Source (Excel/XML)" for every row.
    This pipeline only ever sees XML-derived JSON (no Excel detail-tab
    pass), so every row is uniformly stamped "XML" unless a caller
    supplies something else (e.g. after merging in Excel-tab findings).
    """
    mapping_sessions = defaultdict(list)
    for sess, mp in (session_to_mapping or {}).items():
        mapping_sessions[mp].append(sess)
    for mp in mapping_sessions:
        mapping_sessions[mp].sort()

    def sessions_for(mapping_name):
        sess = mapping_sessions.get(mapping_name, [])
        return "; ".join(sess) if sess else "(no session found)"

    rows = []

    # -- Mapping-level transformations (Filter/Router/Expression/Lookup/
    #    Update Strategy/Source Qualifier living directly inside a mapping)
    for r in build_transformation_catalog(folder_idx):
        if not _is_eligibility_row(r["Transformation Type"], r["PORT_NAME"], r["Business Logic"]):
            continue
        mapping_name = r["_Mapping"]
        rows.append({
            "Session": sessions_for(mapping_name),
            "Mapping/Mapplet": mapping_name,
            "Transformation Name": r["Transformation Name"],
            "Transformation Type": r["Transformation Type"],
            "Eligibility Rule/Logic (Technical)": r["Business Logic"],
            "Eligibility Rule/Logic (Plain Language)": plain_language_translate(r["Business Logic"]),
            "Source (Excel/XML)": source_label,
        })

    # -- Mapplet-internal transformations, attributed once per calling
    #    mapping (a mapplet used by several mappings gets its logic
    #    captured once per mapping that calls it, per spec).
    for r in build_mapplet_transformation_catalog(folder_idx):
        if not _is_eligibility_row(r["Transformation Type"], r["PORT_NAME"], r["Business Logic"]):
            continue
        mapplet_name = r["Mapplet Name"]
        callers = find_mappings_using_mapplet(folder_idx, mapplet_name)
        if not callers:
            callers = ["(no calling mapping found)"]
        for calling_mapping in callers:
            label = f"{calling_mapping} -> {mapplet_name}" if calling_mapping != "(no calling mapping found)" \
                else f"(unresolved) -> {mapplet_name}"
            sess = sessions_for(calling_mapping) if calling_mapping != "(no calling mapping found)" \
                else "(no session found)"
            rows.append({
                "Session": sess,
                "Mapping/Mapplet": label,
                "Transformation Name": r["Transformation Name"],
                "Transformation Type": r["Transformation Type"],
                "Eligibility Rule/Logic (Technical)": r["Business Logic"],
                "Eligibility Rule/Logic (Plain Language)": plain_language_translate(r["Business Logic"]),
                "Source (Excel/XML)": source_label,
            })

    # De-duplicate exact repeats, then sort for stable, readable output.
    seen = set()
    deduped = []
    for r in rows:
        key = tuple(r[c] for c in ELIGIBILITY_CATALOG_DISPLAY_COLS)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    return sorted(deduped, key=lambda r: (r["Session"], r["Mapping/Mapplet"], r["Transformation Name"]))


def build_eligibility_summary(eligibility_rows):
    """Collapses the full Eligibility Rules catalog into the optional
    3-column "Eligibility Rules - Summary" view: one row per
    (Session, Mapping/Mapplet), with every rule found for that combination
    concatenated as a bullet list in the last column."""
    grouped = defaultdict(list)
    order = []
    for r in eligibility_rows:
        key = (r["Session"], r["Mapping/Mapplet"])
        if key not in grouped:
            order.append(key)
        bullet = f"- [{r['Transformation Type']}] {r['Transformation Name']}: {r['Eligibility Rule/Logic (Technical)']}"
        grouped[key].append(bullet)

    return [
        {
            "Session": sess,
            "Mapping/Mapplet": mp,
            "Eligibility Rules/Logics": "\n".join(grouped[(sess, mp)]),
        }
        for sess, mp in order
    ]