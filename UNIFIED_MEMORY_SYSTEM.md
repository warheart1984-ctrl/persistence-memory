# Unified AI Memory System

Integration of nx-search (external long-term memory) with Jarvis Memory Board (internal working memory) to create a complete AI memory architecture.

## Architecture

### Memory Hierarchy

1. **Long-term Memory (nx-search)**
   - External memory: filesystem, projects, historical context
   - Indexed across drives D:, F:, G: 
   - 427,805 files indexed (121 GB)
   - Fast full-text search with BM25 ranking

2. **Working Memory (Jarvis Memory Board)**
   - Internal memory: current session, structured decisions, provenance
   - Evidence-backed continuity ledger
   - Conflict detection and resolution
   - Session continuity across agents

3. **Memory Promotion**
   - AI can promote important findings from filesystem to structured memory
   - Automatic evidence linking with filesystem references
   - Confidence scoring for external evidence

## New API Endpoints

### POST /api/jarvis/memory/external-search
Search nx-search external memory and optionally promote results to working memory.

```json
{
  "query": "project infinity",
  "name_only": false,
  "limit": 25,
  "auto_promote": true,
  "source_agent": "unified-memory-system",
  "session_id": "external-search-session"
}
```

Response:
```json
{
  "external_results": {
    "content": [...],
    "filenames": [...],
    "indexedFiles": 427805
  },
  "promoted_memories": [...],
  "promotion_count": 3
}
```

### GET /api/jarvis/memory/unified
Search both working memory (Jarvis) and long-term memory (nx-search) simultaneously.

```
GET /api/jarvis/memory/unified?query=evolving+ai&limit=25&source_agent=ai-assistant&session_id=session-123
```

Response:
```json
{
  "working_memory": {
    "memories": [...],
    "selections": [...],
    "conflicts": [...]
  },
  "long_term_memory": {
    "content": [...],
    "filenames": [...]
  },
  "query": "evolving ai"
}
```

### POST /api/jarvis/memory/promote
Promote a specific nx-search result to structured working memory.

```json
{
  "path": "G:\\Project Infinity\\evolve_engine\\backends\\local_evolving_ai.py",
  "snippet": "def enforce_laws():",
  "source_agent": "ai-assistant",
  "session_id": "session-123",
  "confidence": 0.8
}
```

Response:
```json
{
  "memory": {
    "id": "mem_123",
    "content": "External context from G:\\Project Infinity\\...",
    "type": "external_context",
    "evidence": [{
      "kind": "filesystem_evidence",
      "ref": "G:\\Project Infinity\\evolve_engine\\..."
    }]
  },
  "status": "promoted"
}
```

## Memory Types

Extended MemoryType to include `"external_context"` for filesystem evidence:

```python
MemoryType = Literal[
    "decision",
    "fact", 
    "task",
    "preference",
    "architecture",
    "research",
    "external_context",  # NEW
]
```

## Evidence Link Types

Extended evidence kinds to include `"filesystem_evidence"`:

```python
{
  "kind": "filesystem_evidence",
  "ref": "G:\\path\\to\\file.py",
  "note": "nx-search indexed content"
}
```

## Usage Examples

### AI Agent Workflow

```python
# 1. Search unified memory system
response = requests.get("http://localhost:8000/api/jarvis/memory/unified", 
    params={"query": "evolving ai", "session_id": "session-123"})

# 2. Analyze results from both memory systems
working_memories = response.json()["working_memory"]["memories"]
external_results = response.json()["long_term_memory"]["content"]

# 3. Promote important external findings
for result in external_results[:3]:
    promote_data = {
        "path": result["path"],
        "snippet": result["snippet"],
        "confidence": 0.8,
        "session_id": "session-123"
    }
    requests.post("http://localhost:8000/api/jarvis/memory/promote", json=promote_data)
```

### Auto-Promotion Workflow

```python
# Search with auto-promotion enabled
search_data = {
    "query": "axiom-x",
    "auto_promote": True,
    "limit": 10,
    "session_id": "session-123"
}
response = requests.post("http://localhost:8000/api/jarvis/memory/external-search", 
    json=search_data)

# Top 5 results automatically promoted to working memory
promoted_count = response.json()["promotion_count"]
```

## Benefits

1. **Situational Awareness** - AI can access entire filesystem context via nx-search
2. **Structured Memory** - Important findings preserved with provenance in Jarvis Memory Board
3. **Memory Hierarchy** - Clear separation between working and long-term memory
4. **Evidence Linking** - Automatic linking between filesystem evidence and structured memory
5. **Conflict Detection** - Conflict detection across both memory systems
6. **Session Continuity** - Long-term context maintained across AI sessions

## Implementation Details

### NxSearchClient
- Python wrapper around nx-search CLI
- Handles subprocess execution and JSON parsing
- Error handling for timeouts and invalid paths
- Conversion of search results to memory format

### Memory Promotion Logic
- Extracts path and snippet from search results
- Creates structured memory with filesystem evidence
- Tags with `nx-search`, `external-memory`, `filesystem`
- Confidence scoring for external evidence

### Unified Search
- Parallel queries to both memory systems
- Merged results with source tagging
- Respects limits and filtering for both systems
- Maintains provenance across memory boundaries

## Testing

Run unified memory system tests:

```bash
cd G:\Project Finish\persistence-memory
python -m pytest tests/test_unified_memory.py -v
```

All tests should pass, validating:
- NxSearchClient initialization and error handling
- Promotion format conversion
- MemoryCreate with external_context type
- Windows path handling
- Empty snippet handling

## Future Enhancements

1. **Smart Promotion** - ML-based scoring for automatic promotion decisions
2. **Memory Consolidation** - Periodic consolidation of promoted memories
3. **Cross-Session Learning** - Learn promotion patterns across sessions
4. **Conflict Resolution** - Automated conflict resolution strategies
5. **Memory Analytics** - Analytics on memory access patterns and promotion effectiveness