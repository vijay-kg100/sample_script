"""Target Field-centric lineage & business-logic extraction engine.

Implements the processing logic described in
`Informatica_Target_Field_Lineage_Prompt.md`:
  - resolve a Workflow / Session / Mapping identifier to a Mapping,
  - locate the selected Target Table (handling the "appears more than
    once" case by requiring a Transformation Instance),
  - for every field on that Target instance, recursively trace the
    producing transformation(s), the business logic/expression involved,
    and every field referenced by that logic, all the way back to the
    Source Qualifier / original source fields (and, best-effort, across
    session boundaries when a source table matches an earlier session's
    target table),
  - return one row per lineage hop, ready to be written to the
    "Field_Lineage" worksheet by field_lineage_excel_generator.

No LLM / heuristic inference is used: every hop is derived from explicit
CONNECTOR edges and TRANSFORMFIELD/EXPRESSION metadata already present in
the parsed RepositoryModel, consistent with the rest of the analyzer
package.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from app.analyzer.workflow_analyzer import compute_execution_order
from app.models.domain import index_instances, resolve_instance

NONE_LABEL = "(none)"
MAX_HOPS = 60


# --------------------------------------------------------------- resolve ---

def resolve_identifier_to_mappings(repo, identifier: str) -> List[str]:
    """Resolve a Workflow / Session / Mapping name (case-insensitive) to
    the list of candidate mapping names it refers to. A Mapping or Session
    name always resolves to exactly one mapping; a Workflow name resolves
    to every mapping used by that workflow's sessions."""
    identifier = (identifier or "").strip()
    if not identifier:
        return []
    wf = repo.workflow

    if identifier in repo.mappings:
        return [identifier]
    if wf and identifier in wf.sessions:
        m_name = wf.sessions[identifier].mapping_name
        return [m_name] if m_name in repo.mappings else []
    if wf and identifier == wf.name:
        return sorted({s.mapping_name for s in wf.sessions.values() if s.mapping_name in repo.mappings})

    low = identifier.lower()
    exact_map = [m for m in repo.mappings if m.lower() == low]
    if exact_map:
        return exact_map
    if wf:
        exact_sess = [s for s in wf.sessions if s.lower() == low]
        if exact_sess:
            m_name = wf.sessions[exact_sess[0]].mapping_name
            return [m_name] if m_name in repo.mappings else []
        if wf.name.lower() == low:
            return sorted({s.mapping_name for s in wf.sessions.values() if s.mapping_name in repo.mappings})
    return []


def session_for_mapping(repo, mapping_name: str) -> Optional[str]:
    """First session (in execution order when available) whose SESSION
    references this mapping, or None if no session references it."""
    wf = repo.workflow
    if wf is None:
        return None
    order = wf.execution_order or compute_execution_order(repo)
    ordered_session_names = [t for t in order if t in wf.sessions] or list(wf.sessions.keys())
    for name in ordered_session_names:
        if wf.sessions[name].mapping_name == mapping_name:
            return name
    return None


def find_target_instances(repo, mapping_name: str, target_table: str):
    """All TARGET instances in the mapping whose underlying table matches
    `target_table` (case-insensitive)."""
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return []
    low = (target_table or "").strip().lower()
    return [i for i in mapping.instances if i.type == "TARGET" and (i.ref_name or "").lower() == low]


def producing_transformation_names(repo, mapping_name: str, target_instance_name: str) -> List[str]:
    """Immediate upstream transformation instance names feeding a given
    Target instance -- shown to the user as 'Transformation Names where
    the Target Table is used' so they can tell instances apart (e.g. an
    insert branch vs. an update branch coming out of a Router)."""
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return []
    names = sorted({c.from_instance for c in mapping.connectors if c.to_instance == target_instance_name})
    return names


def target_fields_for_instance(repo, mapping_name: str, target_instance_name: str) -> List[str]:
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return []
    inst = next((i for i in mapping.instances
                 if i.name == target_instance_name and i.type == "TARGET"), None) \
        or next((i for i in mapping.instances if i.name == target_instance_name), None)
    if inst is None:
        return []
    table = repo.targets.get(inst.ref_name)
    if table is None:
        return []
    return [f.name for f in table.fields]


# ----------------------------------------------------------------- trace ---

@dataclass
class _Ctx:
    kind: str          # "mapping" | "mapplet"
    name: str           # mapping name or mapplet name
    inst_by_name: dict
    pred_index: dict     # (to_instance, to_field) -> [(from_instance, from_field), ...]
    inst_by_name_type: dict = dc_field(default_factory=dict)   # (name, type) -> Instance, collision-safe
    edge_from_type: dict = dc_field(default_factory=dict)      # (to_inst,to_field,from_inst,from_field) -> from type

    def resolve(self, instance_name: str, type_hint: str = ""):
        """Collision-safe instance lookup. Prefer the (name, type_hint) exact
        match when a type hint is available (normally read off the connector
        edge that led here); otherwise falls back to plain name lookup,
        identical to the old behaviour."""
        return resolve_instance(self.inst_by_name, self.inst_by_name_type, instance_name, type_hint)


def _build_ctx(repo, kind: str, name: str) -> Optional[_Ctx]:
    if kind == "mapping":
        obj = repo.mappings.get(name)
    else:
        obj = repo.mapplets.get(name)
    if obj is None:
        return None
    inst_by_name, inst_by_name_type = index_instances(obj.instances)
    pred_index = defaultdict(list)
    edge_from_type = {}
    for c in obj.connectors:
        pred_index[(c.to_instance, c.to_field)].append((c.from_instance, c.from_field))
        if c.from_instance_type:
            edge_from_type[(c.to_instance, c.to_field, c.from_instance, c.from_field)] = c.from_instance_type
    return _Ctx(kind, name, inst_by_name, pred_index, inst_by_name_type, edge_from_type)


