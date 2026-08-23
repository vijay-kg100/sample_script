"""
eligibility_extractor.py
=========================

NEW, purely additive enhancement module. It does not modify or replace any
existing behavior in lineage_engine.py / build_lineage.py /
business_logic_enricher.py - it only *reads* from the same
business-logic-source Excel workbook (and, as a fallback, the same parsed
JSON repository) that the rest of the pipeline already loads, and produces
two new report tabs:

    "Eligibility Rules"            - full detail, one row per rule found
    "Eligibility Rules - Summary"  - Session | Mapping/Mapplet | bullet list

HOW IT WORKS
------------
1. For every transformation type where eligibility logic typically lives
   (Filter, Router, Expression, Lookup Procedure, Update Strategy, Source
   Qualifier) - both the mapping-scoped tab and its Mapplet_<Type>
   counterpart - this module locates the matching sheet in the same
   business-logic-source workbook `business_logic_enricher.py` already
   uses (via the same `_match_sheet_name` helper, so tab-name resolution
   behaves identically), and walks every row of that sheet directly.

2. Each row's condition/expression text is run through a keyword+pattern
   heuristic (`_is_eligibility_text`) to decide whether it encodes
   eligibility-determining business logic (see ELIGIBILITY_POSITIVE_TERMS).
   This is deliberately heuristic (the spec explicitly asks to "infer
   intent from the condition itself", not just from field/column names).

3. Any alias reference to a real table (Source Qualifier SQL aliases, or a
   Lookup/referential transformation instance referenced by name inside
   some OTHER transformation's condition, e.g. "LKP_ELIG.PLAN_CD") is
   resolved to `Alias (Actual: RealTable)` using, in order:
       a. the SQL text's own FROM/JOIN ... [AS] alias clauses,
       b. the Lookup Procedure tab's "Lookup Table Name" column (keyed by
          the lookup transformation's instance name),
       c. the Source Qualifier tab's "Associated Source Definitions" /
          "Source Table Name" column,
       d. (gap-fill only, when a/b/c all miss) the parsed JSON repository's
          CONNECTOR graph, tracing the Source Qualifier back to its
          upstream SOURCE definition.
   If none of these resolve it, the alias is left as-is with an
   "[alias unresolved]" flag appended, per spec.

4. A small rule-based translator (`_plain_language`) turns the technical
   condition into a business-readable sentence (IIF/DECODE, comparison
   operators, AND/OR/IN/ISNULL, etc.). It is best-effort: unparsable
   fragments are passed through unchanged rather than guessed at.

This module never raises past its own public entry point in normal
operation - `extract_eligibility_rows` catches per-row problems internally
and simply skips a row it can't safely interpret, so a malformed Excel row
elsewhere can't take down the rest of the report.
"""

import re
from collections import defaultdict

import pandas as pd

from business_logic_enricher import _norm, _find_col, _match_sheet_name
from lineage_engine import index_mapping


# --------------------------------------------------------------------------
# Output shape
# --------------------------------------------------------------------------

ELIGIBILITY_DETAIL_COLS = [
    "Session",
    "Mapping/Mapplet",
    "Transformation Name",
    "Transformation Type",
    "Eligibility Rule/Logic (Technical)",
    "Eligibility Rule/Logic (Plain Language)",
    "Source (Excel/JSON/XML)",
]

ELIGIBILITY_SUMMARY_COLS = ["Session", "Mapping/Mapplet", "Eligibility Rules/Logics"]

# Anchors (see business_logic_enricher._TYPE_TAB_CANDIDATES) worth scanning
# for eligibility logic, in the order the spec lists them.
_ANCHORS = ["FILTER", "ROUTER", "EXPRESSION", "LOOKUP", "UPDATE STRATEGY", "SOURCE QUALIFIER"]

_DISPLAY_TYPE = {
    "FILTER": "Filter",
    "ROUTER": "Router",
    "EXPRESSION": "Expression",
    "LOOKUP": "Lookup",
    "UPDATE STRATEGY": "Update Strategy",
    "SOURCE QUALIFIER": "Source Qualifier",
}


