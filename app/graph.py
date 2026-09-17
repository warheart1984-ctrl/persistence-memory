"""Bounded relationship graph over continuity-ledger memories."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
import networkx as nx
from app.models import MemoryRecord
from app.store import get_store

MAX_DEPTH, MAX_NODES, MAX_K = 20, 1000, 1000

def _build_graph(memories: list[MemoryRecord], *, min_confidence: float = 0.0,
                 max_age_days: float | None = None) -> nx.DiGraph:
    g = nx.DiGraph(); cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    active = []
    for m in memories:
        if m.confidence < min_confidence: continue
        if cutoff is not None:
            try:
                if datetime.fromisoformat(m.created_at.replace("Z", "+00:00")).timestamp() < cutoff: continue
            except ValueError: continue
        active.append(m); g.add_node(m.id, **m.model_dump())
    ids = {m.id for m in active}
    subjects: dict[str, list[str]] = {}; tags: dict[str, list[str]] = {}
    for m in active:
        if m.subject: subjects.setdefault(m.subject, []).append(m.id)
        for tag in set(m.tags): tags.setdefault(tag, []).append(m.id)
    for relation, groups in (("shares_subject", subjects), ("shares_tag", tags)):
        for key, group in groups.items():
            if len(group) > 1000: continue
            for i, left in enumerate(group):
                for right in group[i + 1:]:
                    data = {"relation": relation, "subject" if relation == "shares_subject" else "tag": key}
                    g.add_edge(left, right, **data); g.add_edge(right, left, **data)
    for m in active:
        if m.supersedes in ids: g.add_edge(m.id, m.supersedes, relation="supersedes")
    return g

def _load_graph(**filters: Any) -> nx.DiGraph:
    return _build_graph(get_store().list_memories(limit=9999), **filters)

def bfs_search(start_id: str, depth: int = 2, max_nodes: int = 50, relations: set[str] | None = None, min_confidence: float = 0.0, max_age_days: float | None = None):
    g = _load_graph(min_confidence=min_confidence, max_age_days=max_age_days); depth = max(0, min(int(depth), MAX_DEPTH)); max_nodes = max(1, min(int(max_nodes), MAX_NODES))
    if start_id not in g: return []
    out = []; seen = {start_id}; queue = [(start_id, 0)]
    while queue and len(seen) < max_nodes:
        node, distance = queue.pop(0)
        if distance >= depth: continue
        for nxt in sorted(g.successors(node)):
            if nxt in seen: continue
            rel = g[node][nxt].get("relation", "")
            if relations and rel not in relations: continue
            seen.add(nxt); out.append({"id": nxt, "distance": distance + 1, "relation": rel}); queue.append((nxt, distance + 1))
            if len(seen) >= max_nodes: break
    return out

def shortest_path(source_id: str, target_id: str, max_nodes: int = MAX_NODES, min_confidence: float = 0.0, max_age_days: float | None = None):
    g = _load_graph(min_confidence=min_confidence, max_age_days=max_age_days)
    if source_id not in g or target_id not in g: return None
    try: path = nx.shortest_path(g, source_id, target_id)
    except nx.NetworkXNoPath: return None
    return path[:max(1, min(int(max_nodes), MAX_NODES))]

def find_related(memory_id: str, k: int = 10, min_distance: int = 1, max_distance: int = 3, min_confidence: float = 0.0, max_age_days: float | None = None):
    g = _load_graph(min_confidence=min_confidence, max_age_days=max_age_days); k = max(1, min(int(k), MAX_K)); max_distance = max(0, min(int(max_distance), MAX_DEPTH))
    if memory_id not in g: return []
    lengths = nx.single_source_shortest_path_length(g, memory_id, cutoff=max_distance); out = []
    for node, distance in lengths.items():
        if node != memory_id and distance >= min_distance: out.append({"id": node, "distance": distance, "relation": g[memory_id][node].get("relation", "connected") if g.has_edge(memory_id, node) else "connected"})
    return sorted(out, key=lambda x: (x["distance"], x["id"]))[:k]

def connected_components(min_size: int = 2, min_confidence: float = 0.0, max_age_days: float | None = None):
    g = _load_graph(min_confidence=min_confidence, max_age_days=max_age_days)
    return [sorted(c) for c in nx.connected_components(g.to_undirected()) if len(c) >= min_size]

def memory_graph_stats(*, min_confidence: float = 0.0, max_age_days: float | None = None):
    g = _load_graph(min_confidence=min_confidence, max_age_days=max_age_days)
    return {"node_count": g.number_of_nodes(), "edge_count": g.number_of_edges(), "density": nx.density(g) if g.number_of_nodes() > 1 else 0.0, "isolated": len(list(nx.isolates(g))), "components": nx.number_connected_components(g.to_undirected())}