def _get_transformation(repo, ctx: _Ctx, ref_name: str):
    if ctx.kind == "mapping":
        return repo.transformation(ctx.name, ref_name)
    return (repo.transformations.get(f"MAPPLET::{ctx.name}::{ref_name}")
            or repo.reusable_transformations.get(ref_name))


def _referenced_ports(tobj, field_name: str, expr: str) -> List[str]:
    """Field names (input/variable ports of the same transformation) that
    the expression text for `field_name` actually references, used to
    recurse 'inside' an Expression/Aggregator/etc. output port per the
    prompt's 'parse the business logic/expression, extract all referenced
    fields' step."""
    if not expr or tobj is None:
        return []
    candidates = [p.name for p in (tobj.input_ports + tobj.variable_ports) if p.name and p.name != field_name]
    candidates.sort(key=len, reverse=True)  # longer names first avoids short-name false positives
    found = []
    for name in candidates:
        if re.search(r"\b" + re.escape(name) + r"\b", expr):
            found.append(name)
    return found


# ---------------------------------------------------- Group-port fan-in/out ---
# Custom Transformations (Java, SQL Transformation, HTTP Transformation, and
# the built-in Union transformation -- which PowerCenter exports as a Custom
# Transformation wrapping "pmuniontrans") carry no per-port
# EXPRESSION/associated-port metadata at all, so the two normal producer
# lookups above (an exact-name CONNECTOR, or a port referenced inside an
# expression) both come up empty even when the port is genuinely wired to
# upstream data. Without a fallback, the trace dead-ends there and reports
# "no upstream connector", which is wrong -- the real first primary Source is
# further back. The same blind spot also hits ordinary Router/Normalizer
# transformations, which fan a single bare input port out to one
# digit-suffixed output port per group/occurrence (e.g. input 'SOURCENAME'
# feeds outputs 'SOURCENAME2', 'SOURCENAME11', ...) -- the mirror image of
# Union's fan-in. The helpers below recover both directions, in order of
# precision, and are shared by every tracer/graph-builder in this module.

_GROUP_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def _group_variant_predecessors(ctx: "_Ctx", instance_name: str, field_name: str, tobj) -> List[Tuple[str, str]]:
    """Fallback #1 -- precise, no-ambiguity port-name matching for
    multi-group transformations whose XML carries no per-port
    EXPRESSION/associated-port metadata at all. Handles both directions:

    - Fan-IN (Union): one same-role input port per input group, suffixed
      with the group number -- output 'REPORTINGID' <- inputs
      'REPORTINGID2', 'REPORTINGID3', ... with no connector into the bare
      output-named port itself.
    - Fan-OUT (Router, Normalizer): a single bare input port feeds one
      digit-suffixed output port per group/occurrence -- output
      'SOURCENAME2' <- input 'SOURCENAME', with no connector into the
      suffixed output port itself.

    Matches by stripping a trailing digit run off whichever side has one
    and comparing the resulting base name to the other side."""
    if tobj is None:
        return []

    def _base(name: str) -> Optional[str]:
        m = _GROUP_SUFFIX_RE.match(name)
        return m.group(1) if m else None

    field_base = _base(field_name)  # set only if field_name itself carries a group suffix

    preds: List[Tuple[str, str]] = []
    for p in tobj.input_ports:
        if p.name == field_name:
            continue
        p_base = _base(p.name)
        is_fan_in = p_base == field_name  # input carries the suffix, output is the bare base
        is_fan_out = field_base is not None and p.name == field_base  # output carries the suffix, input is bare
        # Deliberately NOT matching "both sides carry some suffix against a shared base" --
        # Joiner/Union upstream routinely auto-renames a colliding port by appending a digit
        # (e.g. SOURCENAME vs SOURCENAME1 from a join's master/detail pipelines), which looks
        # identical to a group-suffix pattern but is actually a different field entirely.
        # Only the two unambiguous, single-sided directions above are safe to infer.
        if not (is_fan_in or is_fan_out):
            continue
        for pair in ctx.pred_index.get((instance_name, p.name), []):
            if pair not in preds:
                preds.append(pair)
    return preds


def _opaque_custom_predecessors(ctx: "_Ctx", instance_name: str, ttype: str, tobj) -> List[Tuple[str, str]]:
    """Fallback #2 -- best-effort, for any Custom Transformation (by type,
    not just Union) where fallback #1 found nothing either -- e.g. a Java,
    SQL, HTTP, or other procedural Custom Transformation whose internal
    input-to-output port mapping simply isn't exposed in the exported
    metadata at all. Rather than silently dropping the real upstream path,
    every connected input port on this instance is treated as a possible
    producer, so the reported lineage stays complete even though it can no
    longer say precisely which input(s) feed this specific output."""
    if tobj is None or "custom" not in (ttype or "").lower():
        return []
    preds: List[Tuple[str, str]] = []
    for p in tobj.input_ports:
        for pair in ctx.pred_index.get((instance_name, p.name), []):
            if pair not in preds:
                preds.append(pair)
    return preds