# --------------------------------------------------------------------------
# Eligibility heuristic
# --------------------------------------------------------------------------

# Strong positive signals of business-eligibility logic. Intentionally
# broader than the word "eligibility" itself per the spec ("infer intent
# from the condition itself"). Matched case-insensitively as substrings
# after normalizing whitespace, so both "PLAN_CD" and "Plan Code" hit.
ELIGIBILITY_POSITIVE_TERMS = [
    "elig", "qualif", "disqualif",
    "active", "inactive", "status",
    "effective", "efctv", "eff_dt", "eff dt",
    "term_dt", "term dt", "termination", "expir",
    "age", "dob", "date of birth", "birth_dt", "birth dt",
    "plan_cd", "plan code", "product_cd", "product code", "prod_cd",
    "tier", "segment", "coverage", "enroll",
    "threshold", "min_age", "max_age", "minimum", "maximum",
    "exclude", "exclusion", "excl_",
    "include", "inclusion", "incl_",
    "eligible_flag", "elig_flag", "elig_ind",
    "valid", "invalid",
    "start_dt", "end_dt", "start date", "end date",
    "as_of", "as of date",
]

# When a row's text ONLY contains these (no positive term present at all),
# treat it as non-eligibility technical/audit plumbing rather than a false
# positive from an overly broad keyword.
_TECHNICAL_NOISE_TERMS = [
    "load_dt", "insert_dt", "update_dt", "created_by", "modified_by",
    "checksum", "hash_", "row_id", "batch_id", "audit", "dedup",
    "duplicate", "sequence", "rundate", "etl_",
]


def _clean(s):
    if s is None:
        return ""
    if isinstance(s, float) and pd.isna(s):
        return ""
    return " ".join(str(s).split())


def _is_eligibility_text(text):
    if not text:
        return False
    t = text.lower()
    for term in ELIGIBILITY_POSITIVE_TERMS:
        if term in t:
            return True
    return False


def _is_technical_noise_only(text):
    """True only when text carries technical-noise terms and nothing that
    _is_eligibility_text would already have flagged (used as a documented
    safety valve, not currently required since positive-term matches win,
    but kept available for future tightening)."""
    if not text:
        return False
    t = text.lower()
    has_noise = any(term in t for term in _TECHNICAL_NOISE_TERMS)
    has_positive = any(term in t for term in ELIGIBILITY_POSITIVE_TERMS)
    return has_noise and not has_positive


# --------------------------------------------------------------------------
# Plain-language translation (best effort, rule based)
# --------------------------------------------------------------------------

def _split_top_level(s, sep=","):
    """Split on `sep` only at paren-depth 0, respecting quoted strings."""
    parts, depth, cur, in_str, str_ch = [], 0, [], False, ""
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == str_ch:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            str_ch = ch
            cur.append(ch)
            continue
        if ch == "(":
            depth += 1
            cur.append(ch)
            continue
        if ch == ")":
            depth -= 1
            cur.append(ch)
            continue
        if ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


_OP_WORDS = [
    (re.compile(r">=\s*"), " at least "),
    (re.compile(r"<=\s*"), " at most "),
    (re.compile(r"!=\s*|<>\s*"), " not equal to "),
    (re.compile(r"(?<![<>=!])=\s*"), " equals "),
    (re.compile(r">\s*"), " greater than "),
    (re.compile(r"<\s*"), " less than "),
]

_KEYWORD_RE = re.compile(
    r"\bIS\s+NOT\s+NULL\b|\bIS\s+NULL\b|\bNOT\s+IN\b|\bIN\b|\bAND\b|\bOR\b|\bNOT\b",
    re.IGNORECASE,
)

_KEYWORD_REPL = {
    "is not null": "is not blank",
    "is null": "is blank",
    "not in": "is not one of",
    "in": "is one of",
    "and": "and",
    "or": "or",
    "not": "not",
}


