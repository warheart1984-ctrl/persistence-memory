"""nx-search client for unified AI memory system.

Provides Python interface to nx-search MCP server for external memory access.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any


class NxSearchClient:
    """Client for nx-search providing external memory access."""

    def __init__(self, nx_path: str = "G:/nx-search"):
        self.nx_path = nx_path

    def search(self, query: str, name_only: bool = False, limit: int = 25) -> dict[str, Any]:
        """Search nx-search index for files and content.

        Args:
            query: Search string
            name_only: If true, only match filenames/paths
            limit: Max hits per result set (default 25)

        Returns:
            Dict with content matches and filename matches
        """
        try:
            cmd = ["node", f"{self.nx_path}/bin/nx.js", "search", "--json"]
            if name_only:
                cmd.append("--name-only")
            cmd.append(query)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return {"error": f"nx-search failed: {result.stderr}", "content": [], "filenames": []}
            
            data = json.loads(result.stdout)
            if limit:
                data["content"] = data.get("content", [])[:limit]
                data["filenames"] = data.get("filenames", [])[:limit]
            
            return data
        except subprocess.TimeoutExpired:
            return {"error": "nx-search timeout", "content": [], "filenames": []}
        except Exception as e:
            return {"error": f"nx-search error: {str(e)}", "content": [], "filenames": []}

    def stats(self) -> dict[str, Any]:
        """Get nx-search index statistics.

        Returns:
            Dict with index health metrics
        """
        try:
            result = subprocess.run(
                ["node", f"{self.nx_path}/bin/nx.js", "stats", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {"error": f"nx-search stats failed: {result.stderr}"}
            
            return json.loads(result.stdout)
        except Exception as e:
            return {"error": f"nx-search stats error: {str(e)}"}

    def promote_to_memory(
        self,
        search_result: dict[str, Any],
        source_agent: str,
        session_id: str,
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        """Convert nx-search result to memory record format.

        Args:
            search_result: Single nx-search content result
            source_agent: Agent ID creating the memory
            session_id: Current session ID
            confidence: Confidence score for external evidence

        Returns:
            Dict formatted for MemoryCreate
        """
        path = search_result.get("path", "")
        snippet = search_result.get("snippet", "")
        
        return {
            "content": f"External context from {path}: {snippet}",
            "source_agent": source_agent,
            "session_id": session_id,
            "type": "external_context",
            "confidence": confidence,
            "evidence": [
                {
                    "kind": "filesystem_evidence",
                    "ref": path,
                    "note": "nx-search indexed content",
                }
            ],
            "subject": path.split("\\")[-1] if "\\" in path else path.split("/")[-1],
            "tags": ["nx-search", "external-memory", "filesystem"],
        }