def resolve_custom_transformation_predecessors(ctx: "_Ctx", instance_name: str, field_name: str, ttype: str,
                                                tobj) -> Tuple[List[Tuple[str, str]], bool]:
    """Applies both Custom Transformation fallbacks above, in order of
    precision. Only called once the normal exact-connector and
    expression-reference lookups have both come up empty, so it never
    changes lineage that already resolved correctly. Returns
    (predecessors, opaque) where `opaque` is True only when fallback #2
    (the black-box, all-connected-inputs case) fired, so callers can
    annotate the logic text to keep that ambiguity visible."""
    variant = _group_variant_predecessors(ctx, instance_name, field_name, tobj)
    if variant:
        return variant, False
    opaque_preds = _opaque_custom_predecessors(ctx, instance_name, ttype, tobj)
    if opaque_preds:
        return opaque_preds, True
    return [], False


# ------------------------------------------------------ Direct pass-through ---
# Per-transformation-type classification of "Direct pass-through" vs. "real
# business logic", used by both flat tracers (Target Field Lineage and
# Reportability). Overrides the generic expression-based heuristic for the
# transformation types that carry an unambiguous, purpose-built signal in
# their exported metadata; falls back to the generic heuristic otherwise.

def _classify_is_real_crossing(ttype: str, field_name: str, tobj, expr: str, ref_ports: List[str],
                                generic_is_real_crossing: bool) -> bool:
    """Returns True when this hop is real business logic (NOT a Direct
    pass-through). `generic_is_real_crossing` is the existing
    expression-shape heuristic (functions/operators/multiple fields vs. a
    bare rename), used as-is for any transformation type without a more
    specific rule below, and as the fallback within a rule when its own
    specific signal is absent/inconclusive."""
    t = (ttype or "").lower()
    attrs = tobj.attributes if tobj else {}

    if "expression" in t:
        # Direct pass-through only when the very same attribute is passed through
        # unchanged, or a single OTHER (non-variable) port is renamed with no
        # function/operator applied. Any variable port or real expression -> logic.
        if not expr:
            return False
        stripped = expr.strip()
        if stripped == field_name.strip():
            return False
        if len(ref_ports) == 1 and stripped == ref_ports[0].strip():
            var_names = {p.name for p in (tobj.variable_ports if tobj else [])}
            return ref_ports[0] in var_names
        return True

    if "source qualifier" in t:
        # Direct pass-through unless a custom SQL override Query is present.
        return bool(attrs.get("Sql Query", "").strip())

    if t == "filter":
        # A Filter transformation applies its condition to every row it passes,
        # so any real Filter Condition means this is not a plain pass-through.
        return bool(attrs.get("Filter Condition", "").strip())

    if "aggregator" in t:
        # A port marked GROUP BY changes row granularity -- real logic, even if
        # its own expression is a bare copy of the input.
        port = next((p for p in (tobj.output_ports + tobj.input_ports) if p.name == field_name), None) \
            if tobj else None
        if port is not None and (port.expression_type or "").upper() == "GROUPBY":
            return True
        return generic_is_real_crossing

    if "lookup" in t:
        # Not a pass-through if this attribute (or any other) is referenced in
        # the Lookup Condition or a Lookup SQL Override -- the lookup's outcome
        # for every row then depends on it.
        haystack = f"{attrs.get('Lookup condition', '')} {attrs.get('Lookup Sql Override', '')}"
        if haystack.strip() and re.search(r"\b" + re.escape(field_name) + r"\b", haystack, re.IGNORECASE):
            return True
        return generic_is_real_crossing

    return generic_is_real_crossing


def _entry_into_mapplet(repo, mapplet_name: str, field_name: str) -> Optional[str]:
    """Best-effort resolution of a Mapplet's exposed output field back to
    the internal transformation instance that actually produces it
    (preferring an instance whose type mentions 'Output')."""
    mplt = repo.mapplets.get(mapplet_name)
    if mplt is None:
        return None
    candidates = []
    for inst in mplt.instances:
        if inst.type != "TRANSFORMATION":
            continue
        tobj = (repo.transformations.get(f"MAPPLET::{mapplet_name}::{inst.ref_name}")
                or repo.reusable_transformations.get(inst.ref_name))
        if tobj and any(p.name == field_name for p in tobj.output_ports):
            candidates.append(inst)
    if not candidates:
        return None
    preferred = [i for i in candidates if "output" in (i.ref_type or "").lower()]
    return (preferred[0] if preferred else candidates[0]).name


def _row(mapping_name, mapplet, session, source_table, source_field, target_table, target_field,
         transformation, ttype, full_path, individual_logic, links, hop_count):
    return {
        "Mapping": mapping_name,
        "Mapplet": mapplet or NONE_LABEL,
        "Session": session or NONE_LABEL,
        "Source Table": source_table or "",
        "Source Field": source_field or "",
        "Target Table": target_table,
        "Target Field": target_field,
        "Transformation": transformation,
        "Transformation Type": ttype or "",
        "Transformation_Full_Lineage_Path": full_path,
        "Individual_Transformation": individual_logic or "",
        "Links": links or "",
        "Hop Count": hop_count,
    }


