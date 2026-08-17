"""Domain / object model for parsed Informatica PowerCenter workflows.

These classes are intentionally simple, serializable (via to_dict) containers.
They are format-agnostic: both the XML parser and the JSON parser build the
exact same object graph, so everything downstream (analyzer, graph builder,
document/report generators) never needs to know which format the workflow
was uploaded in.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class Field:
    name: str
    datatype: str = ""
    precision: str = ""
    scale: str = ""
    port_type: str = ""  # for transformation ports: INPUT / OUTPUT / INPUT/OUTPUT / VARIABLE
    expression_type: str = ""  # for transformation ports: GENERAL / GROUPBY / ... (from TRANSFORMFIELD@EXPRESSIONTYPE)

    def to_dict(self):
        return {"name": self.name, "datatype": self.datatype, "precision": self.precision,
                "scale": self.scale, "port_type": self.port_type, "expression_type": self.expression_type}


@dataclass
class Table:
    name: str
    kind: str  # SOURCE | TARGET
    connection: str = ""
    database: str = ""
    schema: str = ""
    fields: List[Field] = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "kind": self.kind, "connection": self.connection,
                "database": self.database, "schema": self.schema,
                "fields": [f.to_dict() for f in self.fields]}


@dataclass
class Transformation:
    name: str
    type: str = ""
    reusable: bool = False
    input_ports: List[Field] = field(default_factory=list)
    output_ports: List[Field] = field(default_factory=list)
    variable_ports: List[Field] = field(default_factory=list)
    expressions: List[Dict] = field(default_factory=list)     # [{port, expression}]
    attributes: Dict[str, str] = field(default_factory=dict)  # TABLEATTRIBUTE name->value
    business_logic: str = ""
    implementation_notes: str = ""
    mapping_name: str = ""
    mapplet_name: Optional[str] = None

    def to_dict(self):
        return {
            "name": self.name, "type": self.type, "reusable": self.reusable,
            "input_ports": [p.to_dict() for p in self.input_ports],
            "output_ports": [p.to_dict() for p in self.output_ports],
            "variable_ports": [p.to_dict() for p in self.variable_ports],
            "expressions": self.expressions, "attributes": self.attributes,
            "business_logic": self.business_logic,
            "implementation_notes": self.implementation_notes,
            "mapping_name": self.mapping_name, "mapplet_name": self.mapplet_name,
        }


@dataclass
class Connector:
    from_instance: str
    from_field: str
    to_instance: str
    to_field: str
    # PowerCenter's CONNECTOR element also carries FROMINSTANCETYPE /
    # TOINSTANCETYPE attributes. These are the only way to tell apart two
    # different canvas objects that share the same NAME -- e.g. a Source
    # instance and a Target instance both called "M2R_DTM_TRANSACTION"
    # (legal in Designer, since Source and Target instance names live in
    # separate namespaces). Without these, instance lookups keyed on name
    # alone silently collapse the two into one. Optional/blank for older
    # exports or hand-built JSON that don't carry them.
    from_instance_type: str = ""
    to_instance_type: str = ""

    def to_dict(self):
        return {"from_instance": self.from_instance, "from_field": self.from_field,
                "to_instance": self.to_instance, "to_field": self.to_field,
                "from_instance_type": self.from_instance_type, "to_instance_type": self.to_instance_type}


@dataclass
class Instance:
    """A single object instance placed inside a Mapping/Mapplet canvas."""
    name: str
    type: str          # SOURCE | TARGET | TRANSFORMATION | MAPPLET
    ref_name: str       # underlying TRANSFORMATION_NAME / SOURCE / TARGET / MAPPLET name
    ref_type: str = ""  # underlying transformation type e.g. 'Expression', 'Source Qualifier'

    def to_dict(self):
        return {"name": self.name, "type": self.type, "ref_name": self.ref_name, "ref_type": self.ref_type}


def index_instances(instances: "List[Instance]"):
    """Builds the two lookup structures every lineage/reportability trace
    needs to resolve a CONNECTOR's from_instance/to_instance name back to
    the actual Instance object:

      - by_name_type: dict[(name, type)] -> Instance -- exact, collision-free
      - by_name:      dict[name] -> Instance -- fallback for the (overwhelming
        majority) of call sites that only have a name, no type. Last instance
        with that name wins, same as the old plain-dict behaviour, so callers
        that never hit a collision see no change at all.

    A name collision (e.g. a Source and a Target both named
    "M2R_DTM_TRANSACTION") is only resolved correctly by callers that pass a
    type_hint into resolve_instance() below -- which requires reading
    FROMINSTANCETYPE/TOINSTANCETYPE off the connector that pointed at this
    instance, not just its name.
    """
    by_name_type = {}
    by_name = {}
    for inst in instances:
        by_name_type[(inst.name, inst.type)] = inst
        by_name[inst.name] = inst
    return by_name, by_name_type


def resolve_instance(by_name: dict, by_name_type: dict, name: str, type_hint: str = ""):
    """Resolves an instance NAME to its Instance object, disambiguating a
    Source/Target (or any type) name collision when a type_hint is available
    (normally read off the CONNECTOR edge that led here). Falls back to the
    old name-only behaviour when no hint is given or the hint doesn't match
    anything, so this is a strict improvement, never a regression."""
    if type_hint:
        inst = by_name_type.get((name, type_hint))
        if inst is not None:
            return inst
    return by_name.get(name)


_CONNECTOR_TYPE_TO_INSTANCE_TYPE = {
    "source definition": "SOURCE",
    "target definition": "TARGET",
    "mapplet": "MAPPLET",
}


def normalize_connector_instance_type(raw: str) -> str:
    """CONNECTOR's FROMINSTANCETYPE/TOINSTANCETYPE attributes carry
    PowerCenter's detailed object-type strings -- "Source Definition",
    "Target Definition", "Mapplet", or (for anything sitting on a
    Transformation instance) the specific transformation type itself, e.g.
    "Expression", "Source Qualifier", "Filter", "Lookup Procedure",
    "Router", "Joiner", "Union", "Custom Transformation", "Sequence
    Generator", "Update Strategy", "Aggregator", "Sorter", "Rank",
    "Normalizer", "Stored Procedure", "Transaction Control", "External
    Procedure", "XML Generator", "XML Parser", "Application Source
    Qualifier", and so on.

    This is a *different* vocabulary from INSTANCE's own TYPE attribute,
    which only ever holds SOURCE / TARGET / TRANSFORMATION / MAPPLET.
    Comparing the two directly would never match. Normalize the connector's
    detailed string down to that same coarse enum so a type_hint read off a
    connector can be compared against an Instance.type."""
    if not raw:
        return ""
    mapped = _CONNECTOR_TYPE_TO_INSTANCE_TYPE.get(raw.strip().lower())
    if mapped:
        return mapped
    # Every other detailed string names some flavor of Transformation
    # instance (Expression, Source Qualifier, Filter, Lookup, ...).
    return "TRANSFORMATION"


@dataclass
class Mapplet:
    name: str
    instances: List[Instance] = field(default_factory=list)
    connectors: List[Connector] = field(default_factory=list)
    transformations: List[str] = field(default_factory=list)  # transformation names owned by this mapplet

    def to_dict(self):
        return {"name": self.name, "instances": [i.to_dict() for i in self.instances],
                "connectors": [c.to_dict() for c in self.connectors],
                "transformations": self.transformations}


@dataclass
class Mapping:
    name: str
    instances: List[Instance] = field(default_factory=list)
    connectors: List[Connector] = field(default_factory=list)
    transformations: List[str] = field(default_factory=list)
    mapplets: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "instances": [i.to_dict() for i in self.instances],
                "connectors": [c.to_dict() for c in self.connectors],
                "transformations": self.transformations, "mapplets": self.mapplets,
                "sources": self.sources, "targets": self.targets}


@dataclass
class Session:
    name: str
    mapping_name: str = ""
    reusable: bool = False

    def to_dict(self):
        return {"name": self.name, "mapping_name": self.mapping_name, "reusable": self.reusable}


@dataclass
class WorkflowLink:
    from_task: str
    to_task: str
    condition: str = ""

    def to_dict(self):
        return {"from_task": self.from_task, "to_task": self.to_task, "condition": self.condition}


@dataclass
class TaskInstance:
    name: str
    task_name: str
    task_type: str  # Session | Command | Decision | Start | Email | ...

    def to_dict(self):
        return {"name": self.name, "task_name": self.task_name, "task_type": self.task_type}


@dataclass
class Workflow:
    name: str
    source_file: str = ""
    sessions: Dict[str, Session] = field(default_factory=dict)
    task_instances: List[TaskInstance] = field(default_factory=list)
    links: List[WorkflowLink] = field(default_factory=list)
    execution_order: List[str] = field(default_factory=list)  # ordered TASKINSTANCE names
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "source_file": self.source_file,
                "sessions": {k: v.to_dict() for k, v in self.sessions.items()},
                "task_instances": [t.to_dict() for t in self.task_instances],
                "links": [l.to_dict() for l in self.links],
                "execution_order": self.execution_order,
                "warnings": self.warnings}


@dataclass
class RepositoryModel:
    """Root aggregate returned by any parser: everything extracted from one upload."""
    workflow: Optional[Workflow] = None
    mappings: Dict[str, Mapping] = field(default_factory=dict)
    mapplets: Dict[str, Mapplet] = field(default_factory=dict)
    transformations: Dict[str, Transformation] = field(default_factory=dict)  # key: mapping_name::transform_name
    reusable_transformations: Dict[str, Transformation] = field(default_factory=dict)  # folder-level reusable
    sources: Dict[str, Table] = field(default_factory=dict)
    targets: Dict[str, Table] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def transformation(self, mapping_name: str, transform_name: str) -> Optional[Transformation]:
        t = self.transformations.get(f"{mapping_name}::{transform_name}")
        if t is not None:
            return t
        return self.reusable_transformations.get(transform_name)