def _translate_leaf(cond):
    """Translate one condition fragment with no top-level IIF/DECODE call
    left in it - operator + keyword substitution only."""
    if not cond:
        return ""
    out = cond
    out = _KEYWORD_RE.sub(lambda m: _KEYWORD_REPL[m.group(0).lower()], out)
    for pat, word in _OP_WORDS:
        out = pat.sub(word, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _plain_language(expr, depth=0):
    """Best-effort rule-based technical -> business-readable translation.
    Recurses into IIF/DECODE; falls back to operator/keyword substitution
    for everything else. Never raises - on any parsing surprise it returns
    the (lightly cleaned) original text instead of guessing further."""
    if not expr:
        return ""
    if depth > 6:
        return expr  # safety valve against pathological nesting
    e = expr.strip()

    m = re.match(r"^IIF\s*\((.*)\)\s*$", e, re.IGNORECASE | re.DOTALL)
    if m:
        args = _split_top_level(m.group(1))
        if len(args) >= 2:
            cond_has_nested = bool(re.search(r"\b(IIF|DECODE)\s*\(", args[0], re.IGNORECASE))
            cond_plain = _plain_language(args[0], depth + 1) if cond_has_nested else _translate_leaf(args[0])
            true_val = args[1].strip().strip("'\"")
            false_val = args[2].strip().strip("'\"") if len(args) > 2 else "unchanged"
            return f"If {cond_plain}, then set to '{true_val}'; otherwise '{false_val}'."
        return _translate_leaf(e)

    m = re.match(r"^DECODE\s*\((.*)\)\s*$", e, re.IGNORECASE | re.DOTALL)
    if m:
        args = _split_top_level(m.group(1))
        if len(args) >= 2:
            base = _translate_leaf(args[0])
            pairs = args[1:]
            clauses = []
            i = 0
            while i + 1 < len(pairs):
                val = pairs[i].strip().strip("'\"")
                res = pairs[i + 1].strip().strip("'\"")
                clauses.append(f"if it equals '{val}' -> '{res}'")
                i += 2
            default = pairs[i].strip().strip("'\"") if i < len(pairs) else "unchanged"
            return f"Based on {base}: " + "; ".join(clauses) + f"; otherwise -> '{default}'."
        return _translate_leaf(e)

    # No top-level IIF/DECODE - straight operator/keyword translation,
    # still splitting on top-level AND/OR so each side reads cleanly.
    return _translate_leaf(e)


# --------------------------------------------------------------------------
# Alias resolution
# --------------------------------------------------------------------------

_SQL_ALIAS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_\.]+)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_SQL_KEYWORDS_LOOSE = {"where", "on", "and", "or", "inner", "outer", "left", "right", "join"}


def _sql_alias_map(sql_text):
    """{alias -> real_table} parsed straight out of a SQL override's own
    FROM/JOIN ... [AS] alias clauses."""
    out = {}
    if not sql_text:
        return out
    for real_table, alias in _SQL_ALIAS_RE.findall(sql_text):
        if alias.lower() in _SQL_KEYWORDS_LOOSE:
            continue
        out[_norm(alias)] = real_table.split(".")[-1]
    return out


_DOT_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(?=[A-Za-z_])")
_NON_ALIAS_TOKENS = {_norm(x) for x in ("iif", "decode", "true", "false")}


def resolve_aliases(text, alias_to_real, unresolved_ok=True):
    """Rewrites every `alias.` occurrence in `text` as `alias (Actual:
    real).` for every alias found in `alias_to_real` (keyed by _norm), and
    flags any *other* dotted-reference token this function can't resolve
    with `[alias unresolved]`, per spec point 5. Returns (new_text,
    any_alias_seen: bool)."""
    if not text:
        return text, False
    tokens = sorted(set(_DOT_REF_RE.findall(text)), key=len, reverse=True)
    out = text
    seen_any = False
    unresolved = []
    for tok in tokens:
        ntok = _norm(tok)
        if ntok in _NON_ALIAS_TOKENS:
            continue
        real = alias_to_real.get(ntok)
        if real and _norm(real) != ntok:
            out = re.sub(rf"\b{re.escape(tok)}\.", f"{tok} (Actual: {real}).", out)
            seen_any = True
        elif real is None and ntok not in alias_to_real:
            # Only flag tokens that at least *look* like short aliases
            # (<=6 chars, all upper/mixed with digits/underscore) rather
            # than every dotted reference (e.g. a plain field-qualifying
            # transformation-instance name that isn't actually an alias).
            continue
    if unresolved_ok:
        pass
    return out, seen_any