def trace_target_field(repo, mapping_name: str, session_name: Optional[str],
                        target_instance_name: str, target_field_name: str) -> List[Dict]:
    """Recursively traces one Target Field back through every direct and
    indirect producer -- expressions, lookups, routers, joins,
    aggregations, mapplets, previous-session dependencies -- down to the
    Source Qualifier / original source field(s). Returns one row per hop,
    per `## Workbook Output` in the prompt."""
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return []
    # This function traces a *Target* field. If the name collides with
    # another instance (e.g. a same-named Source elsewhere on the canvas),
    # prefer the TARGET-typed one -- picking whichever instance happened to
    # come first in the export, regardless of type, used to silently grab
    # the wrong object and dead-end the trace.
    target_inst = next((i for i in mapping.instances
                         if i.name == target_instance_name and i.type == "TARGET"), None) \
        or next((i for i in mapping.instances if i.name == target_instance_name), None)
    if target_inst is None:
        return []
    target_table = target_inst.ref_name
    target_label = f"{target_instance_name}.{target_field_name}"

    rows: List[Dict] = []
    visited = set()  # (ctx.kind, ctx.name, instance, field) - cycle guard for this one target field

    def walk(ctx: _Ctx, mapplet_label: str, instance_name: str, field_name: str, hop: int,
             path_labels: List[str], type_hint: str = ""):
        if hop > MAX_HOPS:
            rows.append(_row(mapping_name, mapplet_label, session_name, "", "", target_table, target_field_name,
                              instance_name, "(truncated)", " -> ".join(reversed(path_labels)),
                              "Maximum lineage depth reached; trace truncated to avoid a runaway loop.", "", hop))
            return
        vkey = (ctx.kind, ctx.name, instance_name, field_name)
        if vkey in visited:
            rows.append(_row(mapping_name, mapplet_label, session_name, "", "", target_table, target_field_name,
                              instance_name, "(cycle)", " -> ".join(reversed(path_labels)),
                              "Cycle detected -- this exact field was already visited earlier in the trace.",
                              "", hop))
            return
        visited.add(vkey)

        # type_hint (read off the CONNECTOR edge that got us here, when the
        # export includes FROMINSTANCETYPE/TOINSTANCETYPE) disambiguates the
        # case where this mapping has two different instances -- e.g. a
        # SOURCE and a TARGET -- sharing the same NAME. Without it we'd fall
        # back to whichever instance happened to be indexed last.
        inst = ctx.resolve(instance_name, type_hint)
        if inst is None:
            return
        this_label = f"{instance_name}.{field_name}"
        full_path = " -> ".join(reversed(path_labels + [this_label]))

        # --- Source: leaf node; try to continue across a session boundary ---
        if inst.type == "SOURCE":
            rows.append(_row(mapping_name, mapplet_label, session_name, inst.ref_name, field_name,
                              target_table, target_field_name, inst.ref_name, "Source Table", full_path,
                              "Original source field (Source Qualifier boundary reached).",
                              f"{this_label}" + (f" -> {path_labels[-1]}" if path_labels else ""), hop))
            _follow_cross_session(repo, inst.ref_name, field_name, mapping_name, hop, path_labels + [this_label],
                                   target_table, target_field_name, rows)
            return

        # --- Mapplet boundary: descend into the mapplet's own graph ---
        if inst.type == "MAPPLET":
            entry_inst_name = _entry_into_mapplet(repo, inst.ref_name, field_name)
            mplt_ctx = _build_ctx(repo, "mapplet", inst.ref_name)
            if mplt_ctx is None or entry_inst_name is None:
                rows.append(_row(mapping_name, inst.ref_name, session_name, "", "", target_table, target_field_name,
                                  inst.ref_name, "Mapplet", full_path,
                                  "Mapplet boundary could not be resolved to an internal producing port; "
                                  "trace stops here.", this_label, hop))
                return
            rows.append(_row(mapping_name, inst.ref_name, session_name, "", "", target_table, target_field_name,
                              inst.ref_name, "Mapplet", full_path,
                              f"Enters Mapplet '{inst.ref_name}' via its output port '{field_name}'.",
                              this_label, hop))
            walk(mplt_ctx, inst.ref_name, entry_inst_name, field_name, hop + 1, path_labels + [this_label])
            return

        # --- Ordinary transformation ---
        tobj = _get_transformation(repo, ctx, inst.ref_name)
        ttype = inst.ref_type or (tobj.type if tobj else "") or "(unknown)"
        expr = ""
        if tobj:
            for e in tobj.expressions:
                if e.get("port") == field_name:
                    expr = e.get("expression", "")
                    break
        if expr:
            individual_logic = expr
        elif tobj and any(p.name == field_name for p in tobj.input_ports) and \
                not any(p.name == field_name for p in tobj.output_ports):
            # Pure input port with no expression of its own: value simply
            # arrives from upstream via the connector traced below.
            individual_logic = "(input port -- value received as-is from the upstream connector)"
        else:
            individual_logic = (tobj.business_logic if tobj else "") \
                or "(pass-through / no port-level expression captured for this port)"

        preds = ctx.pred_index.get((instance_name, field_name), [])
        ref_ports = _referenced_ports(tobj, field_name, expr)

        if not preds and not ref_ports:
            preds, opaque = resolve_custom_transformation_predecessors(ctx, instance_name, field_name, ttype, tobj)
            if opaque:
                individual_logic = (individual_logic + " " if individual_logic else "") + \
                    "[Best-effort: this Custom Transformation's internal port mapping isn't captured in " \
                    "the exported metadata, so every connected input port is shown as a possible source.]"

        if not preds and not ref_ports:
            rows.append(_row(mapping_name, mapplet_label, session_name, "", "", target_table, target_field_name,
                              instance_name, ttype, full_path, individual_logic,
                              "(no upstream connector -- literal, mapping parameter/variable, "
                              "Sequence Generator value, or unconnected port)", hop))
            return

        links_desc = [f"{fi}.{ff} -> {this_label}" for (fi, ff) in preds]
        links_desc += [f"{instance_name}.{rf} (referenced in expression) -> {this_label}" for rf in ref_ports]
        rows.append(_row(mapping_name, mapplet_label, session_name, "", "", target_table, target_field_name,
                          instance_name, ttype, full_path, individual_logic, "; ".join(links_desc), hop))

        for (from_inst, from_field) in preds:
            hint = ctx.edge_from_type.get((instance_name, field_name, from_inst, from_field), "")
            walk(ctx, mapplet_label, from_inst, from_field, hop + 1, path_labels + [this_label], hint)
        for rf in ref_ports:
            # Same instance, different port -- type can't have changed.
            walk(ctx, mapplet_label, instance_name, rf, hop + 1, path_labels + [this_label], inst.type)

    ctx0 = _build_ctx(repo, "mapping", mapping_name)
    preds0 = ctx0.pred_index.get((target_instance_name, target_field_name), [])
    if not preds0:
        rows.append(_row(mapping_name, "", session_name, "", "", target_table, target_field_name,
                          target_instance_name, "Target", target_label,
                          "No inbound connector found for this target field.",
                          "(unconnected target field)", 0))
    for (from_inst, from_field) in preds0:
        hint0 = ctx0.edge_from_type.get((target_instance_name, target_field_name, from_inst, from_field), "")
        walk(ctx0, "", from_inst, from_field, 1, [target_label], hint0)

    return rows


