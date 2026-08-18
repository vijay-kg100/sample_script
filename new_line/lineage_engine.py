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

        elif tag == "WORKFLOW":
            workflows[name] = ch

    return {
        "sources": sources,
        "targets": targets,
        "transformations": transformations,
        "mappings": mappings,
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
# 4. Backward field tracer (within one mapping)
# --------------------------------------------------------------------------

class MappingTracer:
    def __init__(self, folder_idx, mapping_name):
        self.folder_idx = folder_idx
        self.mapping_name = mapping_name
        self.mapping_node = folder_idx["mappings"][mapping_name]
        self.m = index_mapping(self.mapping_node)

    def resolve_transformation(self, name):
        if name in self.m["local_transformations"]:
            return self.m["local_transformations"][name]
        return self.folder_idx["transformations"].get(name)

    def instance_table_name(self, instance_name):
        """Physical table name behind a SOURCE or TARGET instance."""
        inst = self.m["instances"].get(instance_name)
        if not inst:
            return instance_name
        tname = inst["transformation_name"]
        if inst["type"] == "SOURCE":
            return self.folder_idx["sources"].get(tname, {}).get("table", tname)
        if inst["type"] == "TARGET":
            return self.folder_idx["targets"].get(tname, {}).get("table", tname)
        return tname

    def trace_field(self, instance, field, path=None, visited=None):
        """Backward-trace one (instance, field). Returns a list of leaf dicts:
           {source_instance, source_table, source_field, path: [...], leaf_type}
        """
        if path is None:
            path = []
        if visited is None:
            visited = frozenset()

        key = (instance, field)
        if key in visited:
            return []
        visited = visited | {key}

        edges = self.m["incoming"].get((instance, field), [])

        if edges:
            results = []
            for e in edges:
                fi, ff, ftype = e["FROMINSTANCE"], e["FROMFIELD"], e["FROMINSTANCETYPE"]
                if ftype == "Source Definition":
                    results.append({
                        "source_instance": fi,
                        "source_table": self.instance_table_name(fi),
                        "source_field": ff,
                        "path": list(path),
                        "leaf_type": "source",
                    })
                else:
                    label = f"{fi}[{ftype}]"
                    new_path = path + [label] if (not path or path[-1] != label) else path
                    results.extend(self.trace_field(fi, ff, new_path, visited))
            return results

        # --- no incoming CONNECTOR edge: value is derived within this
        # instance's own transformation logic (indirect / lookup / literal)
        inst = self.m["instances"].get(instance)
        if not inst or inst["type"] not in ("TRANSFORMATION",):
            # dead end: e.g. a source field with genuinely no upstream,
            # or unresolved instance
            return [{
                "source_instance": None, "source_table": None,
                "source_field": None, "path": list(path),
                "leaf_type": "unmapped",
            }]

        tdef = self.resolve_transformation(inst["transformation_name"])
        if tdef is None:
            return [{
                "source_instance": None, "source_table": None,
                "source_field": None, "path": list(path),
                "leaf_type": "unresolved_transformation",
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
            return self.trace_field(instance, ref_field, path + [label], visited)

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
                        results.extend(self.trace_field(instance, bf, new_path, visited))
                    if results:
                        return results

        if "LOOKUP" in porttype and not expr:
            lkp_table = tdef["table_attrs"].get("Lookup table name", inst["transformation_name"])
            label = f"{instance}[Lookup:{inst['transformation_name']}]"
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
            lkp_def = self.resolve_transformation(lkp_name)
            lkp_table = (lkp_def["table_attrs"].get("Lookup table name", lkp_name)
                         if lkp_def else lkp_name)
            lkp_label = f"{lkp_name}[Unconnected Lookup->table:{lkp_table}]"
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
                    results.extend(self.trace_field(instance, arg_tok, new_path, visited))

        for ref in refs:
            if ref != field:
                results.extend(self.trace_field(instance, ref, new_path, visited))

        if not results:
            # pure literal / constant expression (e.g. SYSDATE, a string
            # literal, or NEXTVAL handled elsewhere) - nothing further
            # upstream to trace
            results.append({
                "source_instance": None, "source_table": None,
                "source_field": None, "path": new_path,
                "leaf_type": "literal_or_unresolved",
            })
        return results
