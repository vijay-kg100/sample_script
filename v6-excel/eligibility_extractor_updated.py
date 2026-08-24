"""
eligibility_extractor.py
=========================

Purely additive enhancement module (does not modify or replace any existing
behavior in lineage_engine.py / build_lineage.py / business_logic_enricher.py).
It reads from the same business-logic-source Excel workbook that
business_logic_enricher.py already loads, plus the parsed JSON/XML
repository (`folder_idx`) purely for structural facts the Excel doesn't
carry, and produces two new report tabs:

    "Eligibility Rules"            - full detail, one row per rule found
    "Eligibility Rules - Summary"  - Session | Mapping/Mapplet | bullet list

This module is written to track Eligibility_Rules_Requirements.md
("the spec") clause-for-clause, with exactly ONE deliberate, documented
deviation:

    DEVIATION: the spec (§3) describes pulling the raw technical
    expression/condition text for the "Eligibility Rule/Logic (Technical)"
    column directly off the parsed Mapping/Mapplet/Transformation objects
    in the JSON/XML repository. Per explicit product direction, this
    implementation instead sources that text from the SAME per-type sheets
    of the user-supplied --business-logic-excel workbook that
    business_logic_enricher.py already resolves via `_match_sheet_name`
    (Filter, Router, Update Strategy, Source Qualifier, Lookup,
    Transaction Control, Expression, Aggregator - and their Mapplet_<Type>
    counterparts). Column headers within each sheet are the SAME attribute
    names the spec names (`Filter Condition`, `Group Filter Condition`,
    `Lookup Condition`, `Lookup Sql Override`/`Lookup Sql Overide`, etc.),
    so every OTHER rule in the spec - which transformation types/attributes
    are read, the keyword/control-logic filter, alias resolution, plain
    language, output shape, dedup/sort, tab omission - is unaffected and
    implemented as written. The `Source (...)` provenance column reflects
    this honestly: its base value is `"Excel (Table Attribute)"`, not the
    spec's literal `"JSON (Table Attribute)"`, since Excel is the true
    source of the technical text in this build.

    Where the spec's alias-resolution fallback (§5.2) is explicitly
    "lineage-derived" (i.e. genuinely JSON/XML CONNECTOR-graph based, for
    Source Qualifier), that piece is still resolved from `folder_idx` -
    only the primary rule/condition TEXT itself comes from Excel.

Everything else below - scope of the scan (§2), which transformation types
are scanned and how (§3), the keyword/control-logic heuristic (§4), alias
resolution (§5), plain-language translation (§6), output row shape (§7),
Excel/HTML integration (§8), and the non-goals in §9 - is implemented to
match the requirements doc as closely as the Excel-sourced-text deviation
above allows.

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
# Output shape (spec §7)
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

# Category A (spec §3.A): always-reported, no keyword filter - any non-empty
# condition on these types IS eligibility-relevant by construction.
_CATEGORY_A = ("FILTER", "ROUTER", "UPDATE STRATEGY", "SOURCE QUALIFIER")

# Category B (spec §3.B): only reported if it passes the keyword +
# control-logic heuristic (spec §4).
_CATEGORY_B = ("LOOKUP", "TRANSACTION CONTROL", "EXPRESSION", "AGGREGATOR")

_ANCHORS = list(_CATEGORY_A) + list(_CATEGORY_B)

_DISPLAY_TYPE = {
    "FILTER": "Filter",
    "ROUTER": "Router",
    "UPDATE STRATEGY": "Update Strategy",
    "SOURCE QUALIFIER": "Source Qualifier",
    "LOOKUP": "Lookup",
    "TRANSACTION CONTROL": "Transaction Control",
    "EXPRESSION": "Expression",
    "AGGREGATOR": "Aggregator",
}

# Only these two types carry alias resolution (spec §5 header: "Alias
# resolution (Source Qualifier & Lookup only)").
_ALIAS_ELIGIBLE_ANCHORS = ("SOURCE QUALIFIER", "LOOKUP")


# --------------------------------------------------------------------------
# §4 Keyword / control-logic heuristic (category B only)
# --------------------------------------------------------------------------

# ~50 short, case-insensitive substrings covering the theme groups spec §4.1
# lists: eligibility/qualification; status/active/inactive/valid flags;
# age/DOB/birth; thresholds & min/max limits; tier/segment/plan/product
# codes; effective/expiry/termination date fields; enrollment/member/
# coverage terms; date-range fields; group/class/level/band/rank codes;
# approve/reject/decline/waiver/grace/lapse/renew/suspend/block/hold/
# risk/score/cutoff terms. Deliberately simple substring checks (spec §9 -
# "no NLP/ML", cheap to run across a whole repo).
ELIGIBILITY_KEYWORDS = [
    # eligibility / qualification
    "elig", "qualif", "disqualif",
    # status / active-inactive / valid flags
    "status", "active", "inactive", "valid", "invalid",
    # age / DOB / birth
    "age", "dob", "date of birth", "birth_dt", "birth dt", "birth",
    # thresholds & min/max limits
    "threshold", "min_age", "max_age", "minimum", "maximum", "limit", "cutoff",
    # tier / segment / plan / product codes
    "tier", "segment", "plan_cd", "plan code", "product_cd", "product code", "prod_cd",
    # effective / expiry / termination date fields
    "effective", "efctv", "eff_dt", "eff dt", "expir", "term_dt", "term dt", "termination",
    # enrollment / member / coverage terms
    "enroll", "member", "coverage",
    # date-range fields
    "start_dt", "end_dt", "start date", "end date", "as_of", "as of date",
    # group / class / level / band / rank codes
    "group_cd", "group code", "class_cd", "level", "band", "rank",
    # approve/reject/decline/waiver/grace/lapse/renew/suspend/block/hold/risk/score
    "approve", "reject", "decline", "waiver", "grace", "lapse", "renew",
    "suspend", "block", "hold", "risk", "score",
]

# Actual conditional/comparison construct required alongside a keyword hit
# (spec §4.2). A bare mention of an eligibility-sounding field name with no
# real logic attached does not qualify.
_CONTROL_LOGIC_RE = re.compile(
    r"\bIIF\s*\(|\bDECODE\s*\(|\bCASE\b|>=|<=|<>|!=|\bIN\s*\(|\bBETWEEN\b|=\s*'",
    re.IGNORECASE,
)


def _clean(s):
    if s is None:
        return ""
    if isinstance(s, float) and pd.isna(s):
        return ""
    return " ".join(str(s).split())


def _has_keyword(text):
    if not text:
        return False
    t = text.lower()
    return any(term in t for term in ELIGIBILITY_KEYWORDS)


def _has_control_logic(text):
    if not text:
        return False
    return bool(_CONTROL_LOGIC_RE.search(text))


def _passes_category_b_filter(check_text):
    """Both the keyword AND the control-logic check must pass on the SAME
    text (spec §4: 'both of the following are true')."""
    return _has_keyword(check_text) and _has_control_logic(check_text)


# --------------------------------------------------------------------------
# §6 Plain-language translation (best effort, rule based)
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
    (re.compile(r">=\s*"), " is at least "),
    (re.compile(r"<=\s*"), " is at most "),
    (re.compile(r"!=\s*|<>\s*"), " is not equal to "),
    (re.compile(r"(?<![<>=!])=\s*"), " equals "),
    (re.compile(r">\s*"), " greater than "),
    (re.compile(r"<\s*"), " less than "),
]

_KEYWORD_RE = re.compile(
    r"\bIS\s+NOT\s+NULL\b|\bIS\s+NULL\b|\bNOT\s+IN\b|\bIN\s*\(|\bBETWEEN\b|\bAND\b|\bOR\b|\bNOT\b",
    re.IGNORECASE,
)

_KEYWORD_REPL = {
    "is not null": "is not blank",
    "is null": "is blank",
    "not in": "is not one of",
    "and": "AND",
    "or": "OR",
    "not": "NOT",
    "between": "is between",
}


def _keyword_sub(m):
    key = m.group(0).lower()
    if key.startswith("in"):
        # "IN (" -> "is one of ("
        return "is one of ("
    key = re.sub(r"\s+", " ", key).strip()
    return _KEYWORD_REPL.get(key, m.group(0))


def _translate_leaf(cond):
    """Translate one condition fragment with no top-level IIF/DECODE call
    left in it - operator + keyword substitution only (spec §6.4)."""
    if not cond:
        return ""
    out = cond
    out = _KEYWORD_RE.sub(_keyword_sub, out)
    out = re.sub(r"\bISNULL\s*\(\s*([^()]+?)\s*\)", r"\1 is blank", out, flags=re.IGNORECASE)
    for pat, word in _OP_WORDS:
        out = pat.sub(word, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _plain_language(expr, depth=0):
    """Best-effort rule-based technical -> business-readable translation
    (spec §6). Recurses into IIF/DECODE; falls back to operator/keyword
    substitution for everything else. Never raises - on any parsing
    surprise it returns the (lightly cleaned) original text instead of
    guessing further (spec §9)."""
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
            has_false = len(args) > 2
            false_val = args[2].strip().strip("'\"") if has_false else ""
            false_clause = false_val if (has_false and false_val) else "no change / leave as-is"
            text = f"If {cond_plain}, then result is {true_val}; otherwise {false_clause}."
            return _finalize_sentence(text)
        return _finalize_sentence(_translate_leaf(e))

    m = re.match(r"^DECODE\s*\((.*)\)\s*$", e, re.IGNORECASE | re.DOTALL)
    if m:
        args = _split_top_level(m.group(1))
        if len(args) >= 2:
            subject = args[0].strip()
            pairs = args[1:]
            clauses = []
            i = 0
            while i + 1 < len(pairs):
                val = pairs[i].strip().strip("'\"")
                res = pairs[i + 1].strip().strip("'\"")
                clauses.append(f"If {subject} equals {val} then result is {res}")
                i += 2
            has_default = i < len(pairs)
            default = pairs[i].strip().strip("'\"") if has_default else ""
            text = "; ".join(clauses)
            if has_default and default:
                text += f"; otherwise {default}."
            else:
                text += "."
            return _finalize_sentence(text)
        return _finalize_sentence(_translate_leaf(e))

    # No top-level IIF/DECODE - straight operator/keyword translation.
    return _finalize_sentence(_translate_leaf(e))


def _finalize_sentence(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    if text and not text.endswith("."):
        text += "."
    return text


# --------------------------------------------------------------------------
# §5 Alias resolution (Source Qualifier & Lookup ONLY)
# --------------------------------------------------------------------------

_SQL_ALIAS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_\.]+)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_SQL_KEYWORDS_LOOSE = {
    "where", "on", "join", "group", "order", "having", "union", "and", "or",
    "set", "select", "from", "values", "into", "as", "inner", "outer",
    "left", "right",
}

_DOT_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(?=[A-Za-z_])")
_NON_ALIAS_TOKENS = {_norm(x) for x in ("iif", "decode", "true", "false")}

_ACTUAL_ANNOTATION_RE = re.compile(r"\b[A-Za-z0-9_]+\s*\(Actual:\s*([^)]+)\)\.")


def _sql_alias_map(sql_text):
    """§5.1 text-derived aliases: {alias -> real_table} parsed straight out
    of the SQL/join text's own FROM/JOIN ... [AS] alias clauses. Skips SQL
    keywords accidentally matched as an alias, and skips a "match" where
    the alias equals the table name itself."""
    out = {}
    if not sql_text:
        return out
    for real_table, alias in _SQL_ALIAS_RE.findall(sql_text):
        if alias.lower() in _SQL_KEYWORDS_LOOSE:
            continue
        real = real_table.split(".")[-1]
        if _norm(alias) == _norm(real):
            continue
        out[_norm(alias)] = real
    return out


def _sq_real_table_from_json(folder_idx, scope_name, sq_instance_name, mapplet_mode):
    """§5.2a lineage-derived fallback for Source Qualifier: trace the SQ
    instance's immediate upstream SOURCE definition via the mapping's (or
    mapplet's) own CONNECTOR graph in the parsed JSON/XML repository."""
    if not folder_idx or not scope_name:
        return ""
    try:
        node_dict = folder_idx["mapplets"] if mapplet_mode else folder_idx["mappings"]
        scope_node = node_dict.get(scope_name)
        if not scope_node:
            return ""
        idx = index_mapping(scope_node)
        for c in idx["connectors"]:
            if c.get("TOINSTANCE") == sq_instance_name:
                frm = c.get("FROMINSTANCE")
                inst = idx["instances"].get(frm)
                if inst and (inst.get("type") or "").upper() == "SOURCE":
                    src = folder_idx["sources"].get(inst.get("transformation_name"))
                    if src:
                        return src["table"]
        return ""
    except Exception:
        return ""


def _resolve_and_annotate(anchor, text, trans_name, row_table_hint, folder_idx,
                           scope_name, mapplet_mode):
    """Implements spec §5, points 1-4, for Source Qualifier / Lookup rows
    only. Returns (annotated_text, cross_verified: bool, unresolved: bool).

    `row_table_hint` is this row's own primary table:
      - Source Qualifier: filled in lazily below via the JSON CONNECTOR
        graph (spec §5.2a) since the Excel sheet doesn't reliably carry it.
      - Lookup: the row's own "Lookup Table Name" column value (spec §5.2b),
        used only when it differs from the instance name.
    """
    if anchor not in _ALIAS_ELIGIBLE_ANCHORS or not text:
        return text, False, False

    text_alias_map = _sql_alias_map(text)  # §5.1, wins on conflict

    lineage_alias_map = {}
    dotted_tokens = set(_DOT_REF_RE.findall(text))

    if anchor == "SOURCE QUALIFIER":
        for tok in dotted_tokens:
            ntok = _norm(tok)
            if ntok in text_alias_map:
                continue
            real = _sq_real_table_from_json(folder_idx, scope_name, trans_name, mapplet_mode)
            if real:
                lineage_alias_map[ntok] = real
    elif anchor == "LOOKUP":
        if row_table_hint and _norm(row_table_hint) != _norm(trans_name):
            for tok in dotted_tokens:
                ntok = _norm(tok)
                if ntok not in text_alias_map:
                    lineage_alias_map[ntok] = row_table_hint

    merged = dict(lineage_alias_map)
    merged.update(text_alias_map)  # §5.1: text-derived wins over lineage-derived

    known_reals = {_norm(v) for v in merged.values()}
    known_reals.add(_norm(trans_name))

    out = text
    cross_verified = False
    unresolved = False
    for tok in sorted(dotted_tokens, key=len, reverse=True):
        ntok = _norm(tok)
        if ntok in _NON_ALIAS_TOKENS:
            continue
        real = merged.get(ntok)
        if real:
            out = re.sub(rf"\b{re.escape(tok)}\.", f"{tok} (Actual: {real}).", out)
            if ntok not in text_alias_map:
                # resolved only via the lineage fallback, not spelled out
                # in the text itself (spec §5.4)
                cross_verified = True
        elif ntok in known_reals:
            # not an alias - it's already a known real table/transformation
            # name in the repo (spec §5.3, second bullet); leave untouched.
            continue
        elif len(tok) <= 12:
            # short, alias-shaped token with no resolution anywhere -
            # flag it inline, never silently guess (spec §5.3, §9).
            out = re.sub(rf"\b{re.escape(tok)}\.", f"{tok} [alias unresolved].", out)
            unresolved = True

    return out, cross_verified, unresolved


def _strip_actual_annotation(text):
    """Spec §6.1: for plain-language output, strip the 'ALIAS (Actual:
    RealTable).' annotation down to just 'RealTable.'."""
    if not text:
        return text
    return _ACTUAL_ANNOTATION_RE.sub(lambda m: f"{m.group(1)}.", text)


# --------------------------------------------------------------------------
# Mapplet usage (spec §2.2-2.4): which mapping(s) call a given mapplet, and
# what session(s) run each of those mappings.
# --------------------------------------------------------------------------

def build_mapplet_usage(folder_idx, session_to_mapping):
    """{mapplet_name -> [mapping_name, ...]} - the set of DISTINCT mappings
    that call this mapplet at least once. Dedupes multiple INSTANCEs of the
    same mapplet within one mapping down to a single entry (spec §2.3),
    which also means the mapplet's own scan result is effectively reused
    once per calling mapping rather than re-derived per instance (spec
    §2.4 - the underlying per-mapplet workbook rows are in any case only
    ever read once, up front, regardless of how many mappings call them)."""
    usage = defaultdict(list)
    seen = defaultdict(set)
    if not folder_idx:
        return usage

    for mapping_name, mapping_node in folder_idx.get("mappings", {}).items():
        try:
            idx = index_mapping(mapping_node)
        except Exception:
            continue
        mapplets_in_this_mapping = set()
        for inst in idx["instances"].values():
            if (inst.get("type") or "").upper() == "MAPPLET":
                mapplet_name = inst.get("transformation_name")
                if mapplet_name:
                    mapplets_in_this_mapping.add(mapplet_name)
        for mapplet_name in mapplets_in_this_mapping:
            if mapping_name not in seen[mapplet_name]:
                seen[mapplet_name].add(mapping_name)
                usage[mapplet_name].append(mapping_name)
    return usage


def _build_mapping_to_sessions(session_to_mapping):
    """{mapping_name -> 'S1,S2,...'} comma-joined, sorted, deduplicated
    (spec §2, "Sessions column")."""
    mapping_to_sessions = defaultdict(set)
    for session_name, mapping_name in session_to_mapping.items():
        if mapping_name:
            mapping_to_sessions[mapping_name].add(session_name)
    return {m: ",".join(sorted(s for s in sessions if s)) for m, sessions in mapping_to_sessions.items()}


# --------------------------------------------------------------------------
# §3 Per-type row scanners - each returns a list of candidate dicts for ONE
# workbook row:
#   {"technical": str, "table_hint": str, "name_suffix": str, "port": str}
# `table_hint` (Lookup only) is this row's own configured table, used to
# resolve aliases referenced inside its OWN condition text (spec §5.2b).
# `name_suffix` / `port` feed the Transformation Name column (spec §7.3).
# --------------------------------------------------------------------------

def _row_candidates_filter(cols, r):
    cond_col = _find_col(cols, ["Filter Condition"])
    text = _clean(r.get(cond_col)) if cond_col else ""
    if not text:
        return []
    return [{"technical": text, "table_hint": "", "name_suffix": "", "port": ""}]


def _row_candidates_router(cols, r):
    grp_col = _find_col(cols, ["Group Name"])
    cond_col = _find_col(cols, ["Group Filter Condition", "Filter Condition", "Condition"])
    grp = _clean(r.get(grp_col)) if grp_col else ""
    cond = _clean(r.get(cond_col)) if cond_col else ""
    if not cond:
        return []
    suffix = f" [Group: {grp}]" if grp else ""
    return [{"technical": cond, "table_hint": "", "name_suffix": suffix, "port": ""}]


def _row_candidates_update_strategy(cols, r):
    expr_col = _find_col(cols, ["Update Strategy Expression"])
    text = _clean(r.get(expr_col)) if expr_col else ""
    if not text:
        return []
    return [{"technical": text, "table_hint": "", "name_suffix": "", "port": ""}]


def _row_candidates_source_qualifier(cols, r):
    sql_col = _find_col(cols, ["SQL Query", "Sql Override"])
    filt_col = _find_col(cols, ["Source Filter"])
    uj_col = _find_col(cols, ["User Defined Join"])
    sql = _clean(r.get(sql_col)) if sql_col else ""
    filt = _clean(r.get(filt_col)) if filt_col else ""
    uj = _clean(r.get(uj_col)) if uj_col else ""

    has_where = bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE)) if sql else False
    # spec §3.A: emit only if Source Filter present, OR User Defined Join
    # present, OR the SQL override text contains a WHERE clause.
    if not (filt or uj or has_where):
        return []

    parts = []
    if sql:
        parts.append(f"SQL Override: {sql}")
    if filt:
        parts.append(f"Source Filter: {filt}")
    if uj:
        parts.append(f"User Defined Join: {uj}")
    if not parts:
        return []
    return [{"technical": "; ".join(parts), "table_hint": "", "name_suffix": "", "port": ""}]