_CROSS_SESSION_VISITED: set = set()


def _follow_cross_session(repo, source_table: str, field_name: str, current_mapping: str, hop: int,
                           path_labels: List[str], target_table: str, target_field_name: str, rows: List[Dict]):
    """When a Source table matches another mapping's Target table, follow
    the 'Previous Session Target -> Current Session Source' link and keep
    tracing in that upstream mapping/session, per the prompt's
    cross-session requirement. Best-effort: matches by table name (and
    field name, case-insensitive); guarded against infinite loops by a
    module-level visited set keyed on (mapping, table, field)."""
    wf = repo.workflow
    if wf is None:
        return
    vkey = (current_mapping, source_table, field_name.lower())
    if vkey in _CROSS_SESSION_VISITED:
        return
    _CROSS_SESSION_VISITED.add(vkey)

    for m_name, mapping in repo.mappings.items():
        if m_name == current_mapping:
            continue
        for inst in mapping.instances:
            if inst.type != "TARGET" or inst.ref_name != source_table:
                continue
            table = repo.targets.get(inst.ref_name)
            if table is None or not any(f.name.lower() == field_name.lower() for f in table.fields):
                continue
            upstream_session = session_for_mapping(repo, m_name)
            upstream_field = next(f.name for f in table.fields if f.name.lower() == field_name.lower())
            rows.append(_row(m_name, "", upstream_session, "", "", inst.ref_name, upstream_field,
                              inst.name, "Previous Session Target", " -> ".join(reversed(path_labels)),
                              f"Cross-session dependency: '{source_table}' is populated by mapping "
                              f"'{m_name}'" + (f" (session '{upstream_session}')" if upstream_session else "")
                              + " in an earlier load.", f"{inst.name}.{upstream_field} (cross-session)", hop))
            upstream_rows = trace_target_field(repo, m_name, upstream_session, inst.name, upstream_field)
            for r in upstream_rows:
                r["Hop Count"] = r["Hop Count"] + hop
            rows.extend(upstream_rows)
            return  # first match wins; deterministic and avoids fan-out explosion


def reset_cross_session_cache():
    _CROSS_SESSION_VISITED.clear()


def build_field_lineage_rows(repo, mapping_name: str, session_name: Optional[str],
                              target_instance_name: str) -> List[Dict]:
    """Processes every Target Field on the chosen Target instance one by
    one (`## Processing Logic`, step 6) and returns the combined row set
    for the Field_Lineage worksheet."""
    reset_cross_session_cache()
    all_rows: List[Dict] = []
    for field_name in target_fields_for_instance(repo, mapping_name, target_instance_name):
        all_rows.extend(trace_target_field(repo, mapping_name, session_name, target_instance_name, field_name))
    return all_rows


# ============================================================================
# Flattened, single-field-per-row lineage (current implementation).
#
# One row = one (Source Field -> Target Field) dependency for a single
# mapping/mapplet/transformation chain, not one row per physical hop.
#
# Dependency Type ("Direct" vs "Indirect") is based on how many real
# business-logic boundaries were crossed to reach the field, not on the
# number of physical connector hops:
#   - Passing a value through connectors/pass-through ports (no
#     computation) never counts as a crossing.
#   - Being referenced BY NAME inside an expression/condition that
#     computes another port (e.g. the fields used inside an IIF/lookup
#     condition/expression) counts as one crossing.
#   - crossing_count <= 1  -> "Direct"  (referenced directly by the
#     expression that ultimately produces the target field, even if it
#     arrived there through several plain pass-through hops).
#   - crossing_count >= 2  -> "Indirect" (it only feeds the target
#     because it feeds *another* field that is itself used in the
#     target's expression).
#
# Example (matches the "CUSTOMER_SEGMENT" example in the spec):
#   CUSTOMER_SEGMENT = IIF(SALES_AMT > 100000 AND CREDIT_SCORE > 750
#                          AND REGION = 'US', 'PREMIUM', 'STANDARD')
#   SALES_AMT        = ORDER_VALUE + ADJUSTMENT   (an earlier transformation)
#   -> SALES_AMT, CREDIT_SCORE, REGION      = Direct   (crossing_count 1)
#   -> ORDER_VALUE, ADJUSTMENT               = Indirect (crossing_count 2)
# ============================================================================

