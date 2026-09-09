"""Durable, dependency-light build graph primitives.

This module is intentionally additive.  It does not know about StudioRunner,
agents, HTTP routes, or manifests; callers adapt those systems into node
handlers while this module owns graph validation, persistence, and scheduling.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath
from time import time
from typing import Any, cast


class NodeStatus(StrEnum):
    """Durable lifecycle states for a graph node (and compatible run states)."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    CACHED = "cached"


@dataclass(frozen=True, slots=True)
class GraphNodeSpec:
    """Immutable definition of one executable node in a build graph."""

    id: str
    kind: str
    deps: tuple[str, ...] = ()
    input_schema: str | None = None
    output_schema: str | None = None
    mutates_workspace: bool = False
    write_set: tuple[str, ...] = ()
    max_retries: int = 0
    cacheable: bool = True
    required: bool = True
    concurrency_group: str = ""
    concurrency_limit: int = 0
    subgraph_depth: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id).strip())
        object.__setattr__(self, "kind", str(self.kind).strip())
        object.__setattr__(self, "deps", tuple(str(dep).strip() for dep in self.deps))
        object.__setattr__(
            self,
            "write_set",
            tuple(str(path).strip() for path in self.write_set),
        )
        object.__setattr__(self, "max_retries", int(self.max_retries))
        object.__setattr__(self, "concurrency_group", str(self.concurrency_group).strip())
        object.__setattr__(self, "concurrency_limit", int(self.concurrency_limit))
        object.__setattr__(self, "subgraph_depth", int(self.subgraph_depth))
        if not self.id:
            raise ValueError("node id must not be empty")
        if not self.kind:
            raise ValueError(f"node {self.id!r} kind must not be empty")
        if any(not dep for dep in self.deps):
            raise ValueError(f"node {self.id!r} has an empty dependency")
        if len(set(self.deps)) != len(self.deps):
            raise ValueError(f"node {self.id!r} has duplicate dependencies")
        if any(not path for path in self.write_set):
            raise ValueError(f"node {self.id!r} has an empty write-set path")
        if self.max_retries < 0:
            raise ValueError(f"node {self.id!r} max_retries must be non-negative")
        if self.concurrency_group and self.concurrency_limit < 1:
            raise ValueError(f"node {self.id!r} concurrency group needs a positive limit")
        if not self.concurrency_group and self.concurrency_limit:
            raise ValueError(f"node {self.id!r} concurrency limit needs a group")
        if self.subgraph_depth < 0:
            raise ValueError(f"node {self.id!r} subgraph depth must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "deps": list(self.deps),
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "mutates_workspace": self.mutates_workspace,
            "write_set": list(self.write_set),
            "max_retries": self.max_retries,
            "cacheable": self.cacheable,
            "required": self.required,
            "concurrency_group": self.concurrency_group,
            "concurrency_limit": self.concurrency_limit,
            "subgraph_depth": self.subgraph_depth,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GraphNodeSpec:
        return cls(
            id=str(raw["id"]),
            kind=str(raw["kind"]),
            deps=tuple(str(dep) for dep in raw.get("deps", ())),
            input_schema=_optional_text(raw.get("input_schema")),
            output_schema=_optional_text(raw.get("output_schema")),
            mutates_workspace=bool(raw.get("mutates_workspace", False)),
            write_set=tuple(str(path) for path in raw.get("write_set", ())),
            max_retries=int(raw.get("max_retries", 0)),
            cacheable=bool(raw.get("cacheable", True)),
            required=bool(raw.get("required", True)),
            concurrency_group=str(raw.get("concurrency_group", "")),
            concurrency_limit=int(raw.get("concurrency_limit", 0)),
            subgraph_depth=int(raw.get("subgraph_depth", 0)),
        )


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    """A validated directed acyclic graph."""

    graph_id: str
    nodes: tuple[GraphNodeSpec, ...]
    version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", str(self.graph_id).strip())
        object.__setattr__(self, "version", str(self.version).strip())
        object.__setattr__(self, "nodes", tuple(self.nodes))
        if not self.graph_id:
            raise ValueError("graph_id must not be empty")
        if not self.version:
            raise ValueError("graph version must not be empty")

        ids = [node.id for node in self.nodes]
        duplicate_ids = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicate_ids:
            raise ValueError(f"duplicate node id: {duplicate_ids[0]}")

        known = set(ids)
        for node in self.nodes:
            for dependency in node.deps:
                if dependency not in known:
                    raise ValueError(
                        f"node {node.id!r} has unknown dependency {dependency!r}"
                    )

        # Force cycle validation while preserving the caller's node ordering.
        self.topological_nodes()

    def node(self, node_id: str) -> GraphNodeSpec:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def topological_nodes(self) -> tuple[GraphNodeSpec, ...]:
        by_id = {node.id: node for node in self.nodes}
        remaining = {node.id: set(node.deps) for node in self.nodes}
        ordered: list[GraphNodeSpec] = []
        while remaining:
            ready = [
                node.id
                for node in self.nodes
                if node.id in remaining and not remaining[node.id]
            ]
            if not ready:
                cycle_nodes = ", ".join(
                    node.id for node in self.nodes if node.id in remaining
                )
                raise ValueError(f"graph contains a dependency cycle: {cycle_nodes}")
            for node_id in ready:
                ordered.append(by_id[node_id])
                remaining.pop(node_id)
                for dependencies in remaining.values():
                    dependencies.discard(node_id)
        return tuple(ordered)

    def descendants(self, node_id: str, *, include_self: bool = True) -> tuple[str, ...]:
        """Return a stable, topological set of nodes downstream of ``node_id``."""
        self.node(node_id)
        selected = {node_id}
        changed = True
        while changed:
            changed = False
            for node in self.topological_nodes():
                if node.id not in selected and any(dep in selected for dep in node.deps):
                    selected.add(node.id)
                    changed = True
        return tuple(
            node.id
            for node in self.topological_nodes()
            if node.id in selected and (include_self or node.id != node_id)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "version": self.version,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GraphDefinition:
        return cls(
            graph_id=str(raw["graph_id"]),
            version=str(raw.get("version", "1")),
            nodes=tuple(
                GraphNodeSpec.from_dict(node)
                for node in raw.get("nodes", ())
                if isinstance(node, Mapping)
            ),
        )


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    """The routing decision that contributes to reproducibility and caching."""

    backend: str = ""
    model: str = ""
    profile: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "profile": self.profile,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> RoutingSnapshot:
        raw = raw or {}
        parameters = raw.get("parameters", {})
        return cls(
            backend=str(raw.get("backend", "")),
            model=str(raw.get("model", "")),
            profile=str(raw.get("profile", "")),
            parameters=dict(parameters) if isinstance(parameters, Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A durable pointer to an artifact produced or verified by a node."""

    artifact_id: str
    run_id: str
    node_id: str
    kind: str
    uri: str
    digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "uri": self.uri,
            "digest": self.digest,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ArtifactRef:
        metadata = raw.get("metadata", {})
        return cls(
            artifact_id=str(raw["artifact_id"]),
            run_id=str(raw.get("run_id", "")),
            node_id=str(raw.get("node_id", "")),
            kind=str(raw.get("kind", "")),
            uri=str(raw.get("uri", "")),
            digest=str(raw.get("digest", "")),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Structured proof and artifact references attached to a node attempt."""

    facts: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": dict(self.facts),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> EvidenceBundle:
        raw = raw or {}
        facts_value = raw.get("facts", {})
        artifacts_value = raw.get("artifacts", ())
        return cls(
            facts=dict(facts_value) if isinstance(facts_value, Mapping) else {},
            artifacts=tuple(
                ArtifactRef.from_dict(item)
                for item in artifacts_value
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True, slots=True)
class NodeResult:
    """Normalized result returned by a graph node handler."""

    output: Any = None
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)


_FINISHED_NODE_STATUSES = frozenset(
    {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.BLOCKED,
        NodeStatus.CANCELLED,
        NodeStatus.CACHED,
    }
)


@dataclass(frozen=True, slots=True)
class NodeAttempt:
    """One persisted execution attempt for one node."""

    run_id: str
    node_id: str
    attempt: int
    status: NodeStatus
    cache_key: str | None = None
    routing: RoutingSnapshot = field(default_factory=RoutingSnapshot)
    output: Any = None
    error: str | None = None
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    started_at: float = field(default_factory=time)
    finished_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", NodeStatus(self.status))
        object.__setattr__(self, "attempt", int(self.attempt))
        if self.attempt < 1:
            raise ValueError("attempt number must be at least 1")
        if self.status in _FINISHED_NODE_STATUSES and self.finished_at is None:
            object.__setattr__(self, "finished_at", time())


@dataclass(slots=True)
class GraphRun:
    """Materialized durable state for one graph execution."""

    run_id: str
    graph: GraphDefinition
    status: NodeStatus = NodeStatus.PENDING
    inputs: dict[str, Any] = field(default_factory=dict)
    routing: RoutingSnapshot = field(default_factory=RoutingSnapshot)
    node_statuses: dict[str, NodeStatus] = field(default_factory=dict)
    node_outputs: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[NodeAttempt, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    cancel_requested: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        self.status = NodeStatus(self.status)
        self.inputs = dict(self.inputs)
        if isinstance(self.routing, Mapping):
            self.routing = RoutingSnapshot.from_dict(self.routing)
        known = {node.id for node in self.graph.nodes}
        unknown = set(self.node_statuses) - known
        if unknown:
            raise ValueError(f"run contains unknown node status: {sorted(unknown)[0]}")
        self.node_statuses = {
            node.id: NodeStatus(
                self.node_statuses.get(node.id, NodeStatus.PENDING)
            )
            for node in self.graph.nodes
        }
        self.node_outputs = {
            node_id: value
            for node_id, value in self.node_outputs.items()
            if node_id in known
        }
        self.attempts = tuple(self.attempts)
        self.artifacts = tuple(self.artifacts)


_SPECIALIST_JOIN_KIND = "skyn3t.specialist_join"
_MAX_DYNAMIC_SPECIALISTS = 4


@dataclass(frozen=True, slots=True)
class DynamicSpecialistSubgraph:
    """A one-level, durable specialist fan-out/fan-in plan.

    Child count, depth, concurrency, write ownership, and inherited run routing
    are all serialized before execution. Nested subgraphs are intentionally not
    supported in this first bounded contract.
    """

    parent_node_id: str
    specialists: tuple[GraphNodeSpec, ...]
    max_concurrency: int = 2
    depth: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_node_id", str(self.parent_node_id).strip())
        object.__setattr__(self, "specialists", tuple(self.specialists))
        object.__setattr__(self, "max_concurrency", int(self.max_concurrency))
        object.__setattr__(self, "depth", int(self.depth))
        if not self.parent_node_id:
            raise ValueError("dynamic specialist subgraph needs a parent node")
        if self.depth != 1:
            raise ValueError("dynamic specialist subgraphs are limited to depth 1")
        if not 1 <= len(self.specialists) <= _MAX_DYNAMIC_SPECIALISTS:
            raise ValueError(f"dynamic specialist subgraphs allow 1 to {_MAX_DYNAMIC_SPECIALISTS} children")
        if not 1 <= self.max_concurrency <= len(self.specialists):
            raise ValueError("dynamic specialist concurrency must be within child count")
        if any(node.deps for node in self.specialists):
            raise ValueError("dynamic specialist children inherit the parent dependency")
        child_ids = [node.id for node in self.specialists]
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("dynamic specialist child ids must be unique")
        if any(node.mutates_workspace and not node.write_set for node in self.specialists):
            raise ValueError("mutating dynamic specialists need an explicit write set")
        for index, left in enumerate(self.specialists):
            for right in self.specialists[index + 1:]:
                if write_sets_overlap(left, right):
                    raise ValueError(
                        f"dynamic specialist write sets overlap: {left.id} and {right.id}"
                    )

    @property
    def subgraph_id(self) -> str:
        payload = {
            "parent": self.parent_node_id,
            "children": [node.to_dict() for node in self.specialists],
            "max_concurrency": self.max_concurrency,
            "depth": self.depth,
        }
        return "specialists-" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]

    @property
    def join_node_id(self) -> str:
        return f"{self.parent_node_id}--{self.subgraph_id}--join"

    def expand(self, graph: GraphDefinition) -> GraphDefinition:
        parent = graph.node(self.parent_node_id)
        if parent.subgraph_depth:
            raise ValueError("nested dynamic specialist subgraphs are not supported")
        children = tuple(
            replace(
                node,
                id=f"{self.parent_node_id}--{self.subgraph_id}--{node.id}",
                deps=(self.parent_node_id,),
                cacheable=False,
                concurrency_group=self.subgraph_id,
                concurrency_limit=self.max_concurrency,
                subgraph_depth=self.depth,
            )
            for node in self.specialists
        )
        additions = (*children, GraphNodeSpec(
            id=self.join_node_id,
            kind=_SPECIALIST_JOIN_KIND,
            deps=tuple(node.id for node in children),
            cacheable=False,
            subgraph_depth=self.depth,
        ))
        existing = {node.id for node in graph.nodes}
        collision = next((node.id for node in additions if node.id in existing), None)
        if collision is not None:
            raise ValueError(f"dynamic specialist node already exists: {collision}")
        return GraphDefinition(
            graph_id=graph.graph_id,
            version=f"{graph.version}+{self.subgraph_id}",
            nodes=(*graph.nodes, *additions),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "subgraph_id": self.subgraph_id,
            "parent_node_id": self.parent_node_id,
            "children": [node.to_dict() for node in self.specialists],
            "join_node_id": self.join_node_id,
            "max_concurrency": self.max_concurrency,
            "depth": self.depth,
            "routing": "inherit-parent-run",
        }


@dataclass(frozen=True, slots=True)
class GraphRerunComparison:
    comparison_id: str
    source_run_id: str
    rerun_run_id: str
    from_node_id: str
    rerun_nodes: tuple[str, ...]
    baseline_evidence: Mapping[str, Any]
    candidate_evidence: Mapping[str, Any]
    baseline_digest: str
    candidate_digest: str
    outcome: str
    promotion_status: str
    created_at: float = field(default_factory=time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "comparison_id": self.comparison_id,
            "source_run_id": self.source_run_id,
            "rerun_run_id": self.rerun_run_id,
            "from_node_id": self.from_node_id,
            "rerun_nodes": list(self.rerun_nodes),
            "baseline_evidence": dict(self.baseline_evidence),
            "candidate_evidence": dict(self.candidate_evidence),
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "outcome": self.outcome,
            "promotion_status": self.promotion_status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class GraphReviewDecision:
    """An immutable human decision over one durable rerun comparison.

    The receipt repeats the comparison digests it was made against. This keeps
    a later dashboard view from silently detaching a human decision from the
    exact proof evidence that informed it. A decision is deliberately not a
    promotion mechanism: it cannot alter the comparison, graph, workspace, or
    any Cortex policy.
    """

    decision_id: str
    comparison_id: str
    source_run_id: str
    rerun_run_id: str
    decision: str
    note: str
    decided_by: str
    baseline_digest: str
    candidate_digest: str
    outcome: str
    created_at: float = field(default_factory=time)

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "comparison_id",
            "source_run_id",
            "rerun_run_id",
            "decided_by",
            "baseline_digest",
            "candidate_digest",
            "outcome",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"review decision {name} must not be empty")
            object.__setattr__(self, name, value)
        selected = str(self.decision).strip().lower()
        if selected not in {"keep", "reject"}:
            raise ValueError("review decision must be 'keep' or 'reject'")
        normalized_note = str(self.note).strip()
        if len(normalized_note) > 2_000:
            raise ValueError("review decision note must be at most 2000 characters")
        object.__setattr__(self, "decision", selected)
        object.__setattr__(self, "note", normalized_note)
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "comparison_id": self.comparison_id,
            "source_run_id": self.source_run_id,
            "rerun_run_id": self.rerun_run_id,
            "decision": self.decision,
            "note": self.note,
            "decided_by": self.decided_by,
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "promotion": "none",
        }


@dataclass(frozen=True, slots=True)
class GraphReviewBuildRequest:
    """A non-sensitive, immutable request to start a normal Studio build."""

    request_id: str
    decision_id: str
    comparison_id: str
    brief_sha256: str
    stack: str
    requested_by: str
    created_at: float = field(default_factory=time)

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "decision_id",
            "comparison_id",
            "brief_sha256",
            "requested_by",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"review build request {name} must not be empty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "stack", str(self.stack).strip())
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "comparison_id": self.comparison_id,
            "brief_sha256": self.brief_sha256,
            "stack": self.stack,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "kind": "normal_studio_build",
        }


@dataclass(frozen=True, slots=True)
class GraphReviewBuildDispatch:
    """An immutable link from a reviewed experiment to one normal build."""

    dispatch_id: str
    request_id: str
    decision_id: str
    comparison_id: str
    build_id: str
    created_at: float = field(default_factory=time)

    def __post_init__(self) -> None:
        for name in (
            "dispatch_id",
            "request_id",
            "decision_id",
            "comparison_id",
            "build_id",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"review build dispatch {name} must not be empty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "dispatch_id": self.dispatch_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "comparison_id": self.comparison_id,
            "build_id": self.build_id,
            "created_at": self.created_at,
            "kind": "normal_studio_build",
        }


@dataclass(frozen=True, slots=True)
class GraphRerunResult:
    source_run_id: str
    rerun: GraphRun
    comparison: GraphRerunComparison


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _canonicalize(value: Any) -> Any:
    """Convert supported values into a deterministic JSON-compatible tree."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonicalize(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return sorted(items, key=canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not canonically serializable")


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used by all graph fingerprints."""

    return json.dumps(
        _canonicalize(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def node_cache_key(
    *,
    graph: GraphDefinition,
    node: GraphNodeSpec,
    upstream_outputs: Mapping[str, Any],
    toolchain: Mapping[str, Any] | None = None,
    prompt: Any = None,
    routing: RoutingSnapshot | Mapping[str, Any] | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> str:
    """Fingerprint every input capable of changing a node's observable result."""

    routing_value = routing.to_dict() if isinstance(routing, RoutingSnapshot) else routing
    payload = {
        "graph": graph.to_dict(),
        "node": node.to_dict(),
        "upstream": dict(upstream_outputs),
        "toolchain": dict(toolchain or {}),
        "prompt": prompt,
        "routing": routing_value or {},
        "inputs": dict(inputs or {}),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


_UNSET = object()
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS graph_runs (
        run_id TEXT PRIMARY KEY,
        graph_json TEXT NOT NULL,
        status TEXT NOT NULL,
        inputs_json TEXT NOT NULL,
        routing_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_node_states (
        run_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        status TEXT NOT NULL,
        output_json TEXT,
        cache_key TEXT,
        error TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (run_id, node_id),
        FOREIGN KEY (run_id) REFERENCES graph_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_node_attempts (
        run_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        cache_key TEXT,
        routing_json TEXT NOT NULL,
        output_json TEXT,
        error TEXT,
        evidence_json TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        PRIMARY KEY (run_id, node_id, attempt),
        FOREIGN KEY (run_id, node_id)
            REFERENCES graph_node_states(run_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_artifacts (
        artifact_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        uri TEXT NOT NULL,
        digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES graph_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_node_cache (
        cache_key TEXT PRIMARY KEY,
        output_json TEXT,
        evidence_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_rerun_comparisons (
        comparison_id TEXT PRIMARY KEY,
        source_run_id TEXT NOT NULL,
        rerun_run_id TEXT NOT NULL UNIQUE,
        comparison_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (source_run_id) REFERENCES graph_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY (rerun_run_id) REFERENCES graph_runs(run_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_review_decisions (
        decision_id TEXT PRIMARY KEY,
        comparison_id TEXT NOT NULL UNIQUE,
        decision_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (comparison_id)
            REFERENCES graph_rerun_comparisons(comparison_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_review_build_requests (
        request_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL,
        comparison_id TEXT NOT NULL,
        request_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (decision_id)
            REFERENCES graph_review_decisions(decision_id) ON DELETE RESTRICT,
        FOREIGN KEY (comparison_id)
            REFERENCES graph_rerun_comparisons(comparison_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_review_build_dispatches (
        dispatch_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        decision_id TEXT NOT NULL UNIQUE,
        comparison_id TEXT NOT NULL,
        build_id TEXT NOT NULL UNIQUE,
        dispatch_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY (request_id)
            REFERENCES graph_review_build_requests(request_id) ON DELETE RESTRICT,
        FOREIGN KEY (decision_id)
            REFERENCES graph_review_decisions(decision_id) ON DELETE RESTRICT,
        FOREIGN KEY (comparison_id)
            REFERENCES graph_rerun_comparisons(comparison_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS graph_node_attempts_run_idx
    ON graph_node_attempts(run_id, node_id, attempt)
    """,
    """
    CREATE INDEX IF NOT EXISTS graph_artifacts_run_idx
    ON graph_artifacts(run_id, node_id)
    """,
)


def _json_dump(value: Any) -> str:
    return canonical_json(value)


def _json_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class GraphStore:
    """Small transactional SQLite store for durable graph execution state."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the complete schema in one transaction; safe to call repeatedly."""

        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        except BaseException:
            connection.rollback()
            connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            connection = self._connection
            self._connection = None
            self._initialized = False
            if connection is not None:
                await asyncio.to_thread(connection.close)

    async def _read(self, operation: Any) -> Any:
        await self.initialize()
        async with self._lock:
            connection = self._require_connection()
            return await asyncio.to_thread(operation, connection)

    async def _write(self, operation: Any) -> Any:
        await self.initialize()
        async with self._lock:
            connection = self._require_connection()

            def transact() -> Any:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise

            return await asyncio.to_thread(transact)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("graph store is closed")
        return self._connection

    async def save_run(self, run: GraphRun) -> None:
        now = time()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_runs (
                    run_id, graph_json, status, inputs_json, routing_json,
                    created_at, updated_at, cancel_requested, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    graph_json = excluded.graph_json,
                    status = excluded.status,
                    inputs_json = excluded.inputs_json,
                    routing_json = excluded.routing_json,
                    updated_at = excluded.updated_at,
                    cancel_requested = excluded.cancel_requested,
                    error = excluded.error
                """,
                (
                    run.run_id,
                    _json_dump(run.graph.to_dict()),
                    run.status.value,
                    _json_dump(run.inputs),
                    _json_dump(run.routing.to_dict()),
                    run.created_at,
                    now,
                    int(run.cancel_requested),
                    run.error,
                ),
            )
            for node in run.graph.nodes:
                connection.execute(
                    """
                    INSERT INTO graph_node_states (
                        run_id, node_id, status, output_json, cache_key, error,
                        updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
                    ON CONFLICT(run_id, node_id) DO NOTHING
                    """,
                    (
                        run.run_id,
                        node.id,
                        run.node_statuses[node.id].value,
                        (
                            _json_dump(run.node_outputs[node.id])
                            if node.id in run.node_outputs
                            else None
                        ),
                        now,
                    ),
                )

        await self._write(operation)

    async def set_run_status(
        self,
        run_id: str,
        status: NodeStatus,
        *,
        error: str | None = None,
    ) -> None:
        status = NodeStatus(status)

        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET status = ?, error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status.value, error, time(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

        await self._write(operation)

    async def request_cancel(self, run_id: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET cancel_requested = 1, updated_at = ?
                WHERE run_id = ?
                """,
                (time(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(run_id)

        await self._write(operation)

    async def cancellation_requested(self, run_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT cancel_requested FROM graph_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            return bool(row["cancel_requested"])

        return bool(await self._read(operation))

    async def set_node_status(
        self,
        run_id: str,
        node_id: str,
        status: NodeStatus,
        *,
        output: Any = _UNSET,
        cache_key: str | None = None,
        error: str | None = None,
    ) -> None:
        status = NodeStatus(status)

        def operation(connection: sqlite3.Connection) -> None:
            if output is _UNSET:
                cursor = connection.execute(
                    """
                    UPDATE graph_node_states
                    SET status = ?, cache_key = COALESCE(?, cache_key),
                        error = ?, updated_at = ?
                    WHERE run_id = ? AND node_id = ?
                    """,
                    (status.value, cache_key, error, time(), run_id, node_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE graph_node_states
                    SET status = ?, output_json = ?, cache_key = COALESCE(?, cache_key),
                        error = ?, updated_at = ?
                    WHERE run_id = ? AND node_id = ?
                    """,
                    (
                        status.value,
                        _json_dump(output),
                        cache_key,
                        error,
                        time(),
                        run_id,
                        node_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise KeyError(f"{run_id}:{node_id}")

        await self._write(operation)

    async def save_attempt(self, attempt: NodeAttempt) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_node_attempts (
                    run_id, node_id, attempt, status, cache_key, routing_json,
                    output_json, error, evidence_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node_id, attempt) DO UPDATE SET
                    status = excluded.status,
                    cache_key = excluded.cache_key,
                    routing_json = excluded.routing_json,
                    output_json = excluded.output_json,
                    error = excluded.error,
                    evidence_json = excluded.evidence_json,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at
                """,
                (
                    attempt.run_id,
                    attempt.node_id,
                    attempt.attempt,
                    attempt.status.value,
                    attempt.cache_key,
                    _json_dump(attempt.routing.to_dict()),
                    _json_dump(attempt.output) if attempt.output is not None else None,
                    attempt.error,
                    _json_dump(attempt.evidence.to_dict()),
                    attempt.started_at,
                    attempt.finished_at,
                ),
            )

        await self._write(operation)

    async def save_artifacts(self, artifacts: Sequence[ArtifactRef]) -> None:
        items = tuple(artifacts)
        if not items:
            return

        def operation(connection: sqlite3.Connection) -> None:
            connection.executemany(
                """
                INSERT INTO graph_artifacts (
                    artifact_id, run_id, node_id, kind, uri, digest, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    node_id = excluded.node_id,
                    kind = excluded.kind,
                    uri = excluded.uri,
                    digest = excluded.digest,
                    metadata_json = excluded.metadata_json
                """,
                [
                    (
                        artifact.artifact_id,
                        artifact.run_id,
                        artifact.node_id,
                        artifact.kind,
                        artifact.uri,
                        artifact.digest,
                        _json_dump(artifact.metadata),
                    )
                    for artifact in items
                ],
            )

        await self._write(operation)

    async def commit_node_success(
        self,
        attempt: NodeAttempt,
        *,
        cacheable: bool,
    ) -> None:
        """Atomically publish a successful attempt, evidence, state, and cache."""

        if attempt.status is not NodeStatus.SUCCEEDED:
            raise ValueError("commit_node_success requires a succeeded attempt")
        if cacheable and not attempt.cache_key:
            raise ValueError("a cacheable success requires a cache key")
        artifacts = tuple(attempt.evidence.artifacts)
        if any(
            artifact.run_id != attempt.run_id
            or artifact.node_id != attempt.node_id
            for artifact in artifacts
        ):
            raise ValueError("success artifacts must belong to the attempt node")

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_node_attempts (
                    run_id, node_id, attempt, status, cache_key, routing_json,
                    output_json, error, evidence_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node_id, attempt) DO UPDATE SET
                    status = excluded.status,
                    cache_key = excluded.cache_key,
                    routing_json = excluded.routing_json,
                    output_json = excluded.output_json,
                    error = excluded.error,
                    evidence_json = excluded.evidence_json,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at
                """,
                (
                    attempt.run_id,
                    attempt.node_id,
                    attempt.attempt,
                    attempt.status.value,
                    attempt.cache_key,
                    _json_dump(attempt.routing.to_dict()),
                    (
                        _json_dump(attempt.output)
                        if attempt.output is not None
                        else None
                    ),
                    attempt.error,
                    _json_dump(attempt.evidence.to_dict()),
                    attempt.started_at,
                    attempt.finished_at,
                ),
            )
            if artifacts:
                connection.executemany(
                    """
                    INSERT INTO graph_artifacts (
                        artifact_id, run_id, node_id, kind, uri, digest,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        run_id = excluded.run_id,
                        node_id = excluded.node_id,
                        kind = excluded.kind,
                        uri = excluded.uri,
                        digest = excluded.digest,
                        metadata_json = excluded.metadata_json
                    """,
                    [
                        (
                            artifact.artifact_id,
                            artifact.run_id,
                            artifact.node_id,
                            artifact.kind,
                            artifact.uri,
                            artifact.digest,
                            _json_dump(artifact.metadata),
                        )
                        for artifact in artifacts
                    ],
                )
            cursor = connection.execute(
                """
                UPDATE graph_node_states
                SET status = ?, output_json = ?, cache_key = ?,
                    error = NULL, updated_at = ?
                WHERE run_id = ? AND node_id = ?
                """,
                (
                    NodeStatus.SUCCEEDED.value,
                    _json_dump(attempt.output),
                    attempt.cache_key,
                    time(),
                    attempt.run_id,
                    attempt.node_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"{attempt.run_id}:{attempt.node_id}")
            if cacheable:
                connection.execute(
                    """
                    INSERT INTO graph_node_cache (
                        cache_key, output_json, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        output_json = excluded.output_json,
                        evidence_json = excluded.evidence_json,
                        created_at = excluded.created_at
                    """,
                    (
                        attempt.cache_key,
                        (
                            _json_dump(attempt.output)
                            if attempt.output is not None
                            else None
                        ),
                        _json_dump(attempt.evidence.to_dict()),
                        time(),
                    ),
                )

        await self._write(operation)

    async def load_run(self, run_id: str) -> GraphRun | None:
        def operation(connection: sqlite3.Connection) -> GraphRun | None:
            run_row = connection.execute(
                "SELECT * FROM graph_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            graph = GraphDefinition.from_dict(_json_load(run_row["graph_json"], {}))
            node_rows = connection.execute(
                """
                SELECT node_id, status, output_json
                FROM graph_node_states
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
            attempt_rows = connection.execute(
                """
                SELECT *
                FROM graph_node_attempts
                WHERE run_id = ?
                ORDER BY node_id, attempt
                """,
                (run_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT *
                FROM graph_artifacts
                WHERE run_id = ?
                ORDER BY artifact_id
                """,
                (run_id,),
            ).fetchall()
            node_statuses = {
                str(row["node_id"]): NodeStatus(str(row["status"]))
                for row in node_rows
            }
            node_outputs = {
                str(row["node_id"]): _json_load(row["output_json"])
                for row in node_rows
                if row["output_json"] is not None
            }
            attempts = tuple(
                NodeAttempt(
                    run_id=str(row["run_id"]),
                    node_id=str(row["node_id"]),
                    attempt=int(row["attempt"]),
                    status=NodeStatus(str(row["status"])),
                    cache_key=_optional_text(row["cache_key"]),
                    routing=RoutingSnapshot.from_dict(
                        _json_load(row["routing_json"], {})
                    ),
                    output=_json_load(row["output_json"]),
                    error=_optional_text(row["error"]),
                    evidence=EvidenceBundle.from_dict(
                        _json_load(row["evidence_json"], {})
                    ),
                    started_at=float(row["started_at"]),
                    finished_at=(
                        float(row["finished_at"])
                        if row["finished_at"] is not None
                        else None
                    ),
                )
                for row in attempt_rows
            )
            artifacts = tuple(
                ArtifactRef(
                    artifact_id=str(row["artifact_id"]),
                    run_id=str(row["run_id"]),
                    node_id=str(row["node_id"]),
                    kind=str(row["kind"]),
                    uri=str(row["uri"]),
                    digest=str(row["digest"]),
                    metadata=_json_load(row["metadata_json"], {}),
                )
                for row in artifact_rows
            )
            return GraphRun(
                run_id=str(run_row["run_id"]),
                graph=graph,
                status=NodeStatus(str(run_row["status"])),
                inputs=_json_load(run_row["inputs_json"], {}),
                routing=RoutingSnapshot.from_dict(
                    _json_load(run_row["routing_json"], {})
                ),
                node_statuses=node_statuses,
                node_outputs=node_outputs,
                attempts=attempts,
                artifacts=artifacts,
                created_at=float(run_row["created_at"]),
                updated_at=float(run_row["updated_at"]),
                cancel_requested=bool(run_row["cancel_requested"]),
                error=_optional_text(run_row["error"]),
            )

        return await self._read(operation)

    async def recover_run(self, run_id: str) -> GraphRun | None:
        now = time()

        def operation(connection: sqlite3.Connection) -> bool:
            run_row = connection.execute(
                "SELECT status FROM graph_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                return False
            connection.execute(
                """
                UPDATE graph_node_states
                SET status = ?, error = NULL, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    NodeStatus.READY.value,
                    now,
                    run_id,
                    NodeStatus.RUNNING.value,
                ),
            )
            connection.execute(
                """
                UPDATE graph_node_attempts
                SET status = ?, error = ?, finished_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    NodeStatus.FAILED.value,
                    "interrupted before completion",
                    now,
                    run_id,
                    NodeStatus.RUNNING.value,
                ),
            )
            if str(run_row["status"]) == NodeStatus.RUNNING.value:
                connection.execute(
                    """
                    UPDATE graph_runs
                    SET status = ?, updated_at = ?, cancel_requested = 0, error = NULL
                    WHERE run_id = ?
                    """,
                    (NodeStatus.READY.value, now, run_id),
                )
            return True

        exists = await self._write(operation)
        if not exists:
            return None
        return await self.load_run(run_id)

    async def put_cached_result(self, cache_key: str, result: NodeResult) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_node_cache (
                    cache_key, output_json, evidence_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    output_json = excluded.output_json,
                    evidence_json = excluded.evidence_json,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    _json_dump(result.output) if result.output is not None else None,
                    _json_dump(result.evidence.to_dict()),
                    time(),
                ),
            )

        await self._write(operation)

    async def get_cached_result(self, cache_key: str) -> NodeResult | None:
        def operation(connection: sqlite3.Connection) -> NodeResult | None:
            row = connection.execute(
                """
                SELECT output_json, evidence_json
                FROM graph_node_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            return NodeResult(
                output=_json_load(row["output_json"]),
                evidence=EvidenceBundle.from_dict(
                    _json_load(row["evidence_json"], {})
                ),
            )

        return await self._read(operation)

    async def list_runs(self, *, limit: int = 25) -> tuple[GraphRun, ...]:
        """Return recent durable runs without exposing raw database state."""
        selected_limit = max(1, min(int(limit), 100))

        def operation(connection: sqlite3.Connection) -> tuple[str, ...]:
            rows = connection.execute(
                "SELECT run_id FROM graph_runs ORDER BY updated_at DESC LIMIT ?",
                (selected_limit,),
            ).fetchall()
            return tuple(str(row["run_id"]) for row in rows)

        run_ids = await self._read(operation)
        runs: list[GraphRun] = []
        for run_id in run_ids:
            run = await self.load_run(run_id)
            if run is not None:
                runs.append(run)
        return tuple(runs)

    async def save_rerun_comparison(self, comparison: GraphRerunComparison) -> None:
        """Persist a comparison once; never overwrite immutable evidence."""
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_rerun_comparisons (
                    comparison_id, source_run_id, rerun_run_id, comparison_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    comparison.comparison_id,
                    comparison.source_run_id,
                    comparison.rerun_run_id,
                    _json_dump(comparison.to_dict()),
                    comparison.created_at,
                ),
            )

        await self._write(operation)

    async def load_rerun_comparison(self, rerun_run_id: str) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT comparison_json FROM graph_rerun_comparisons WHERE rerun_run_id = ?",
                (rerun_run_id,),
            ).fetchone()
            return None if row is None else dict(_json_load(row["comparison_json"], {}))

        return await self._read(operation)


    async def load_rerun_comparison_by_id(
        self,
        comparison_id: str,
    ) -> dict[str, Any] | None:
        """Load one immutable comparison by its public evidence identifier."""

        selected_id = str(comparison_id).strip()
        if not selected_id:
            return None

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT comparison_json FROM graph_rerun_comparisons
                WHERE comparison_id = ?
                """,
                (selected_id,),
            ).fetchone()
            return None if row is None else dict(_json_load(row["comparison_json"], {}))

        return await self._read(operation)

    async def save_review_decision(self, decision: GraphReviewDecision) -> None:
        """Append one human decision; a comparison can never be re-decided."""

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_review_decisions (
                    decision_id, comparison_id, decision_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.comparison_id,
                    _json_dump(decision.to_dict()),
                    decision.created_at,
                ),
            )

        try:
            await self._write(operation)
        except sqlite3.IntegrityError as exc:
            if "graph_review_decisions.comparison_id" in str(exc):
                raise ValueError("a review decision is already recorded for this comparison") from None
            raise

    async def load_review_decision(
        self,
        comparison_id: str,
    ) -> dict[str, Any] | None:
        """Load the immutable human decision for a comparison, if any."""

        selected_id = str(comparison_id).strip()
        if not selected_id:
            return None

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT decision_json FROM graph_review_decisions
                WHERE comparison_id = ?
                """,
                (selected_id,),
            ).fetchone()
            return None if row is None else dict(_json_load(row["decision_json"], {}))

        return await self._read(operation)

    async def save_review_build_request(self, request: GraphReviewBuildRequest) -> None:
        """Append a request receipt without retaining the private build brief."""

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_review_build_requests (
                    request_id, decision_id, comparison_id, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.decision_id,
                    request.comparison_id,
                    _json_dump(request.to_dict()),
                    request.created_at,
                ),
            )

        await self._write(operation)

    async def save_review_build_dispatch(self, dispatch: GraphReviewBuildDispatch) -> None:
        """Append the immutable normal-build link for a reviewed experiment."""

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO graph_review_build_dispatches (
                    dispatch_id, request_id, decision_id, comparison_id,
                    build_id, dispatch_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dispatch.dispatch_id,
                    dispatch.request_id,
                    dispatch.decision_id,
                    dispatch.comparison_id,
                    dispatch.build_id,
                    _json_dump(dispatch.to_dict()),
                    dispatch.created_at,
                ),
            )

        try:
            await self._write(operation)
        except sqlite3.IntegrityError as exc:
            if "graph_review_build_dispatches.decision_id" in str(exc):
                raise ValueError("a follow-up build is already queued for this decision") from None
            raise

    async def load_review_build_dispatch(
        self,
        comparison_id: str,
    ) -> dict[str, Any] | None:
        """Load an already queued normal-build link for one comparison."""

        selected_id = str(comparison_id).strip()
        if not selected_id:
            return None

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                """
                SELECT dispatch_json FROM graph_review_build_dispatches
                WHERE comparison_id = ?
                """,
                (selected_id,),
            ).fetchone()
            return None if row is None else dict(_json_load(row["dispatch_json"], {}))

        return await self._read(operation)

    async def list_review_items(self, *, limit: int = 25) -> tuple[dict[str, Any], ...]:
        """Return immutable comparisons paired with any decision/build receipts."""

        selected_limit = max(1, min(int(limit), 100))

        def operation(connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
            rows = connection.execute(
                """
                SELECT
                    comparisons.comparison_json AS comparison_json,
                    decisions.decision_json AS decision_json,
                    dispatches.dispatch_json AS dispatch_json
                FROM graph_rerun_comparisons AS comparisons
                LEFT JOIN graph_review_decisions AS decisions
                    ON decisions.comparison_id = comparisons.comparison_id
                LEFT JOIN graph_review_build_dispatches AS dispatches
                    ON dispatches.comparison_id = comparisons.comparison_id
                ORDER BY comparisons.created_at DESC
                LIMIT ?
                """,
                (selected_limit,),
            ).fetchall()
            return tuple(
                {
                    "comparison": dict(_json_load(row["comparison_json"], {})),
                    "decision": (
                        None
                        if row["decision_json"] is None
                        else dict(_json_load(row["decision_json"], {}))
                    ),
                    "build_dispatch": (
                        None
                        if row["dispatch_json"] is None
                        else dict(_json_load(row["dispatch_json"], {}))
                    ),
                }
                for row in rows
            )

        return await self._read(operation)


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Inputs exposed to a node handler without coupling it to the executor."""

    run_id: str
    node: GraphNodeSpec
    inputs: Mapping[str, Any]
    upstream_outputs: Mapping[str, Any]
    attempt: int
    routing: RoutingSnapshot
    cancel_event: asyncio.Event


NodeHandler = Callable[[NodeContext], Any | Awaitable[Any]]


def _normalized_write_set(node: GraphNodeSpec) -> tuple[tuple[str, ...], ...]:
    if not node.mutates_workspace:
        return ()
    if not node.write_set:
        # An undeclared mutation is treated as workspace-wide rather than being
        # allowed to race another writer.
        return (("*",),)
    normalized: list[tuple[str, ...]] = []
    for raw in node.write_set:
        path = PurePosixPath(raw.replace("\\", "/"))
        parts = tuple(part for part in path.parts if part not in ("", "."))
        if path.is_absolute() or ".." in parts:
            raise ValueError(
                f"node {node.id!r} write-set path must stay workspace-relative: {raw!r}"
            )
        normalized.append(parts or ("*",))
    return tuple(normalized)


def _path_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def write_sets_overlap(left: GraphNodeSpec, right: GraphNodeSpec) -> bool:
    """Return whether two mutating nodes may touch the same workspace path."""

    left_paths = _normalized_write_set(left)
    right_paths = _normalized_write_set(right)
    if not left_paths or not right_paths:
        return False
    if ("*",) in left_paths or ("*",) in right_paths:
        return True
    return any(
        _path_prefix(left_path, right_path)
        or _path_prefix(right_path, left_path)
        for left_path in left_paths
        for right_path in right_paths
    )


def _bind_evidence(
    evidence: EvidenceBundle,
    *,
    run_id: str,
    node_id: str,
) -> EvidenceBundle:
    artifacts: list[ArtifactRef] = []
    for artifact in evidence.artifacts:
        artifact_id = artifact.artifact_id
        if artifact.run_id != run_id or artifact.node_id != node_id:
            identity = f"{run_id}\0{node_id}\0{artifact_id}".encode()
            artifact_id = hashlib.sha256(identity).hexdigest()
        artifacts.append(
            replace(
                artifact,
                artifact_id=artifact_id,
                run_id=run_id,
                node_id=node_id,
            )
        )
    return EvidenceBundle(facts=dict(evidence.facts), artifacts=tuple(artifacts))


def _proof_snapshot(run: GraphRun, node_ids: Sequence[str]) -> dict[str, Any]:
    """Capture only durable proof facts/artifact digests, never mutable outputs."""
    snapshot: dict[str, Any] = {}
    for node_id in node_ids:
        attempts = [
            attempt for attempt in run.attempts
            if attempt.node_id == node_id
            and attempt.status in {NodeStatus.SUCCEEDED, NodeStatus.CACHED}
        ]
        attempt = attempts[-1] if attempts else None
        evidence = attempt.evidence if attempt is not None else EvidenceBundle()
        snapshot[node_id] = {
            "status": run.node_statuses[node_id].value,
            "attempt": attempt.attempt if attempt is not None else None,
            "facts": dict(evidence.facts),
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "uri": artifact.uri,
                    "digest": artifact.digest,
                    "metadata": dict(artifact.metadata),
                }
                for artifact in evidence.artifacts
            ],
        }
    return snapshot


def compare_rerun_evidence(
    source: GraphRun,
    rerun: GraphRun,
    *,
    from_node_id: str,
    rerun_nodes: Sequence[str],
) -> GraphRerunComparison:
    """Make a durable, evidence-only comparison; promotion always needs review."""
    nodes = tuple(str(node_id) for node_id in rerun_nodes)
    baseline = _proof_snapshot(source, nodes)
    candidate = _proof_snapshot(rerun, nodes)
    baseline_digest = hashlib.sha256(canonical_json(baseline).encode("utf-8")).hexdigest()
    candidate_digest = hashlib.sha256(canonical_json(candidate).encode("utf-8")).hexdigest()
    complete = all(
        rerun.node_statuses[node_id] in {NodeStatus.SUCCEEDED, NodeStatus.CACHED}
        for node_id in nodes
    )
    outcome = "incomplete" if not complete else (
        "equivalent" if baseline_digest == candidate_digest else "changed"
    )
    comparison_id = hashlib.sha256(
        canonical_json({
            "source": source.run_id,
            "rerun": rerun.run_id,
            "from": from_node_id,
            "nodes": nodes,
            "baseline": baseline_digest,
            "candidate": candidate_digest,
        }).encode("utf-8")
    ).hexdigest()[:24]
    return GraphRerunComparison(
        comparison_id=comparison_id,
        source_run_id=source.run_id,
        rerun_run_id=rerun.run_id,
        from_node_id=from_node_id,
        rerun_nodes=nodes,
        baseline_evidence=baseline,
        candidate_evidence=candidate,
        baseline_digest=baseline_digest,
        candidate_digest=candidate_digest,
        outcome=outcome,
        promotion_status="review_required" if complete else "not_ready",
    )


def _rerun_nodes_from_inputs(inputs: Mapping[str, Any]) -> frozenset[str]:
    raw = inputs.get("_graph_rerun")
    if not isinstance(raw, Mapping):
        return frozenset()
    node_ids = raw.get("rerun_nodes")
    if not isinstance(node_ids, Sequence) or isinstance(node_ids, (str, bytes)):
        return frozenset()
    return frozenset(str(node_id) for node_id in node_ids)


@dataclass(frozen=True, slots=True)
class _NodeCompletion:
    node_id: str
    status: NodeStatus
    output: Any = None


class GraphExecutor:
    """Concurrent, resumable executor for :class:`GraphDefinition` values."""

    def __init__(
        self,
        store: GraphStore,
        *,
        handlers: Mapping[str, NodeHandler],
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.store = store
        self.handlers = dict(handlers)
        self.max_concurrency = int(max_concurrency)
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def execute(
        self,
        graph: GraphDefinition,
        *,
        run_id: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        routing: RoutingSnapshot | Mapping[str, Any] | None = None,
        toolchain: Mapping[str, Any] | None = None,
        prompt: Any = None,
        dynamic_specialists: DynamicSpecialistSubgraph | None = None,
    ) -> GraphRun:
        """Create and execute a new durable graph run."""

        await self.store.initialize()
        selected_run_id = run_id or uuid.uuid4().hex
        if await self.store.load_run(selected_run_id) is not None:
            raise ValueError(f"graph run already exists: {selected_run_id}")
        routing_snapshot = (
            routing
            if isinstance(routing, RoutingSnapshot)
            else RoutingSnapshot.from_dict(routing)
        )
        run_inputs = dict(inputs or {})
        if dynamic_specialists is not None:
            if dynamic_specialists.max_concurrency > self.max_concurrency:
                raise ValueError("dynamic specialist concurrency exceeds executor capacity")
            graph = dynamic_specialists.expand(graph)
            run_inputs["_dynamic_specialist_subgraph"] = dynamic_specialists.to_dict()
        run = GraphRun(
            run_id=selected_run_id,
            graph=graph,
            inputs=run_inputs,
            routing=routing_snapshot,
        )
        await self.store.save_run(run)
        await self.store.set_run_status(run.run_id, NodeStatus.READY)
        return await self._drive(
            run.run_id,
            toolchain=dict(toolchain or {}),
            prompt=prompt,
        )

    async def resume(
        self,
        run_id: str,
        *,
        toolchain: Mapping[str, Any] | None = None,
        prompt: Any = None,
    ) -> GraphRun:
        """Recover interrupted attempts and continue without redoing successes."""

        run = await self.store.recover_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in {
            NodeStatus.SUCCEEDED,
            NodeStatus.CACHED,
            NodeStatus.CANCELLED,
        }:
            return run
        return await self._drive(
            run_id,
            toolchain=dict(toolchain or {}),
            prompt=prompt,
            force_nodes=_rerun_nodes_from_inputs(run.inputs),
        )

    async def rerun_descendants(
        self,
        source_run_id: str,
        from_node_id: str,
        *,
        run_id: str | None = None,
        toolchain: Mapping[str, Any] | None = None,
        prompt: Any = None,
    ) -> GraphRerunResult:
        """Fork a completed run and execute only a selected node and descendants."""

        await self.store.initialize()
        source = await self.store.load_run(source_run_id)
        if source is None:
            raise KeyError(source_run_id)
        if source.status not in {NodeStatus.SUCCEEDED, NodeStatus.CACHED}:
            raise ValueError("only completed graph runs can be selectively rerun")
        if source.node_statuses.get(from_node_id) not in {
            NodeStatus.SUCCEEDED,
            NodeStatus.CACHED,
        }:
            raise ValueError("the selected rerun node must have completed successfully")
        rerun_nodes = source.graph.descendants(from_node_id)
        selected_run_id = run_id or uuid.uuid4().hex
        if await self.store.load_run(selected_run_id) is not None:
            raise ValueError(f"graph run already exists: {selected_run_id}")
        run_inputs = dict(source.inputs)
        run_inputs["_graph_rerun"] = {
            "schema_version": 1,
            "source_run_id": source.run_id,
            "from_node_id": from_node_id,
            "rerun_nodes": list(rerun_nodes),
        }
        rerun = GraphRun(
            run_id=selected_run_id,
            graph=source.graph,
            inputs=run_inputs,
            routing=source.routing,
            node_statuses={
                node.id: (
                    NodeStatus.PENDING if node.id in rerun_nodes
                    else source.node_statuses[node.id]
                )
                for node in source.graph.nodes
            },
            node_outputs={
                node_id: output
                for node_id, output in source.node_outputs.items()
                if node_id not in rerun_nodes
            },
        )
        await self.store.save_run(rerun)
        await self.store.set_run_status(rerun.run_id, NodeStatus.READY)
        completed = await self._drive(
            rerun.run_id,
            toolchain=dict(toolchain or {}),
            prompt=prompt,
            force_nodes=frozenset(rerun_nodes),
        )
        comparison = compare_rerun_evidence(
            source,
            completed,
            from_node_id=from_node_id,
            rerun_nodes=rerun_nodes,
        )
        await self.store.save_rerun_comparison(comparison)
        return GraphRerunResult(
            source_run_id=source.run_id,
            rerun=completed,
            comparison=comparison,
        )

    async def cancel(self, run_id: str) -> None:
        """Durably request cancellation and wake a local active executor."""

        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        await self.store.request_cancel(run_id)

    async def _drive(
        self,
        run_id: str,
        *,
        toolchain: Mapping[str, Any],
        prompt: Any,
        force_nodes: frozenset[str] = frozenset(),
    ) -> GraphRun:
        run = await self.store.load_run(run_id)
        if run is None:
            raise KeyError(run_id)
        graph = run.graph
        statuses = dict(run.node_statuses)
        outputs = dict(run.node_outputs)
        attempts_by_node: dict[str, int] = {node.id: 0 for node in graph.nodes}
        for attempt in run.attempts:
            attempts_by_node[attempt.node_id] = max(
                attempts_by_node.get(attempt.node_id, 0),
                attempt.attempt,
            )

        cancel_event = self._cancel_events.setdefault(run_id, asyncio.Event())
        if run.cancel_requested:
            cancel_event.set()
        cancel_watcher = asyncio.create_task(
            self._watch_for_cancel(run_id, cancel_event),
            name=f"graph-cancel-{run_id}",
        )
        running: dict[asyncio.Task[_NodeCompletion], GraphNodeSpec] = {}
        await self.store.set_run_status(run_id, NodeStatus.RUNNING)

        async def transition(
            node: GraphNodeSpec,
            status: NodeStatus,
            *,
            output: Any = _UNSET,
            cache_key: str | None = None,
            error: str | None = None,
        ) -> None:
            await self.store.set_node_status(
                run_id,
                node.id,
                status,
                output=output,
                cache_key=cache_key,
                error=error,
            )
            statuses[node.id] = status
            if output is not _UNSET:
                outputs[node.id] = output

        try:
            while True:
                if cancel_event.is_set():
                    await self._cancel_running(running)
                    for node in graph.nodes:
                        if statuses[node.id] not in _FINISHED_NODE_STATUSES:
                            await transition(node, NodeStatus.CANCELLED)
                    await self.store.set_run_status(run_id, NodeStatus.CANCELLED)
                    break

                made_progress = False
                for node in graph.topological_nodes():
                    if statuses[node.id] not in {NodeStatus.PENDING, NodeStatus.READY}:
                        continue
                    dependency_statuses = [statuses[dep] for dep in node.deps]
                    if any(
                        status
                        in {
                            NodeStatus.FAILED,
                            NodeStatus.BLOCKED,
                            NodeStatus.CANCELLED,
                        }
                        for status in dependency_statuses
                    ):
                        await transition(
                            node,
                            NodeStatus.BLOCKED,
                            error="dependency did not complete successfully",
                        )
                        made_progress = True
                    elif statuses[node.id] is NodeStatus.PENDING and all(
                        status in {NodeStatus.SUCCEEDED, NodeStatus.CACHED}
                        for status in dependency_statuses
                    ):
                        await transition(node, NodeStatus.READY)
                        made_progress = True

                launched = False
                active_specs = list(running.values())
                for node in graph.topological_nodes():
                    if len(running) >= self.max_concurrency:
                        break
                    if statuses[node.id] is not NodeStatus.READY:
                        continue
                    if node.concurrency_group and sum(
                        active.concurrency_group == node.concurrency_group
                        for active in active_specs
                    ) >= node.concurrency_limit:
                        continue
                    if any(write_sets_overlap(node, active) for active in active_specs):
                        continue
                    upstream = {
                        dependency: outputs[dependency]
                        for dependency in node.deps
                        if dependency in outputs
                    }
                    task = asyncio.create_task(
                        self._execute_node(
                            run=run,
                            node=node,
                            upstream_outputs=upstream,
                            first_attempt=attempts_by_node.get(node.id, 0) + 1,
                            toolchain=toolchain,
                            prompt=prompt,
                            cancel_event=cancel_event,
                            force_run=node.id in force_nodes,
                        ),
                        name=f"graph-node-{run_id}-{node.id}",
                    )
                    running[task] = node
                    active_specs.append(node)
                    statuses[node.id] = NodeStatus.RUNNING
                    launched = True

                if running:
                    wait_for: set[asyncio.Task[Any]] = set(running)
                    wait_for.add(cancel_watcher)
                    done, _ = await asyncio.wait(
                        wait_for,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_watcher in done:
                        continue
                    completed_tasks = [
                        task for task in done if task in running
                    ]
                    for task in completed_tasks:
                        node = running.pop(task)
                        try:
                            completion = task.result()
                        except asyncio.CancelledError:
                            statuses[node.id] = NodeStatus.CANCELLED
                            continue
                        statuses[node.id] = completion.status
                        if completion.status in {
                            NodeStatus.SUCCEEDED,
                            NodeStatus.CACHED,
                        }:
                            outputs[node.id] = completion.output
                    continue

                if all(
                    status in _FINISHED_NODE_STATUSES for status in statuses.values()
                ):
                    break
                if not made_progress and not launched:
                    for node in graph.nodes:
                        if statuses[node.id] not in _FINISHED_NODE_STATUSES:
                            await transition(
                                node,
                                NodeStatus.BLOCKED,
                                error="scheduler could not make progress",
                            )
                    break

            if not cancel_event.is_set():
                failed_required = any(
                    node.required
                    and statuses[node.id]
                    not in {NodeStatus.SUCCEEDED, NodeStatus.CACHED}
                    for node in graph.nodes
                )
                final_status = (
                    NodeStatus.FAILED if failed_required else NodeStatus.SUCCEEDED
                )
                await self.store.set_run_status(run_id, final_status)
        except asyncio.CancelledError:
            cancel_event.set()
            await self._cancel_running(running)
            for node in graph.nodes:
                if statuses[node.id] not in _FINISHED_NODE_STATUSES:
                    await transition(node, NodeStatus.CANCELLED)
            await self.store.set_run_status(run_id, NodeStatus.CANCELLED)
            raise
        finally:
            cancel_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_watcher
            self._cancel_events.pop(run_id, None)

        completed = await self.store.load_run(run_id)
        if completed is None:
            raise RuntimeError(f"graph run disappeared during execution: {run_id}")
        return completed

    async def _watch_for_cancel(
        self,
        run_id: str,
        cancel_event: asyncio.Event,
    ) -> None:
        while not cancel_event.is_set():
            if await self.store.cancellation_requested(run_id):
                cancel_event.set()
                return
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=0.05)
            except TimeoutError:
                continue

    @staticmethod
    async def _cancel_running(
        running: Mapping[asyncio.Task[_NodeCompletion], GraphNodeSpec],
    ) -> None:
        tasks = tuple(running)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_node(
        self,
        *,
        run: GraphRun,
        node: GraphNodeSpec,
        upstream_outputs: Mapping[str, Any],
        first_attempt: int,
        toolchain: Mapping[str, Any],
        prompt: Any,
        cancel_event: asyncio.Event,
        force_run: bool = False,
    ) -> _NodeCompletion:
        cache_key = node_cache_key(
            graph=run.graph,
            node=node,
            upstream_outputs=upstream_outputs,
            toolchain=toolchain,
            prompt=prompt,
            routing=run.routing,
            inputs=run.inputs,
        )
        if node.cacheable and not force_run:
            cached = await self.store.get_cached_result(cache_key)
            if cached is not None:
                evidence = _bind_evidence(
                    cached.evidence,
                    run_id=run.run_id,
                    node_id=node.id,
                )
                attempt = NodeAttempt(
                    run_id=run.run_id,
                    node_id=node.id,
                    attempt=first_attempt,
                    status=NodeStatus.CACHED,
                    cache_key=cache_key,
                    routing=run.routing,
                    output=cached.output,
                    evidence=evidence,
                )
                await self.store.save_attempt(attempt)
                await self.store.save_artifacts(evidence.artifacts)
                await self.store.set_node_status(
                    run.run_id,
                    node.id,
                    NodeStatus.CACHED,
                    output=cached.output,
                    cache_key=cache_key,
                )
                return _NodeCompletion(
                    node_id=node.id,
                    status=NodeStatus.CACHED,
                    output=cached.output,
                )

        handler = self.handlers.get(node.kind)
        for offset in range(node.max_retries + 1):
            attempt_number = first_attempt + offset
            started_at = time()
            await self.store.set_node_status(
                run.run_id,
                node.id,
                NodeStatus.RUNNING,
                cache_key=cache_key,
            )
            await self.store.save_attempt(
                NodeAttempt(
                    run_id=run.run_id,
                    node_id=node.id,
                    attempt=attempt_number,
                    status=NodeStatus.RUNNING,
                    cache_key=cache_key,
                    routing=run.routing,
                    started_at=started_at,
                )
            )
            context = NodeContext(
                run_id=run.run_id,
                node=node,
                inputs=run.inputs,
                upstream_outputs=dict(upstream_outputs),
                attempt=attempt_number,
                routing=run.routing,
                cancel_event=cancel_event,
            )
            raw_result: Any
            try:
                if handler is None and node.kind == _SPECIALIST_JOIN_KIND:
                    raw_result = NodeResult(
                        output=dict(context.upstream_outputs),
                        evidence=EvidenceBundle(
                            facts={
                                "specialist_children": sorted(context.upstream_outputs),
                                "routing_inherited": True,
                            }
                        ),
                    )
                elif handler is None:
                    raise LookupError(f"no graph handler registered for kind {node.kind!r}")
                elif inspect.iscoroutinefunction(handler):
                    raw_result = handler(context)
                else:
                    sync_handler = cast(Callable[[NodeContext], Any], handler)
                    raw_result = await asyncio.to_thread(sync_handler, context)
                if inspect.isawaitable(raw_result):
                    raw_result = await raw_result
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                result = (
                    raw_result
                    if isinstance(raw_result, NodeResult)
                    else NodeResult(output=raw_result)
                )
                evidence = _bind_evidence(
                    result.evidence,
                    run_id=run.run_id,
                    node_id=node.id,
                )
                result = NodeResult(output=result.output, evidence=evidence)
                succeeded = NodeAttempt(
                    run_id=run.run_id,
                    node_id=node.id,
                    attempt=attempt_number,
                    status=NodeStatus.SUCCEEDED,
                    cache_key=cache_key,
                    routing=run.routing,
                    output=result.output,
                    evidence=evidence,
                    started_at=started_at,
                )
                await self.store.commit_node_success(
                    succeeded,
                    cacheable=node.cacheable,
                )
                return _NodeCompletion(
                    node_id=node.id,
                    status=NodeStatus.SUCCEEDED,
                    output=result.output,
                )
            except asyncio.CancelledError:
                cancelled = NodeAttempt(
                    run_id=run.run_id,
                    node_id=node.id,
                    attempt=attempt_number,
                    status=NodeStatus.CANCELLED,
                    cache_key=cache_key,
                    routing=run.routing,
                    error="cancelled",
                    started_at=started_at,
                )
                await self.store.save_attempt(cancelled)
                await self.store.set_node_status(
                    run.run_id,
                    node.id,
                    NodeStatus.CANCELLED,
                    cache_key=cache_key,
                    error="cancelled",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - failures are durable data
                error = f"{type(exc).__name__}: {exc}"
                failed = NodeAttempt(
                    run_id=run.run_id,
                    node_id=node.id,
                    attempt=attempt_number,
                    status=NodeStatus.FAILED,
                    cache_key=cache_key,
                    routing=run.routing,
                    error=error,
                    started_at=started_at,
                )
                await self.store.save_attempt(failed)
                await self.store.set_node_status(
                    run.run_id,
                    node.id,
                    NodeStatus.FAILED,
                    cache_key=cache_key,
                    error=error,
                )
                if offset < node.max_retries:
                    await self.store.set_node_status(
                        run.run_id,
                        node.id,
                        NodeStatus.READY,
                        cache_key=cache_key,
                    )
                    continue
                return _NodeCompletion(
                    node_id=node.id,
                    status=NodeStatus.FAILED,
                )
        raise RuntimeError("unreachable retry loop")
