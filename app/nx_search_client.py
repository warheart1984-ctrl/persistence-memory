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
import queue
import shutil
import subprocess
import sys
import threading
import time
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


MCP_TIMEOUT_S = 15.0


class NxSearchClient:
    """Client for nx-search providing external memory access via MCP + CLI."""

    def __init__(self, nx_path: str | None = None, require_available: bool = True, persistent: bool = False):
        """`persistent=False` (the default) closes the MCP subprocess after each search/stats call, so a client created per
        request cannot leak one; pass `persistent=True` (and close() or use `with`) to keep one connection for many calls."""
        self.nx_path = nx_path or _default_nx_path()
        self._persistent = persistent
        self._mcp_process: subprocess.Popen | None = None
        self._mcp_lines: queue.Queue[str | None] | None = None
        self._reader: threading.Thread | None = None
        self._mcp_lock = threading.RLock()
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

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _ensure_mcp(self) -> None:
        """Start the MCP stdio subprocess if it is not running, and complete the handshake."""
        with self._mcp_lock:
            if not self._available:
                raise RuntimeError("nx-search not available")
            if self._mcp_process is not None and self._mcp_process.poll() is None:
                return
            self._stop_mcp()
            # This client is the authorised caller (the API already enforced JARVIS_NX_ENABLED), so the bridge is switched
            # on for the child only. stderr is discarded: an undrained pipe fills up and stalls the child.
            env = {**os.environ, "JARVIS_NX_ENABLED": "1"}
            proc = subprocess.Popen(
                [self._node, self._get_nx_bin(), "mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
            lines: queue.Queue[str | None] = queue.Queue()

            def pump(stream=proc.stdout, out=lines) -> None:
                try:
                    for line in stream:
                        out.put(line)
                finally:
                    out.put(None)  # the child closed its stdout

            reader = threading.Thread(target=pump, name="nx-mcp-reader", daemon=True)
            reader.start()
            self._mcp_process, self._mcp_lines, self._reader = proc, lines, reader
            try:
                init = self._mcp_roundtrip({
                    "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "jarvis-ledger", "version": "0"}},
                })
                if init is None or "error" in init:
                    detail = (init or {}).get("error", {}).get("message", "no answer") if init else "no answer"
                    raise RuntimeError(f"nx-search MCP refused to initialize: {detail}")
                # A notification has no id and gets NO response: send it, do not wait for one.
                self._mcp_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except Exception:
                self._stop_mcp()
                raise

    def _mcp_send(self, message: dict[str, Any]) -> None:
        proc = self._mcp_process
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise RuntimeError("nx-search MCP connection is not running")
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def _mcp_roundtrip(self, message: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
        """Send a request (it must carry an id) and return the response with that id; None if the child exited."""
        timeout = MCP_TIMEOUT_S if timeout is None else timeout
        self._mcp_send(message)
        lines = self._mcp_lines
        assert lines is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"nx-search MCP did not answer within {timeout:g}s")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"nx-search MCP did not answer within {timeout:g}s") from exc
            if line is None:
                return None
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(response, dict) and response.get("id") == message["id"]:
                return response

    def _mcp_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Send a JSON-RPC message over MCP stdio. A notification (no id) is only written; a request waits for its answer."""
        with self._mcp_lock:
            self._ensure_mcp()
            if "id" not in message:
                self._mcp_send(message)
                return None
            return self._mcp_roundtrip(message)

    def _mcp_call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool and return its structured result.

        Transport and protocol failures (no process, timeout, JSON-RPC error, unreadable answer) RAISE, so callers fall back
        to the CLI; only a tool's own result, including its own error object, is returned.
        """
        with self._mcp_lock:
            self._ensure_mcp()
            try:
                response = self._mcp_roundtrip({
                    "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                })
            except Exception:
                self._stop_mcp()  # a timed-out or broken connection is not reused
                raise
            if response is None:
                self._stop_mcp()
                raise RuntimeError("nx-search MCP connection lost")
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "MCP error") if isinstance(response["error"], dict) else "MCP error")
            content = response.get("result", {}).get("content", [])
            if content and content[0].get("type") == "text":
                try:
                    return json.loads(content[0]["text"])
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Invalid JSON from MCP") from exc
            raise RuntimeError("Unexpected MCP response")

    def search(self, query: str, name_only: bool = False, limit: int = 25) -> dict[str, Any]:
        """Search the nx-search index via MCP, falling back to the CLI when the MCP connection fails."""
        if not self._available:
            return {"error": "nx-search not available", "content": [], "filenames": []}
        try:
            return self._mcp_call_tool("nx_search", {"query": query, "name_only": name_only, "limit": limit})
        except Exception:
            return self._search_subprocess(query, name_only, limit)
        finally:
            self._release()

    def _search_subprocess(self, query: str, name_only: bool, limit: int) -> dict[str, Any]:
        """Fallback search via subprocess."""
        try:
            cmd = [self._node, self._get_nx_bin(), "search", "--json"]
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
        """Get nx-search index statistics via MCP (CLI fallback)."""
        if not self._available:
            return {"error": "nx-search not available"}
        try:
            return self._mcp_call_tool("nx_stats", {})
        except Exception:
            return self._stats_subprocess()
        finally:
            self._release()

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
        return self._cli_cmd("ask", question, *(["--no-stream"] if no_stream else []))

    def jarvis_chat(self, message: str) -> dict[str, Any]:
        """Send a message to interactive JARVIS chat (single turn)."""
        # This would need a persistent session; for now use ask
        return self.ask(message)

    def remember(self, key: str, value: str) -> dict[str, Any]:
        """Store a persistent preference in nx-search's local memory."""
        return self._cli_cmd("remember", key, value)

    def forget(self, key: str) -> dict[str, Any]:
        """Forget a persistent preference."""
        return self._cli_cmd("remember", "--forget", key)

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
        return self._cli_cmd("describe", *args)

    def spatialize(self, directory: str, every_nth: int = 1, max_frames: int | None = None, tag: str = "") -> dict[str, Any]:
        """Spatialize a directory of rendered frames (temporal + spatial memory)."""
        args = [directory, "--every-nth", str(every_nth)]
        if max_frames:
            args += ["--max-frames", str(max_frames)]
        if tag:
            args += ["--tag", tag]
        return self._cli_cmd("spatialize", *args)

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
        return self._cli_cmd("reindex", path)

    def prune(self, paths: list[str]) -> dict[str, Any]:
        """Prune missing files from index."""
        return self._cli_cmd("prune", *paths)

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
        # A nested list here once became one argv element and made subprocess raise inside the except below, so every call
        # "failed" without ever running nx. Misuse is a programming error and is loud.
        bad = [a for a in args if not isinstance(a, str)]
        if bad:
            raise TypeError(f"nx CLI arguments must be strings (unpack lists with *); got {type(bad[0]).__name__}")
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

    def _release(self) -> None:
        if not self._persistent:
            self.close()

    def _stop_mcp(self) -> None:
        proc, self._mcp_process, self._mcp_lines, self._reader = self._mcp_process, None, None, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    def close(self) -> None:
        """Close the MCP connection (idempotent)."""
        with self._mcp_lock:
            self._stop_mcp()

    def __enter__(self) -> "NxSearchClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()