FlatDict = Dict[str, object]


def _resolve_port_logic(repo, ctx: _Ctx, inst: "Instance", field_name: str):
    """Returns (ttype, logic_text, ref_ports, is_real_crossing) for one
    instance/field, reusing the same expression/port classification rules
    as the hop-by-hop tracer.

    is_real_crossing distinguishes genuine business logic (functions,
    operators, conditionals, or combining more than one field) from a
    trivial rename/passthrough expression such as EXPRESSION="IN_FIELD"
    (a bare copy of a single upstream port with no transformation
    applied) -- the latter must NOT count as a Direct/Indirect boundary,
    or a field that only passed through renames would be misclassified
    as Indirect."""
    tobj = _get_transformation(repo, ctx, inst.ref_name)
    ttype = inst.ref_type or (tobj.type if tobj else "") or "(unknown)"
    expr = ""
    if tobj:
        for e in tobj.expressions:
            if e.get("port") == field_name:
                expr = e.get("expression", "")
                break
    ref_ports = _referenced_ports(tobj, field_name, expr) if expr else []
    generic_is_real_crossing = bool(ref_ports) and not (
        len(ref_ports) == 1 and expr.strip() == ref_ports[0].strip()
    )
    is_real_crossing = _classify_is_real_crossing(ttype, field_name, tobj, expr, ref_ports,
                                                   generic_is_real_crossing)
    if expr:
        logic_text = expr
    elif tobj and any(p.name == field_name for p in tobj.input_ports) and \
            not any(p.name == field_name for p in tobj.output_ports):
        logic_text = "(input port -- value received as-is from the upstream connector)"
    else:
        logic_text = (tobj.business_logic if tobj else "") \
            or "(pass-through / no port-level expression captured for this port)"
    return ttype, logic_text, ref_ports, is_real_crossing


def _emit_leaf(leaf_rows: List[FlatDict], mapping_name: str, mapplet_label: str,
               session_name: Optional[str], source_table: str, source_field: str,
               target_table: str, target_field: str, path_hops: List[dict],
               crossing_count: int, hop_count: int, terminal_note: str = "",
               links: str = "", target_label: str = ""):
    reversed_hops = list(reversed(path_hops))
    prefix = [f"{source_table}.{source_field}"] if source_table else []
    suffix = [target_label] if target_label else []
    full_path = " -> ".join(prefix + [h["label"] for h in reversed_hops] + suffix) or terminal_note

    if path_hops:
        nearest = path_hops[-1]  # transformation instance closest to the source for this field
        transformation_name = nearest["instance"]
        ttype = nearest["ttype"]
    else:
        transformation_name = "(direct)"
        ttype = "Target"

    logic_lines = [f"{h['instance']} ({h['ttype']}): {h['logic']}" for h in reversed_hops]
    if terminal_note:
        logic_lines.append(terminal_note)
    individual = "\n".join(logic_lines) or "(pass-through / no transformation between source and target)"

    leaf_rows.append({
        "Session": session_name or NONE_LABEL,
        "Mapping": mapping_name,
        "Mapplet": mapplet_label or NONE_LABEL,
        "Source Table": source_table or "",
        "Source Field": source_field or "",
        "Target Table": target_table,
        "Target Field": target_field,
        "Transformation": transformation_name,
        "Transformation Type": ttype or "",
        "Transformation_Full_Lineage_Path": full_path,
        "Individual_Transformations": individual,
        "Dependency Type": "Direct" if crossing_count <= 1 else "Indirect",
        "Links": links,
        "Hop Count": hop_count,
    })