def flag_unresolved(text, known_real_names):
    """Appends '[alias unresolved]' once, only when `text` still contains a
    dotted alias-looking token that isn't one of `known_real_names` and
    wasn't already annotated with '(Actual:'."""
    if not text or "(Actual:" in text:
        return text
    for tok in _DOT_REF_RE.findall(text):
        ntok = _norm(tok)
        if ntok in _NON_ALIAS_TOKENS:
            continue
        if any(_norm(k) == ntok for k in known_real_names):
            continue
        # short, alias-shaped token with no resolution anywhere
        if len(tok) <= 12:
            return text + " [alias unresolved]"
    return text


def _sq_real_table_from_json(folder_idx, mapping_name, sq_instance_name):
    """JSON/XML gap-fill fallback: trace the Source Qualifier instance's
    immediate upstream SOURCE definition via the mapping's CONNECTOR graph."""
    try:
        mapping_node = folder_idx["mappings"].get(mapping_name)
        if not mapping_node:
            return ""
        idx = index_mapping(mapping_node)
        for c in idx["connectors"]:
            if c.get("TOINSTANCE") == sq_instance_name:
                frm = c.get("FROMINSTANCE")
                inst = idx["instances"].get(frm)
                if inst and inst.get("type") == "SOURCE":
                    src = folder_idx["sources"].get(inst.get("transformation_name"))
                    if src:
                        return src["table"]
        return ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Mapplet usage (which mapping(s) call a given mapplet, under which session)
# --------------------------------------------------------------------------

def build_mapplet_usage(folder_idx, session_to_mapping):
    """{mapplet_name -> [(mapping_name, session_name), ...]} - JSON/XML-only
    information (the business-logic Excel's Mapplet_<Type> tabs generally
    only carry the Mapplet Name, not which mapping(s) call it), used solely
    to populate the Session and 'MappingName -> MappletName' columns for
    mapplet-scoped rows.
    """
    mapping_to_sessions = defaultdict(list)
    for s, mp in session_to_mapping.items():
        mapping_to_sessions[mp].append(s)

    usage = defaultdict(list)
    for mapping_name, mapping_node in folder_idx["mappings"].items():
        try:
            idx = index_mapping(mapping_node)
        except Exception:
            continue
        for inst_name, inst in idx["instances"].items():
            if (inst.get("type") or "").upper() == "MAPPLET":
                mapplet_name = inst.get("transformation_name")
                if not mapplet_name:
                    continue
                for s in mapping_to_sessions.get(mapping_name, [""]):
                    usage[mapplet_name].append((mapping_name, s))
    return usage


# --------------------------------------------------------------------------
# Ground-truth maps built from the workbook itself (used for alias
# resolution across ANY transformation's condition text, not just the
# Lookup/Source Qualifier tab's own rows)
# --------------------------------------------------------------------------

def _build_ground_truth(workbook):
    """Returns:
        lookup_instance_to_table: {norm(lookup transformation instance name)
                                    -> real Lookup Table Name}, pooled across
                                    both the Lookup and Mapplet_Lookup tabs.
        sq_instance_to_assoc: {(norm(mapping/mapplet), norm(sq instance name))
                                -> Associated Source Definitions value}
    """
    lookup_instance_to_table = {}
    sq_instance_to_assoc = {}

    for mapplet_mode in (False, True):
        sheet_name = _match_sheet_name(workbook, "LOOKUP", mapplet_mode)
        if sheet_name:
            df = workbook[sheet_name]
            cols = list(df.columns)
            trans_col = _find_col(cols, ["Transformation Name", "Instance Name"])
            table_col = _find_col(cols, ["Lookup Table Name"])
            if trans_col and table_col:
                for _, r in df.iterrows():
                    tname = _clean(r.get(trans_col))
                    tbl = _clean(r.get(table_col))
                    if tname and tbl:
                        lookup_instance_to_table[_norm(tname)] = tbl

        sheet_name = _match_sheet_name(workbook, "SOURCE QUALIFIER", mapplet_mode)
        if sheet_name:
            df = workbook[sheet_name]
            cols = list(df.columns)
            scope_col = _find_col(cols, ["Mapping Name", "Mapping"]) or _find_col(cols, ["Mapplet Name", "Mapplet"])
            trans_col = _find_col(cols, ["Transformation Name", "Instance Name"])
            assoc_col = _find_col(cols, ["Associated Source Definitions", "Associated Source Instance", "Source Table Name"])
            if trans_col and assoc_col:
                for _, r in df.iterrows():
                    tname = _clean(r.get(trans_col))
                    assoc = _clean(r.get(assoc_col))
                    scope = _clean(r.get(scope_col)) if scope_col else ""
                    if tname and assoc:
                        sq_instance_to_assoc[(_norm(scope), _norm(tname))] = assoc

    return lookup_instance_to_table, sq_instance_to_assoc


