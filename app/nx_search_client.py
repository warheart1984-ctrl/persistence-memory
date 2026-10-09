"""nx-search client for unified AI memory system.

Provides Python interface to nx-search via MCP stdio transport for low-latency search,
with subprocess fallback for CLI-only commands (ask, jarvis, remember, describe, spatialize).

Platform notes:
- Windows: nx-search at G:/nx-search (or NX_SEARCH_PATH)
- Linux/Mac: nx-search at ~/.local/share/nx-search or NX_SEARCH_PATH
- Live/production: disabled by default (JARVIS_NX_ENABLED=false)
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _default_nx_path() -> str:
    """Get platform-appropriate default nx-search path."""
    # Explicit env var wins
    env_path = os.environ.get("NX_SEARCH_PATH")
    if env_path:
        return env_path
    
    system = platform.system().lower()
    if system == "windows":
        return "G:/nx-search"
    # Linux/Mac default install location
    home = Path.home()
    return str(home / ".local" / "share" / "nx-search")


def _is_nx_available(nx_path: str) -> bool:
    """Check if nx-search binary exists at path."""
    bin_path = Path(nx_path) / "bin" / "nx.js"
    return bin_path.exists()


def _find_node() -> str | None:
    """Find node executable (node on Unix, node.exe on Windows)."""
    return shutil.which("node")


class NxSearchClient:
    """Client for nx-search providing external memory access via MCP + CLI."""

    def __init__(self, nx_path: str | None = None, require_available: bool = True):
        self.nx_path = nx_path or _default_nx_path()
        self._mcp_process: subprocess.Popen | None = None
        self._request_id = 0
        self._node = _find_node()
        
        # Check availability
        self._available = _is_nx_available(self.nx_path) and self._node is not None
        if require_available and not self._available:
            raise RuntimeError(
                f"nx-search not available at {self.nx_path} "
                f"(node: {'found' if self._node else 'not found'}). "
                f"Set NX_SEARCH_PATH or install nx-search."
            )

    @property
    def available(self) -> bool:
        """Whether nx-search is available for use."""
        return self._available

    @property
    def node_path(self) -> str | None:
        """Path to node executable."""
        return self._node

    def _get_nx_bin(self) -> str:
        """Get path to nx.js binary."""
        return str(Path(self.nx_path) / "bin" / "nx.js")

    def _ensure_mcp(self) -> None:
        """Start MCP stdio subprocess if not running."""
        if not self._available:
            raise RuntimeError("nx-search not available")
        
        if self._mcp_process is None or self._mcp_process.poll() is not None:
            self._mcp_process = subprocess.Popen(
                [self._node, self._get_nx_bin(), "mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # Initialize MCP session
            self._mcp_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self._mcp_request({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _mcp_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Send JSON-RPC request over MCP stdio."""
        if self._mcp_process is None or self._mcp_process.poll() is not None:
            self._ensure_mcp()
        
        assert self._mcp_process is not None
        assert self._mcp_process.stdin is not None
        assert self._mcp_process.stdout is not None
        
        line = json.dumps(message, separators=(",", ":")) + "\n"
        self._mcp_process.stdin.write(line)
        self._mcp_process.stdin.flush()
        
        # Read response
        response_line = self._mcp_process.stdout.readline()
        if not response_line:
            return None
        return json.loads(response_line)

    def _mcp_call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and return structured result."""
        self._request_id += 1
        response = self._mcp_request({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        if response is None:
            return {"error": "MCP connection lost", "content": [], "filenames": []}
        if "error" in response:
            return {"error": response["error"].get("message", "MCP error"), "content": [], "filenames": []}
        result = response.get("result", {})
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return {"error": "Invalid JSON from MCP", "content": [], "filenames": []}
        return {"error": "Unexpected MCP response", "content": [], "filenames": []}

    def search(self, query: str, name_only: bool = False, limit: int = 25) -> dict[str, Any]:
        """Search nx-search index via MCP (low latency, persistent connection)."""
        try:
            return self._mcp_call_tool("nx_search", {
                "query": query,
                "name_only": name_only,
                "limit": limit,
            })
        except Exception as e:
            # Fallback to subprocess on MCP failure
            return self._search_subprocess(query, name_only, limit)

    def _search_subprocess(self, query: str, name_only: bool, limit: int) -> dict[str, Any]:
        """Fallback search via subprocess."""
        try:
            cmd = ["node", self._get_nx_bin(), "search", "--json"]
            if name_only:
                cmd.append("--name-only")
            cmd.append(query)
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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
        """Get nx-search index statistics via MCP."""
        try:
            return self._mcp_call_tool("nx_stats", {})
        except Exception:
            return self._stats_subprocess()

    def _stats_subprocess(self) -> dict[str, Any]:
        """Fallback stats via subprocess."""
        if not self._available:
            return {"error": "nx-search not available"}
        try:
            result = subprocess.run(
                [self._node, self._get_nx_bin(), "stats", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return {"error": f"nx-search stats failed: {result.stderr}"}
            return json.loads(result.stdout)
        except Exception as e:
            return {"error": f"nx-search stats error: {str(e)}"}

    # --- CLI-only commands (not exposed via MCP) ---

    def ask(self, question: str, no_stream: bool = False) -> dict[str, Any]:
        """Ask JARVIS a natural-language question over indexed files."""
        return self._cli_cmd("ask", [question] + (["--no-stream"] if no_stream else []))

    def jarvis_chat(self, message: str) -> dict[str, Any]:
        """Send a message to interactive JARVIS chat (single turn)."""
        # This would need a persistent session; for now use ask
        return self.ask(message)

    def remember(self, key: str, value: str) -> dict[str, Any]:
        """Store a persistent preference in nx-search's local memory."""
        return self._cli_cmd("remember", [key, value])

    def forget(self, key: str) -> dict[str, Any]:
        """Forget a persistent preference."""
        return self._cli_cmd("remember", ["--forget", key])

    def describe(self, image_path: str, question: str | None = None, holo: bool = False, native: bool = False, save: bool = False) -> dict[str, Any]:
        """Describe an image via vision (NVIDIA + HoloRT4D)."""
        args = [image_path]
        if question:
            args += ["--q", question]
        if holo:
            args.append("--holo")
        if native:
            args.append("--native")
        if save:
            args.append("--save")
        return self._cli_cmd("describe", args)

    def spatialize(self, directory: str, every_nth: int = 1, max_frames: int | None = None, tag: str = "") -> dict[str, Any]:
        """Spatialize a directory of rendered frames (temporal + spatial memory)."""
        args = [directory, "--every-nth", str(every_nth)]
        if max_frames:
            args += ["--max-frames", str(max_frames)]
        if tag:
            args += ["--tag", tag]
        return self._cli_cmd("spatialize", args)

    def watch(self, paths: list[str], debounce_ms: int = 750, no_reconcile: bool = False) -> subprocess.Popen:
        """Start file watcher (returns process handle for background monitoring)."""
        if not self._available:
            raise RuntimeError("nx-search not available")
        args = ["watch"] + paths + ["--debounce", str(debounce_ms)]
        if no_reconcile:
            args.append("--no-reconcile")
        return subprocess.Popen(
            [self._node, self._get_nx_bin()] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def scan(self, paths: list[str] | None = None, rebuild: bool = False) -> dict[str, Any]:
        """Scan and index paths."""
        args = ["scan"]
        if rebuild:
            args.append("--rebuild")
        if paths:
            args.extend(paths)
        return self._cli_cmd(*args)

    def reindex(self, path: str) -> dict[str, Any]:
        """Incremental reindex of a single path."""
        return self._cli_cmd("reindex", [path])

    def prune(self, paths: list[str]) -> dict[str, Any]:
        """Prune missing files from index."""
        return self._cli_cmd("prune", paths)

    def serve(self, port: int | None = None) -> subprocess.Popen:
        """Start web UI server on port 7788 (or specified)."""
        if not self._available:
            raise RuntimeError("nx-search not available")
        args = ["serve"]
        if port:
            args += ["--port", str(port)]
        return subprocess.Popen(
            [self._node, self._get_nx_bin()] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _cli_cmd(self, *args: str) -> dict[str, Any]:
        """Run nx CLI command and parse JSON output if available."""
        if not self._available:
            return {"error": "nx-search not available"}
        try:
            cmd = [self._node, self._get_nx_bin()] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                return {"error": f"nx-search failed: {result.stderr}"}
            # Try to parse as JSON, fallback to text
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"output": result.stdout.strip()}
        except subprocess.TimeoutExpired:
            return {"error": "nx-search timeout"}
        except Exception as e:
            return {"error": f"nx-search error: {str(e)}"}

    def promote_to_memory(
        self,
        search_result: dict[str, Any],
        source_agent: str,
        session_id: str,
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        """Convert nx-search result to memory record format."""
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

    def close(self) -> None:
        """Close MCP connection."""
        if self._mcp_process and self._mcp_process.poll() is None:
            self._mcp_process.terminate()
            self._mcp_process.wait(timeout=5)
        self._mcp_process = None

    def __enter__(self) -> "NxSearchClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()