def _walk_flat(repo, ctx: _Ctx, mapplet_label: str, instance_name: str, field_name: str, hop: int,
               path_hops: List[dict], crossing_count: int, visited: set, leaf_rows: List[FlatDict],
               mapping_name: str, session_name: Optional[str], target_table: str, target_field: str,
               target_label: str, entry_stack: Optional[List[Tuple[_Ctx, str]]] = None,
               type_hint: str = ""):
    entry_stack = entry_stack or []
    if hop > MAX_HOPS:
        _emit_leaf(leaf_rows, mapping_name, mapplet_label, session_name, "", "", target_table, target_field,
                   path_hops, crossing_count, hop,
                   "Maximum lineage depth reached; trace truncated to avoid a runaway loop.",
                   target_label=target_label)
        return
    vkey = (ctx.kind, ctx.name, instance_name, field_name)
    if vkey in visited:
        _emit_leaf(leaf_rows, mapping_name, mapplet_label, session_name, "", "", target_table, target_field,
                   path_hops, crossing_count, hop,
                   "Cycle detected -- this exact field was already visited earlier in the trace.",
                   target_label=target_label)
        return
    visited.add(vkey)

    inst = ctx.resolve(instance_name, type_hint)
    if inst is None:
        _emit_leaf(leaf_rows, mapping_name, mapplet_label, session_name, "", "", target_table, target_field,
                   path_hops, crossing_count, hop, "Referenced instance not found; trace stopped.",
                   target_label=target_label)
        return

    # --- Source: leaf node; try to continue across a session boundary ---
    if inst.type == "SOURCE":
        _emit_leaf(leaf_rows, mapping_name, mapplet_label, session_name, inst.ref_name, field_name,
                   target_table, target_field, path_hops, crossing_count, hop,
                   target_label=target_label)
        _follow_cross_session_flat(repo, inst.ref_name, field_name, mapping_name, session_name, hop,
                                    target_table, target_field, leaf_rows)
        return

    # --- Mapplet boundary: descend into the mapplet's own graph ---
    if inst.type == "MAPPLET":
        entry_inst_name = _entry_into_mapplet(repo, inst.ref_name, field_name)
        mplt_ctx = _build_ctx(repo, "mapplet", inst.ref_name)
        if mplt_ctx is None or entry_inst_name is None:
            _emit_leaf(leaf_rows, mapping_name, inst.ref_name, session_name, "", "", target_table, target_field,
                       path_hops, crossing_count, hop,
                       f"Mapplet boundary '{inst.ref_name}' could not be resolved to an internal producing "
                       "port; trace stops here.",
                       target_label=target_label)
            return
        new_path = path_hops + [{
            "instance": inst.ref_name, "field": field_name, "ttype": "Mapplet",
            "logic": f"Enters Mapplet '{inst.ref_name}' via its output port '{field_name}'.",
            "label": f"{inst.name}.{field_name}",
        }]
        # Remember where to bubble back out to if we hit the mapplet's Input
        # Transformation boundary (its input ports have no connector inside
        # the mapplet's own graph -- they're fed by a connector in the
        # enclosing mapping/mapplet, into this MAPPLET instance's port,
        # under the SAME port name as the internal Input Transformation
        # port we eventually dead-end at -- not necessarily this entry
        # field, so only the instance reference is kept here).
        new_stack = entry_stack + [(ctx, instance_name)]
        _walk_flat(repo, mplt_ctx, inst.ref_name, entry_inst_name, field_name, hop + 1, new_path,
                   crossing_count, visited, leaf_rows, mapping_name, session_name, target_table, target_field,
                   target_label, new_stack)
        return

    # --- Ordinary transformation ---
    ttype, logic_text, ref_ports, is_real_crossing = _resolve_port_logic(repo, ctx, inst, field_name)
    tobj = _get_transformation(repo, ctx, inst.ref_name)
    preds = ctx.pred_index.get((instance_name, field_name), [])

    if not preds and not ref_ports:
        preds, opaque = resolve_custom_transformation_predecessors(ctx, instance_name, field_name, ttype, tobj)
        if opaque:
            logic_text = (logic_text + " " if logic_text else "") + \
                "[Best-effort: this Custom Transformation's internal port mapping isn't captured in the " \
                "exported metadata, so every connected input port is shown as a possible source.]"

    this_hop = {"instance": instance_name, "field": field_name, "ttype": ttype, "logic": logic_text,
                "label": f"{instance_name}.{field_name}"}
    new_path = path_hops + [this_hop]

    if not preds and not ref_ports:
        if ctx.kind == "mapplet" and entry_stack:
            # Dead end at a Mapplet boundary port (typically its Input
            # Transformation): bubble back out to the enclosing
            # mapping/mapplet and keep tracing from whatever feeds the
            # MAPPLET instance's own port there (the mapplet-internal graph
            # has no connector into an Input Transformation's port -- it's
            # fed by a connector one level up, into the MAPPLET instance).
            # Keep popping the stack in case of nested mapplets with no
            # connector at an intermediate level either.
            remaining_stack = list(entry_stack)
            while remaining_stack:
                parent_ctx, parent_instance_name = remaining_stack.pop()
                parent_preds = parent_ctx.pred_index.get((parent_instance_name, field_name), [])
                if parent_preds:
                    for (from_inst, from_field) in parent_preds:
                        parent_hint = parent_ctx.edge_from_type.get(
                            (parent_instance_name, field_name, from_inst, from_field), "")
                        _walk_flat(repo, parent_ctx, mapplet_label, from_inst, from_field, hop + 1, new_path,
                                   crossing_count, visited, leaf_rows, mapping_name, session_name,
                                   target_table, target_field, target_label, remaining_stack,
                                   type_hint=parent_hint)
                    return
        _emit_leaf(leaf_rows, mapping_name, mapplet_label, session_name, "", "", target_table, target_field,
                   new_path, crossing_count, hop,
                   "(no upstream connector -- literal, mapping parameter/variable, Sequence Generator "
                   "value, or unconnected port)",
                   target_label=target_label)
        return

    # Plain pass-through wiring: does not cross a new business-logic boundary,
    # unless this hop's own classification says it's real business logic
    # (Filter/Lookup/Source Qualifier/Aggregator table-level signals aren't
    # necessarily expressed via a referenced port, so the crossing has to be
    # counted here too, not only when recursing into ref_ports below).
    next_crossing = crossing_count + 1 if is_real_crossing else crossing_count
    for (from_inst, from_field) in preds:
        hint = ctx.edge_from_type.get((instance_name, field_name, from_inst, from_field), "")
        _walk_flat(repo, ctx, mapplet_label, from_inst, from_field, hop + 1, new_path, next_crossing,
                   visited, leaf_rows, mapping_name, session_name, target_table, target_field,
                   target_label, entry_stack, type_hint=hint)
    # Fields referenced by name inside this port's own expression. A bare
    # rename/passthrough expression (e.g. EXPRESSION="IN_FIELD") does not
    # cross a new business-logic boundary; a real expression (functions,
    # operators, conditionals, or multiple fields combined) does.
    for rf in ref_ports:
        # Same instance, different port -- type can't have changed.
        _walk_flat(repo, ctx, mapplet_label, instance_name, rf, hop + 1, new_path, next_crossing,
                   visited, leaf_rows, mapping_name, session_name, target_table, target_field,
                   target_label, entry_stack, type_hint=inst.type)