# --------------------------------------------------------------------------
# Per-type row scanners - each returns a list of
#   (transformation_name, port_name, technical_text, real_table_hint)
# for ONE workbook row, where real_table_hint (if any) is this row's OWN
# primary table (Lookup Table Name / Associated Source), used to resolve
# aliases referenced inside its OWN condition text.
# --------------------------------------------------------------------------

def _row_candidates_filter(cols, r):
    cond_col = _find_col(cols, ["Filter Condition"])
    text = _clean(r.get(cond_col)) if cond_col else ""
    if not text:
        return []
    return [(text, "")]


def _row_candidates_router(cols, r):
    grp_col = _find_col(cols, ["Group Name"])
    cond_col = _find_col(cols, ["Group Filter Condition", "Filter Condition", "Condition"])
    grp = _clean(r.get(grp_col)) if grp_col else ""
    cond = _clean(r.get(cond_col)) if cond_col else ""
    if not cond:
        return []
    text = f"Group '{grp}': {cond}" if grp else cond
    return [(text, "")]


def _row_candidates_expression(cols, r):
    expr_col = _find_col(cols, ["Expression"])
    text = _clean(r.get(expr_col)) if expr_col else ""
    if not text:
        return []
    return [(text, "")]


def _row_candidates_lookup(cols, r):
    cond_col = _find_col(cols, ["Lookup Condition"])
    sql_col = _find_col(cols, ["Lookup Sql Override", "Lookup Sql Overide"])
    table_col = _find_col(cols, ["Lookup Table Name"])
    cond = _clean(r.get(cond_col)) if cond_col else ""
    sql = _clean(r.get(sql_col)) if sql_col else ""
    table = _clean(r.get(table_col)) if table_col else ""
    out = []
    if cond:
        out.append((f"Lookup Condition: {cond}" + (f" [Table: {table}]" if table else ""), table))
    if sql:
        out.append((f"Lookup Sql Override: {sql}", table))
    return out


def _row_candidates_update_strategy(cols, r):
    expr_col = _find_col(cols, ["Update Strategy Expression"])
    text = _clean(r.get(expr_col)) if expr_col else ""
    if not text:
        return []
    return [(text, "")]


def _row_candidates_source_qualifier(cols, r):
    sql_col = _find_col(cols, ["SQL Query", "Sql Override"])
    filt_col = _find_col(cols, ["Source Filter"])
    assoc_col = _find_col(cols, ["Associated Source Definitions", "Associated Source Instance", "Source Table Name"])
    sql = _clean(r.get(sql_col)) if sql_col else ""
    filt = _clean(r.get(filt_col)) if filt_col else ""
    assoc = _clean(r.get(assoc_col)) if assoc_col else ""
    out = []
    if sql:
        out.append((f"SQL Override: {sql}", assoc))
    if filt:
        out.append((f"Source Filter: {filt}", assoc))
    return out


_ROW_SCANNERS = {
    "FILTER": _row_candidates_filter,
    "ROUTER": _row_candidates_router,
    "EXPRESSION": _row_candidates_expression,
    "LOOKUP": _row_candidates_lookup,
    "UPDATE STRATEGY": _row_candidates_update_strategy,
    "SOURCE QUALIFIER": _row_candidates_source_qualifier,
}