def _row_candidates_lookup(cols, r):
    cond_col = _find_col(cols, ["Lookup Condition"])
    sql_col = _find_col(cols, ["Lookup Sql Override", "Lookup Sql Overide"])
    table_col = _find_col(cols, ["Lookup Table Name"])
    cond = _clean(r.get(cond_col)) if cond_col else ""
    sql = _clean(r.get(sql_col)) if sql_col else ""
    table = _clean(r.get(table_col)) if table_col else ""

    parts = []
    if cond:
        parts.append(f"Lookup Condition: {cond}")
    if sql:
        parts.append(f"Lookup Sql Override: {sql}")
    if not parts:
        return []
    return [{"technical": "; ".join(parts), "table_hint": table, "name_suffix": "", "port": ""}]


def _row_candidates_transaction_control(cols, r):
    cond_col = _find_col(cols, ["Transaction Control Condition"])
    text = _clean(r.get(cond_col)) if cond_col else ""
    if not text:
        return []
    return [{"technical": text, "table_hint": "", "name_suffix": "", "port": ""}]


def _row_candidates_expression_like(cols, r):
    """Shared by Expression and Aggregator - both are evaluated per output
    port (spec §3.B)."""
    port_col = _find_col(cols, ["PORT_NAME", "Port Name", "Port"])
    expr_col = _find_col(cols, ["Expression", "Aggregator Expression"])
    port = _clean(r.get(port_col)) if port_col else ""
    expr = _clean(r.get(expr_col)) if expr_col else ""
    if not expr:
        return []
    return [{"technical": expr, "table_hint": "", "name_suffix": f".{port}" if port else "", "port": port}]


