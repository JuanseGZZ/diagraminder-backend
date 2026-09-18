"""MCP server (stdio) de los DIAGRAMAS (doc 37 §F18).

Esto es la tesis del refactor hecha código: **el contexto de tu proyecto, en un canvas
que vos y tu agente editan a la vez**. Claude Code (o cualquier cliente MCP) lee el
organigrama antes de tocar el repo y escribe en actividades lo que le falta; vos lo ves
cambiar en vivo en la pantalla, sin recargar.

Por qué se ve en vivo: las escrituras van por `/state/write` del backend local, que ya
tiene resuelto lo difícil —marca el mtime como visto para que el watcher no devuelva el
eco, y **rechaza con 409 si la web tocó el mismo archivo hace menos de medio segundo**
(la anti-pisada del bug 2026-07-15)— y emite el cambio por SSE. Escribir el disco a mano
desde acá se saltearía las dos cosas.

- Transporte: JSON-RPC 2.0 por stdio, un mensaje JSON por línea (MCP stdio), igual que
  `editor_mcp.py` y `permission_mcp.py`.
- Credenciales por env: DMD_URL (base del backend, ej http://127.0.0.1:8765) y
  DMD_TOKEN (el token del backend; está en token.txt, o lo imprime `--mcp-config`).
- stdout es SOLO JSON-RPC: cualquier log va a stderr o rompe el protocolo.

Se lanza re-ejecutando el propio backend con `--mcp-diagrams` (sirve igual para el
binario onefile, que no puede asumir un python3 del sistema).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = ""
TOKEN = ""

_STR = {"type": "string"}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


# Las descripciones van al MODELO → en inglés (regla dura de CLAUDE.md).
TOOLS = [
    {
        "name": "list_diagrams",
        "description": (
            "Lists every diagram the user has, grouped by folder: [{folder, name, type}]. "
            "Call this FIRST — the other tools take the diagram by name, and the name you "
            "need is the one here. `type` tells you what the diagram is for: cart (an org "
            "chart: the project's documentation), freestyle (a free canvas: associative "
            "memory), activities (an ordered to-do), object (data model / UML), "
            "orchestrator (a team of AI agents), treeQuestionary, documents."
        ),
        "inputSchema": _schema({}, []),
    },
    {
        "name": "read_diagram",
        "description": (
            "Returns the tree.json of one diagram, verbatim. This is the user's real "
            "context — what they decided, what they learned, what is still missing — so "
            "read it BEFORE you start working on their code, not after."
        ),
        "inputSchema": _schema(
            {"name": _STR, "folder": {"type": "string", "description": "folder it lives in (optional if the name is unique)"}},
            ["name"]),
    },
    {
        "name": "diagram_schema",
        "description": (
            "The EXACT schema of a diagram type, with its node kinds and fields. Call it "
            "before your first write_diagram of that type. Do NOT guess the shape from what "
            "you saw in a read: a diagram written with invented fields opens EMPTY in the "
            "app, and the user loses what you wrote."
        ),
        "inputSchema": _schema({"type": {"type": "string", "description": "cart, freestyle, activities, object, orchestrator, treeQuestionary, documents"}}, ["type"]),
    },
    {
        "name": "write_diagram",
        "description": (
            "Replaces a diagram whole with the JSON you pass, and the user sees it change "
            "LIVE on screen. Rules: (1) read_diagram first and keep everything you are not "
            "changing — this replaces, it does not merge; (2) respect the schema exactly "
            "(diagram_schema); (3) keep `type` as it was. If it answers 409, the user just "
            "edited that same diagram: read it again and reapply your change on top."
        ),
        "inputSchema": _schema(
            {"name": _STR,
             "folder": {"type": "string", "description": "folder it lives in (optional if the name is unique)"},
             "json": {"type": "string", "description": "the COMPLETE tree.json"}},
            ["name", "json"]),
    },
]


def _api(path, body=None):
    """GET o POST contra el backend local. Devuelve (json, error_str)."""
    url = f"{BASE}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}token={urllib.parse.quote(TOKEN)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}"), None
    except urllib.error.HTTPError as e:
        try:
            detalle = json.loads(e.read().decode() or "{}").get("error") or ""
        except Exception:
            detalle = ""
        if e.code == 409:
            return None, ("the user just edited this diagram from the app. Call read_diagram "
                          "again and reapply your change on top of what is there now.")
        return None, f"HTTP {e.code}{': ' + detalle if detalle else ''}"
    except Exception as e:
        return None, (f"could not reach DiagraMinder at {BASE} ({e}). Is the app running?")


def _proyectos():
    """[(carpeta, nombre, treeJson)] de todo lo que hay en disco."""
    st, err = _api("/state")
    if err:
        return None, err
    # `/state` devuelve folders como LISTA: [{name, projects:[{id,name,treeJson}]}]
    out = []
    for f in (st.get("folders") or []):
        for p in (f.get("projects") or []):
            out.append((f.get("name"), p.get("name"), p.get("treeJson") or ""))
    return out, None


def _buscar(nombre, carpeta):
    """Ubica un diagrama por nombre (y carpeta si se dio). Error claro si hay dudas:
    un nombre ambiguo resuelto a dedo escribe en el diagrama equivocado."""
    todos, err = _proyectos()
    if err:
        return None, err
    cand = [t for t in todos if t[1] == nombre and (not carpeta or t[0] == carpeta)]
    if not cand:
        cerca = ", ".join(sorted({f"{f}/{n}" for f, n, _ in todos})[:12]) or "(none)"
        return None, f"no diagram named '{nombre}'" + (f" in folder '{carpeta}'" if carpeta else "") + f". Available: {cerca}"
    if len(cand) > 1:
        dónde = ", ".join(sorted(c[0] for c in cand))
        return None, f"'{nombre}' exists in several folders ({dónde}). Pass `folder` to say which one."
    return cand[0], None


def call_tool(name, args):
    """(texto, is_error). El texto va al modelo → en inglés."""
    if name == "list_diagrams":
        todos, err = _proyectos()
        if err:
            return err, True
        filas = []
        for folder, nombre, tj in todos:
            try:
                tipo = json.loads(tj).get("type") or "?"
            except Exception:
                tipo = "?"
            filas.append({"folder": folder, "name": nombre, "type": tipo})
        if not filas:
            return "The user has no diagrams yet.", False
        return json.dumps(filas, ensure_ascii=False), False

    if name == "diagram_schema":
        tipo = (args.get("type") or "").strip()
        try:
            import skills
        except Exception as e:
            return f"could not load the schemas: {e}", True
        clave = f"diagramind-{tipo.lower()}"
        cuerpo = skills.SKILLS.get(clave)
        if not cuerpo:
            hay = ", ".join(sorted(k.replace("diagramind-", "") for k in skills.SKILLS
                                   if k.startswith("diagramind-") and k != "diagramind-format"))
            return f"no schema for type '{tipo}'. Types with a schema: {hay}", True
        return cuerpo, False

    if name in ("read_diagram", "write_diagram"):
        nombre = (args.get("name") or "").strip()
        carpeta = (args.get("folder") or "").strip() or None
        if not nombre:
            return "`name` is required (use list_diagrams to see them).", True
        hit, err = _buscar(nombre, carpeta)
        if err:
            return err, True
        folder, nombre_real, tj = hit
        if name == "read_diagram":
            return tj or "{}", False

        crudo = args.get("json")
        if not crudo:
            return "`json` is required: the COMPLETE tree.json.", True
        try:
            obj = json.loads(crudo) if isinstance(crudo, str) else crudo
        except json.JSONDecodeError as e:
            return f"that is not valid JSON: {e}", True
        if not isinstance(obj, dict) or not obj.get("type"):
            return "the JSON must be an object with a `type` field (keep the one it already had).", True
        # el tipo NO se cambia por accidente: es la identidad del diagrama
        try:
            previo = json.loads(tj).get("type")
        except Exception:
            previo = None
        if previo and obj.get("type") != previo:
            return (f"this diagram is of type '{previo}' and your JSON says '{obj.get('type')}'. "
                    "Changing the type would break it — keep it as it was."), True
        _, err = _api("/state/write", {"folder": folder, "name": nombre_real, "treeJson": obj})
        if err:
            return err, True
        return f"OK: '{nombre_real}' updated. The user can see it on screen already.", False

    return f"unknown tool: {name}", True


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    global BASE, TOKEN
    BASE = (os.environ.get("DMD_URL") or "http://127.0.0.1:8765").rstrip("/")
    TOKEN = os.environ.get("DMD_TOKEN") or ""
    if not TOKEN:
        print("falta DMD_TOKEN (el token del backend; lo imprime `--mcp-config`)", file=sys.stderr)
        sys.exit(2)

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
            continue                                   # las notificaciones no se responden
        if method == "initialize":
            _reply(mid, {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "diagraminder", "version": "1.0.0"},
            })
        elif method == "ping":
            _reply(mid, {})
        elif method == "tools/list":
            _reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            # Una excepción acá NO puede matar el server: el cliente perdería la sesión
            # entera por una tool que falló. Se devuelve como error de la tool y sigue.
            try:
                text, is_err = call_tool(params.get("name") or "", params.get("arguments") or {})
            except Exception as e:
                text, is_err = f"the tool failed: {type(e).__name__}: {e}", True
            _reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
        elif mid is not None:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