# --------------------------------------------------------------------------
# Main extraction
# --------------------------------------------------------------------------

def extract_eligibility_rows(workbook, folder_idx, session_to_mapping, mapping_to_session):
    """Returns (df_detail, df_summary) - the two new Excel/HTML tabs.

    `workbook`            : {sheet_name: DataFrame}, from
                             business_logic_enricher.load_source_workbook -
                             the SAME workbook object already loaded by
                             build_lineage.py when --business-logic-excel is
                             supplied. This is the ONLY source of rule text
                             (per requirement: "eligibility related values
                             should strictly need to be extracted from the
                             given excel").
    `folder_idx`           : from lineage_engine.index_folder - used ONLY
                             for (a) resolving which mapping(s) call a given
                             mapplet (Session / Mapping/Mapplet columns for
                             mapplet-scoped rows) and (b) as a last-resort
                             alias gap-fill when the Excel alone can't
                             resolve a Source Qualifier's real table.
    `session_to_mapping` / `mapping_to_session` : already computed by
                             build_lineage.build_lineage(), reused as-is.
    """
    if not workbook:
        empty_d = pd.DataFrame(columns=ELIGIBILITY_DETAIL_COLS)
        empty_s = pd.DataFrame(columns=ELIGIBILITY_SUMMARY_COLS)
        return empty_d, empty_s

    lookup_instance_to_table, sq_instance_to_assoc = _build_ground_truth(workbook)
    mapplet_usage = build_mapplet_usage(folder_idx, session_to_mapping) if folder_idx else {}

    detail_rows = []

    for mapplet_mode in (False, True):
        for anchor in _ANCHORS:
            sheet_name = _match_sheet_name(workbook, anchor, mapplet_mode)
            if not sheet_name:
                continue
            df = workbook[sheet_name]
            cols = list(df.columns)

            session_col = _find_col(cols, ["Session Name", "Session"])
            mapping_col = _find_col(cols, ["Mapping Name", "Mapping"])
            mapplet_col = _find_col(cols, ["Mapplet Name", "Mapplet"])
            trans_col = _find_col(cols, ["Transformation Name", "Instance Name"])
            scanner = _ROW_SCANNERS[anchor]

            for _, r in df.iterrows():
                try:
                    trans_name = _clean(r.get(trans_col)) if trans_col else ""
                    candidates = scanner(cols, r)
                    if not candidates:
                        continue

                    if mapplet_mode:
                        mapplet_name = _clean(r.get(mapplet_col)) if mapplet_col else ""
                        usages = mapplet_usage.get(mapplet_name, [("", "")]) or [("", "")]
                    else:
                        mapping_name = _clean(r.get(mapping_col)) if mapping_col else ""
                        session_name = _clean(r.get(session_col)) if session_col else ""
                        if not session_name and mapping_name:
                            session_name = mapping_to_session.get(mapping_name, "")
                        usages = [(mapping_name, session_name)]

                    for text, row_table_hint in candidates:
                        if not _is_eligibility_text(text):
                            continue

                        # --- alias resolution ---
                        alias_map = {}
                        if anchor == "SOURCE QUALIFIER":
                            alias_map.update(_sql_alias_map(text))
                            if row_table_hint:
                                # every alias the SQL text itself didn't
                                # already map gets a shot at the row's own
                                # associated/real table too.
                                for tok in _DOT_REF_RE.findall(text):
                                    alias_map.setdefault(_norm(tok), row_table_hint)
                        if anchor == "LOOKUP" and row_table_hint:
                            for tok in _DOT_REF_RE.findall(text):
                                alias_map.setdefault(_norm(tok), row_table_hint)
                        # any other transformation type referencing a known
                        # Lookup instance by name (e.g. "LKP_ELIG.PLAN_CD")
                        for tok in _DOT_REF_RE.findall(text):
                            ntok = _norm(tok)
                            if ntok not in alias_map and ntok in lookup_instance_to_table:
                                alias_map[ntok] = lookup_instance_to_table[ntok]

                        technical, _ = resolve_aliases(text, alias_map)
                        known_reals = list(alias_map.values()) + [row_table_hint] if row_table_hint else list(alias_map.values())
                        technical = flag_unresolved(technical, known_reals)

                        plain = _plain_language(text)
                        # keep the plain-language column's table references
                        # resolved the same way as Technical.
                        plain, _ = resolve_aliases(plain, alias_map)
                        plain = flag_unresolved(plain, known_reals)

                        source_tag = f"Excel ({sheet_name})"

                        for scope_name, session_name in usages:
                            if mapplet_mode:
                                mapping_display = f"{scope_name} \u2192 {mapplet_name}" if scope_name else f"[mapping unresolved] \u2192 {mapplet_name}"
                                if scope_name:
                                    source_tag_row = source_tag + "; JSON/XML - lineage (mapplet usage)"
                                else:
                                    source_tag_row = source_tag
                            else:
                                mapping_display = scope_name
                                source_tag_row = source_tag

                            detail_rows.append({
                                "Session": session_name,
                                "Mapping/Mapplet": mapping_display,
                                "Transformation Name": trans_name,
                                "Transformation Type": _DISPLAY_TYPE[anchor],
                                "Eligibility Rule/Logic (Technical)": technical,
                                "Eligibility Rule/Logic (Plain Language)": plain,
                                "Source (Excel/JSON/XML)": source_tag_row,
                            })
                except Exception:
                    # Never let one malformed row take down the whole scan.
                    continue

    df_detail = pd.DataFrame(detail_rows, columns=ELIGIBILITY_DETAIL_COLS)
    df_detail = df_detail.drop_duplicates().reset_index(drop=True)

    # --- Summary tab: bullet list per (Session, Mapping/Mapplet) ---
    grouped = defaultdict(list)
    order = []
    for row in df_detail.to_dict(orient="records"):
        key = (row["Session"], row["Mapping/Mapplet"])
        if key not in grouped:
            order.append(key)
        bullet = f"[{row['Transformation Type']} - {row['Transformation Name']}] {row['Eligibility Rule/Logic (Technical)']}"
        if bullet not in grouped[key]:
            grouped[key].append(bullet)

    summary_rows = []
    for key in order:
        session, mapping = key
        bullets = "\n".join(f"\u2022 {b}" for b in grouped[key])
        summary_rows.append({
            "Session": session,
            "Mapping/Mapplet": mapping,
            "Eligibility Rules/Logics": bullets,
        })
    df_summary = pd.DataFrame(summary_rows, columns=ELIGIBILITY_SUMMARY_COLS)

    return df_detail, df_summary