_ROW_SCANNERS = {
    "FILTER": _row_candidates_filter,
    "ROUTER": _row_candidates_router,
    "UPDATE STRATEGY": _row_candidates_update_strategy,
    "SOURCE QUALIFIER": _row_candidates_source_qualifier,
    "LOOKUP": _row_candidates_lookup,
    "TRANSACTION CONTROL": _row_candidates_transaction_control,
    "EXPRESSION": _row_candidates_expression_like,
    "AGGREGATOR": _row_candidates_expression_like,
}


# --------------------------------------------------------------------------
# Main extraction (spec §2, §7)
# --------------------------------------------------------------------------

def extract_eligibility_rows(workbook, folder_idx, session_to_mapping, mapping_to_session):
    """Returns (df_detail, df_summary) - the two new Excel/HTML tabs.

    `workbook`            : {sheet_name: DataFrame}, from
                             business_logic_enricher.load_source_workbook -
                             the SAME workbook object already loaded by
                             build_lineage.py when --business-logic-excel is
                             supplied. This is the source of every rule's
                             raw technical text (see module docstring for
                             the documented deviation from the spec's
                             JSON/XML-sourced description of this column).
    `folder_idx`           : from lineage_engine.index_folder. Used for:
                             (a) resolving which mapping(s) call a given
                             mapplet, so mapplet-scoped rows get correct
                             Session / Mapping->Mapplet values (spec
                             §2.2-2.4), and (b) the Source-Qualifier
                             lineage-derived alias fallback (spec §5.2a).
    `session_to_mapping`   : the UNRESTRICTED {session: mapping} map
                             covering every session/workflow in the
                             repository (spec §2 - "not ... the
                             execution-order window of some anchor
                             target"), as already built by
                             build_lineage.build_lineage() across every
                             workflow before any target/session-window
                             restriction is applied.
    `mapping_to_session`   : unused for Session-column purposes here (kept
                             in the signature for backward compatibility
                             with build_lineage.py's call site); the
                             Session column is derived from
                             `session_to_mapping` directly so a mapping run
                             by more than one session gets ALL of them,
                             comma-joined (spec §2 - "Sessions column").
    """
    empty_d = pd.DataFrame(columns=ELIGIBILITY_DETAIL_COLS)
    empty_s = pd.DataFrame(columns=ELIGIBILITY_SUMMARY_COLS)
    if not workbook:
        return empty_d, empty_s

    mapping_to_sessions_joined = _build_mapping_to_sessions(session_to_mapping or {})
    mapplet_usage = build_mapplet_usage(folder_idx, session_to_mapping or {}) if folder_idx else {}

    detail_rows = []

    for mapplet_mode in (False, True):
        for anchor in _ANCHORS:
            sheet_name = _match_sheet_name(workbook, anchor, mapplet_mode)
            if not sheet_name:
                continue
            df = workbook[sheet_name]
            cols = list(df.columns)

            mapping_col = _find_col(cols, ["Mapping Name", "Mapping"])
            mapplet_col = _find_col(cols, ["Mapplet Name", "Mapplet"])
            trans_col = _find_col(cols, ["Transformation Name", "Instance Name"])
            session_col = _find_col(cols, ["Session Name", "Session"])
            scanner = _ROW_SCANNERS[anchor]
            is_category_a = anchor in _CATEGORY_A

            for _, r in df.iterrows():
                try:
                    trans_name = _clean(r.get(trans_col)) if trans_col else ""
                    candidates = scanner(cols, r)
                    if not candidates:
                        continue

                    if mapplet_mode:
                        mapplet_name = _clean(r.get(mapplet_col)) if mapplet_col else ""
                        scope_names = mapplet_usage.get(mapplet_name, []) or [""]
                    else:
                        scope_names = [_clean(r.get(mapping_col)) if mapping_col else ""]

                    for cand in candidates:
                        text = cand["technical"]
                        if not text:
                            continue

                        # --- §3/§4: category A always reported; category B
                        #     needs the keyword + control-logic heuristic.
                        if not is_category_a:
                            if anchor in ("EXPRESSION", "AGGREGATOR"):
                                check_text = f"{cand['port']} {text}".strip()
                            else:
                                check_text = text
                            if not _passes_category_b_filter(check_text):
                                continue

                        row_trans_name = f"{trans_name}{cand['name_suffix']}"

                        for scope_name in scope_names:
                            # --- §5: alias resolution (SQ & Lookup only) ---
                            technical, cross_verified, unresolved = _resolve_and_annotate(
                                anchor, text, trans_name, cand["table_hint"],
                                folder_idx, scope_name, mapplet_mode,
                            )

                            # --- §6: plain-language gloss ---
                            plain = _plain_language(_strip_actual_annotation(technical))

                            # --- §7.2/§7.3/§2.1: Mapping/Mapplet + Session ---
                            if mapplet_mode:
                                if scope_name:
                                    mapping_display = f"{scope_name} \u2192 {mapplet_name}"
                                    session_name = mapping_to_sessions_joined.get(scope_name, "")
                                else:
                                    mapping_display = f"[mapping unresolved] \u2192 {mapplet_name}"
                                    session_name = _clean(r.get(session_col)) if session_col else ""
                            else:
                                mapping_display = scope_name
                                session_name = mapping_to_sessions_joined.get(scope_name, "")
                                if not session_name and session_col:
                                    session_name = _clean(r.get(session_col))

                            # --- §7.7 / §5.4: Source provenance ---
                            source_tag = "Excel (Table Attribute)"
                            if mapplet_mode and scope_name:
                                source_tag += " (Mapping resolved via JSON/XML lineage)"
                            if cross_verified:
                                source_tag += " - alias cross-verified via lineage"
                            if unresolved:
                                source_tag += " - alias unresolved"

                            detail_rows.append({
                                "Session": session_name,
                                "Mapping/Mapplet": mapping_display,
                                "Transformation Name": row_trans_name,
                                "Transformation Type": _DISPLAY_TYPE[anchor],
                                "Eligibility Rule/Logic (Technical)": technical,
                                "Eligibility Rule/Logic (Plain Language)": plain,
                                "Source (Excel/JSON/XML)": source_tag,
                            })
                except Exception:
                    # Never let one malformed row take down the whole scan.
                    continue

    if not detail_rows:
        return empty_d, empty_s

    df_detail = pd.DataFrame(detail_rows, columns=ELIGIBILITY_DETAIL_COLS)
    # spec §7 "Dedup & sort": drop exact duplicate rows (all 7 columns
    # identical), then sort by (Mapping/Mapplet, Transformation Name).
    df_detail = df_detail.drop_duplicates().reset_index(drop=True)
    df_detail = df_detail.sort_values(
        by=["Mapping/Mapplet", "Transformation Name"], kind="stable"
    ).reset_index(drop=True)

    # --- Summary tab: bullet list per (Session, Mapping/Mapplet) ---
    # spec §7 summary: "Preserve first-seen order of (Session,
    # Mapping/Mapplet) pairs from the detail rows" - detail rows are
    # already in their final sorted order at this point.
    grouped = defaultdict(list)
    order = []
    for row in df_detail.to_dict(orient="records"):
        key = (row["Session"], row["Mapping/Mapplet"])
        if key not in grouped:
            order.append(key)
        bullet = f"\u2022 [{row['Transformation Name']} ({row['Transformation Type']})] {row['Eligibility Rule/Logic (Plain Language)']}"
        if bullet not in grouped[key]:
            grouped[key].append(bullet)

    summary_rows = []
    for key in order:
        session, mapping = key
        summary_rows.append({
            "Session": session,
            "Mapping/Mapplet": mapping,
            "Eligibility Rules/Logics": "\n".join(grouped[key]),
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
#
# Per spec §1/§8: if no eligibility logic was found anywhere, both tabs
# must be omitted entirely (no empty tab) - this function no-ops (returns
# False without touching the file) whenever df_detail is empty.
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
    if(!el) return;
    el.innerHTML = cols.map(c => '<th>' + esc(c) + '</th>').join('');
  }

  window.renderEligDetail = function(){
    const head = document.getElementById('elig-detail-head');
    const body = document.getElementById('elig-detail-body');
    if(!head || !body || !eligDetailRows.length) return;
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
    if(!head || !body || !eligSummaryRows.length) return;
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
        if df_detail is None or len(df_detail) == 0:
            # spec §1/§8: no eligibility logic found -> omit the tab
            # entirely, everywhere. Leave the HTML file untouched.
            return False

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
