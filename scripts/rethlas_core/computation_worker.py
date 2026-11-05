"""Disposable MCP client process, bounded by its host parent."""
import asyncio
import json
from pathlib import Path
import sys
from .computation import _mcp
from .verification import load_json

if __name__ == "__main__":
    root, request, timeout, log, *runtime = sys.argv[1:]
    if len(runtime) > 1:
        raise ValueError("Expected at most one math runtime configuration")
    result = asyncio.run(_mcp(Path(root), load_json(Path(request)), float(timeout), Path(log),
                             runtime[0] if runtime else None))
    print(json.dumps(result, ensure_ascii=False))