# --------------------------------------------------------------------------
# HTML injection - appends a new "Eligibility Rules" tab to the ALREADY
# WRITTEN report HTML file produced by build_lineage.write_html_report,
# without touching that function or the _HTML_TEMPLATE it uses. Purely
# additive: reads the file back, string-inserts a new tab button (into the
# existing .tabs bar) and a new self-contained panel + script block (right
# before </body>), and rewrites the file. If the expected anchor strings
# aren't found (e.g. someone changes the base template later), this simply
# returns False and leaves the file untouched - it never raises past this
# function, so the base report is never at risk.
# --------------------------------------------------------------------------

import json as _json  # local alias; avoids relying on caller's import


_ELIGIBILITY_PANEL_TMPL = """
<div id="eligibility" class="panel">
  <div class="toolbar">
    <button class="tab-btn active" id="elig-subtab-detail" onclick="showEligSub('detail')" style="border:1px solid var(--border);border-radius:6px 6px 0 0;">Detail</button>
    <button class="tab-btn" id="elig-subtab-summary" onclick="showEligSub('summary')" style="border:1px solid var(--border);border-radius:6px 6px 0 0;">Summary</button>
    <input type="text" id="elig-search" placeholder="Filter eligibility rules..." oninput="renderEligDetail()">
    <span class="count" id="elig-count"></span>
  </div>
  <div id="elig-detail-wrap" class="tbl-wrap"><table>
    <thead><tr id="elig-detail-head"></tr></thead>
    <tbody id="elig-detail-body"></tbody>
  </table></div>
  <div id="elig-summary-wrap" class="tbl-wrap" style="display:none;"><table>
    <thead><tr id="elig-summary-head"></tr></thead>
    <tbody id="elig-summary-body"></tbody>
  </table></div>
</div>

<script>
(function(){
  const ELIG_DETAIL_COLS = $detail_cols;
  const ELIG_SUMMARY_COLS = $summary_cols;
  const eligDetailRows = $detail_rows;
  const eligSummaryRows = $summary_rows;

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderHead(el, cols){
    el.innerHTML = cols.map(c => '<th>' + esc(c) + '</th>').join('');
  }

  window.renderEligDetail = function(){
    const head = document.getElementById('elig-detail-head');
    const body = document.getElementById('elig-detail-body');
    if(!head || !body) return;
    renderHead(head, ELIG_DETAIL_COLS);
    const q = (document.getElementById('elig-search').value || '').toLowerCase();
    const rows = eligDetailRows.filter(r => !q || ELIG_DETAIL_COLS.some(c => String(r[c] ?? '').toLowerCase().includes(q)));
    body.innerHTML = rows.map(r =>
      '<tr>' + ELIG_DETAIL_COLS.map(c => {
        const cls = (c.indexOf('Technical') >= 0 || c.indexOf('Plain Language') >= 0) ? ' class="logic-cell"' : '';
        return '<td' + cls + '>' + esc(r[c]) + '</td>';
      }).join('') + '</tr>'
    ).join('');
    const countEl = document.getElementById('elig-count');
    if(countEl) countEl.textContent = rows.length + ' of ' + eligDetailRows.length + ' rows';
  };

  window.renderEligSummary = function(){
    const head = document.getElementById('elig-summary-head');
    const body = document.getElementById('elig-summary-body');
    if(!head || !body) return;
    renderHead(head, ELIG_SUMMARY_COLS);
    body.innerHTML = eligSummaryRows.map(r =>
      '<tr>' + ELIG_SUMMARY_COLS.map(c => '<td class="logic-cell">' + esc(r[c]) + '</td>').join('') + '</tr>'
    ).join('');
  };

  window.showEligSub = function(which){
    document.getElementById('elig-subtab-detail').classList.toggle('active', which === 'detail');
    document.getElementById('elig-subtab-summary').classList.toggle('active', which === 'summary');
    document.getElementById('elig-detail-wrap').style.display = which === 'detail' ? '' : 'none';
    document.getElementById('elig-summary-wrap').style.display = which === 'summary' ? '' : 'none';
  };

  renderEligDetail();
  renderEligSummary();
})();
</script>
"""


