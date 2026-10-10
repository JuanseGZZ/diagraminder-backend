"""MCP server (stdio) de un nodo GitHub del orquestador (doc 28 §GitHub).

Lo lanza Claude Code cuando un agente CLI tiene un `agGithub` cableado: un server
`dmgh<idNodo>` que traduce cada tool-call a `/gh/call` de ESTE backend. El token de
GitHub vive en el backend y no pasa por acá: este proceso solo tiene el token LOCAL.
La lista de tools y el permiso los decide el backend (`/gh/tools`, y otra vez en cada
`/gh/call`): acá no hay ninguna regla, solo traducción.

Env: DMGH_URL, DMGH_TOKEN (token local), DMGH_PROJECT (el orquestador), DMGH_NODE.
Se lanza re-ejecutando el backend con `--mcp-gh`. stdout es solo JSON-RPC.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = TOKEN = PROJECT = NODE = ""


def _http(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("X-DiagraMind-Token", TOKEN)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=900) as r:
            return json.loads(r.read() or b"{}"), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read() or b"{}").get("error") or f"HTTP {e.code}"
        except ValueError:
            return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"the DiagraMinder backend is not reachable: {getattr(e, 'reason', e)}"


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    global BASE, TOKEN, PROJECT, NODE
    BASE = (os.environ.get("DMGH_URL") or "").rstrip("/")
    TOKEN = os.environ.get("DMGH_TOKEN") or ""
    PROJECT = os.environ.get("DMGH_PROJECT") or ""
    NODE = os.environ.get("DMGH_NODE") or ""
    if not BASE or not TOKEN or not PROJECT or not NODE:
        print("faltan DMGH_URL / DMGH_TOKEN / DMGH_PROJECT / DMGH_NODE", file=sys.stderr)
        sys.exit(2)
    q = urllib.parse.quote
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method.startswith("notifications/"):
            continue
        if method == "initialize":
            _reply(mid, {"protocolVersion": params.get("protocolVersion") or "2024-11-05",
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "dmgh", "version": "1.0.0"}})
        elif method == "ping":
            _reply(mid, {})
        elif method == "tools/list":
            out, err = _http("GET", f"/gh/tools?projectId={q(PROJECT)}&nodeId={q(NODE)}")
            _reply(mid, {"tools": (out or {}).get("tools") or []})
        elif method == "tools/call":
            out, err = _http("POST", "/gh/call", {"projectId": PROJECT, "nodeId": NODE,
                                                  "tool": params.get("name") or "",
                                                  "args": params.get("arguments") or {}, "author": "IA"})
            if err:
                _reply(mid, {"content": [{"type": "text", "text": err}], "isError": True})
            else:
                res = out.get("result")
                text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
                _reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        elif mid is not None:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