def _follow_cross_session_flat(repo, source_table: str, field_name: str, current_mapping: str,
                                session_name: Optional[str], hop: int, target_table: str, target_field: str,
                                leaf_rows: List[FlatDict]):
    """Same cross-session behaviour as the hop-by-hop tracer, but appends
    flattened leaf rows and stamps the required 'Links' description:
    prev_session.Target_table.field_name -> Current_session.source.field_name."""
    wf = repo.workflow
    if wf is None:
        return
    vkey = (current_mapping, source_table, field_name.lower())
    if vkey in _CROSS_SESSION_VISITED:
        return
    _CROSS_SESSION_VISITED.add(vkey)

    for m_name, mapping in repo.mappings.items():
        if m_name == current_mapping:
            continue
        for inst in mapping.instances:
            if inst.type != "TARGET" or inst.ref_name != source_table:
                continue
            table = repo.targets.get(inst.ref_name)
            if table is None or not any(f.name.lower() == field_name.lower() for f in table.fields):
                continue
            upstream_session = session_for_mapping(repo, m_name)
            upstream_field = next(f.name for f in table.fields if f.name.lower() == field_name.lower())
            link = (f"{upstream_session or m_name}.{inst.ref_name}.{upstream_field} -> "
                    f"{session_name or current_mapping}.{source_table}.{field_name}")
            upstream_rows = trace_target_field_flat(repo, m_name, upstream_session, inst.name, upstream_field)
            for r in upstream_rows:
                r["Hop Count"] = r["Hop Count"] + hop
                if not r["Links"]:
                    r["Links"] = link
            leaf_rows.extend(upstream_rows)
            return  # first match wins; deterministic and avoids fan-out explosion


def trace_target_field_flat(repo, mapping_name: str, session_name: Optional[str],
                             target_instance_name: str, target_field_name: str) -> List[FlatDict]:
    """Flattened version of trace_target_field(): one row per (Source Field,
    Target Field) dependency instead of one row per physical hop, with a
    computed Direct/Indirect Dependency Type."""
    mapping = repo.mappings.get(mapping_name)
    if mapping is None:
        return []
    target_inst = next((i for i in mapping.instances
                         if i.name == target_instance_name and i.type == "TARGET"), None) \
        or next((i for i in mapping.instances if i.name == target_instance_name), None)
    if target_inst is None:
        return []
    target_table = target_inst.ref_name

    target_label = f"{target_instance_name}.{target_field_name}"
    leaf_rows: List[FlatDict] = []
    visited: set = set()
    ctx0 = _build_ctx(repo, "mapping", mapping_name)
    preds0 = ctx0.pred_index.get((target_instance_name, target_field_name), [])

    if not preds0:
        _emit_leaf(leaf_rows, mapping_name, "", session_name, "", "", target_table, target_field_name,
                   [], 0, 0, "No inbound connector found for this target field.", target_label=target_label)
        return leaf_rows

    for (from_inst, from_field) in preds0:
        hint0 = ctx0.edge_from_type.get((target_instance_name, target_field_name, from_inst, from_field), "")
        _walk_flat(repo, ctx0, "", from_inst, from_field, 1, [], 0, visited, leaf_rows,
                   mapping_name, session_name, target_table, target_field_name, target_label,
                   type_hint=hint0)
    return leaf_rows


def build_field_lineage_rows_flat(repo, mapping_name: str, session_name: Optional[str],
                                   target_instance_name: str) -> List[FlatDict]:
    """Flattened equivalent of build_field_lineage_rows(): every Target
    Field on the chosen Target instance, one row per contributing field."""
    reset_cross_session_cache()
    all_rows: List[FlatDict] = []
    for field_name in target_fields_for_instance(repo, mapping_name, target_instance_name):
        all_rows.extend(trace_target_field_flat(repo, mapping_name, session_name, target_instance_name, field_name))
    return all_rows


def build_full_workflow_field_lineage(repo) -> List[FlatDict]:
    """Runs the flattened trace for every Target Table field, across every
    Mapping (and, transitively, every Mapplet used by those mappings) in
    the uploaded workflow -- not just a single selected Target Table."""
    reset_cross_session_cache()
    all_rows: List[FlatDict] = []
    wf = repo.workflow

    ordered_mappings: List[Tuple[str, Optional[str]]] = []
    seen = set()
    if wf is not None:
        order = wf.execution_order or compute_execution_order(repo)
        ordered_session_names = [t for t in order if t in wf.sessions] or list(wf.sessions.keys())
        for sname in ordered_session_names:
            mname = wf.sessions[sname].mapping_name
            if mname in repo.mappings and mname not in seen:
                ordered_mappings.append((mname, sname))
                seen.add(mname)
    for mname in repo.mappings:
        if mname not in seen:
            ordered_mappings.append((mname, session_for_mapping(repo, mname)))
            seen.add(mname)

    for mapping_name, session_name in ordered_mappings:
        mapping = repo.mappings[mapping_name]
        for tinst in [i for i in mapping.instances if i.type == "TARGET"]:
            for field_name in target_fields_for_instance(repo, mapping_name, tinst.name):
                all_rows.extend(
                    trace_target_field_flat(repo, mapping_name, session_name, tinst.name, field_name)
                )
    return all_rows