def append_eligibility_tab_to_html(html_path, df_detail, df_summary):
    """Best-effort, non-destructive injection - see module docstring above
    this function. Returns True on success, False if it skipped (file left
    untouched either way on failure)."""
    from string import Template
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        n = len(df_detail)
        button_html = (
            f'<button class="tab-btn" data-tab="eligibility" '
            f'onclick="showTab(\'eligibility\')">Eligibility Rules ({n} rows)</button>\n'
        )
        tabs_close_anchor = '\n</div>\n\n<div id="lineage" class="panel active">'
        if tabs_close_anchor not in html:
            return False
        html = html.replace(
            tabs_close_anchor,
            "\n" + button_html + '</div>\n\n<div id="lineage" class="panel active">',
            1,
        )

        panel = Template(_ELIGIBILITY_PANEL_TMPL).substitute(
            detail_cols=_json.dumps(ELIGIBILITY_DETAIL_COLS),
            summary_cols=_json.dumps(ELIGIBILITY_SUMMARY_COLS),
            detail_rows=_json.dumps(df_detail.to_dict(orient="records")),
            summary_rows=_json.dumps(df_summary.to_dict(orient="records")),
        )
        body_close = "\n</body>"
        if body_close in html:
            html = html.replace(body_close, panel + body_close, 1)
        else:
            html += panel

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    except Exception as e:
        print("Eligibility HTML tab injection skipped:", e)
        return False
