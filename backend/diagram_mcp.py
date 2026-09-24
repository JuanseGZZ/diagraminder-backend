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

import diagram_memory as _mem
import editor_mcp as _fs
import mcp_policy as _mp

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

# ---------------------------------------------------------------------------------
# La MEMORIA (diagram_memory.py): los mismos diagramas, recorridos de a un nodo en vez
# de tragados enteros. Solo organigramas (cart) y canvases (freestyle). No reemplazan a
# read/write_diagram: son la otra forma de trabajar con lo mismo.
_DIAGRAMA = {"name": {"type": "string", "description": "the diagram's name (list_diagrams); a cart or a freestyle"},
             "folder": {"type": "string", "description": "folder it lives in (optional if the name is unique)"}}
_ID = {"type": "integer"}


def _props(**extra):
    return {**_DIAGRAMA, **extra}


_COMO_MEMORIA = (
    " Organigrams (cart) and canvases (freestyle) are the user's MEMORY: use them as one. "
    "Traverse, don't swallow: memory_overview to get the map, memory_search to jump to "
    "what matters, memory_read on those few nodes and their neighbours. Only fall back to "
    "read_diagram when you really need the whole file.")

MEMORY_TOOLS = [
    {
        "name": "memory_overview",
        "description": (
            "The MAP of an organigram or a canvas, cheap: every node as `[id] title`, with no "
            "coordinates, colours or HTML. An organigram comes as an indented outline; a "
            "canvas comes with its most connected nodes (the hubs) first, and its groups. "
            "Start here." + _COMO_MEMORIA),
        "inputSchema": _schema(_props(max_nodes={"type": "integer", "description": "cap (default 300)"}), ["name"]),
    },
    {
        "name": "memory_search",
        "description": (
            "Finds the nodes of an organigram or a canvas whose title or text mention your "
            "words (accent- and case-insensitive), best first, each with a snippet. Use it "
            "before reading: it takes you straight to the cluster that matters."),
        "inputSchema": _schema(_props(query=_STR, limit={"type": "integer", "description": "default 15"}),
                               ["name", "query"]),
    },
    {
        "name": "memory_read",
        "description": (
            "Reads some nodes as plain text, with their neighbours: parent, children and path "
            "in an organigram; arrows in and out (with their labels) and the group in a canvas. "
            "`depth` also reads the neighbours' text, 1 to 3 hops out (default 0: only the ids "
            "you pass, neighbours by title). This is how you read the memory — a few nodes at "
            "a time, never the whole thing."),
        "inputSchema": _schema(_props(ids={"type": "array", "items": _ID, "description": "node ids (from memory_overview or memory_search)"},
                                      depth={"type": "integer", "description": "0-3, default 0"}),
                               ["name", "ids"]),
    },
    {
        "name": "memory_add",
        "description": (
            "Adds ONE node next to another, and the user sees it appear on screen. `relation` "
            "to the `anchor`: child (default), sibling (same parent in an organigram; pointed "
            "from the same nodes on a canvas) or parent. On a canvas it lands in a free spot "
            "near the anchor, inside the anchor's group, and anchoring on a group puts it "
            "inside that group. `content` is plain text or light markdown (# heading, - "
            "bullets, **bold**, `code`, [text](url)); it is converted for you. Keep a node to "
            "ONE idea: if it grows, add a second node and link it. Without `anchor` it goes "
            "under the root (organigram) or on its own (canvas)."),
        "inputSchema": _schema(_props(title=_STR,
                                      content={"type": "string", "description": "the text: decisions, state, gotchas — what is worth remembering"},
                                      anchor={"type": "integer", "description": "id of the node it relates to"},
                                      relation={"type": "string", "enum": ["child", "sibling", "parent"]},
                                      label={"type": "string", "description": "canvas only: the arrow's label, e.g. 'depends on'"},
                                      color={"type": "string", "description": "optional hex, e.g. #e53935"}),
                               ["name", "title"]),
    },
    {
        "name": "memory_update",
        "description": (
            "Edits ONE node: `title`, `content` (replaces the text), `append` (adds to the "
            "end — the way to keep a memory up to date without losing what was there), "
            "`color`, or, in an organigram, `move_under` another card. When you finish a task "
            "and learned something, append it to the node it belongs to: a memory nobody "
            "updates turns into noise."),
        "inputSchema": _schema(_props(id=_ID, title=_STR, content=_STR, append=_STR,
                                      color={"type": "string", "description": "hex, or \"\" to clear it"},
                                      move_under={"type": "integer", "description": "organigram only: the new parent's id"}),
                               ["name", "id"]),
    },
    {
        "name": "memory_link",
        "description": (
            "Canvas only: draws an arrow `from` → `to`, with an optional `label` (what the "
            "relation is: 'uses', 'caused by', 'replaces'). Linking is what makes the memory "
            "associative — relate new knowledge to what it touches."),
        "inputSchema": _schema(_props(**{"from": _ID, "to": _ID, "label": _STR}), ["name", "from", "to"]),
    },
    {
        "name": "memory_unlink",
        "description": "Canvas only: removes the arrow `from` → `to`.",
        "inputSchema": _schema(_props(**{"from": _ID, "to": _ID}), ["name", "from", "to"]),
    },
    {
        "name": "memory_delete",
        "description": (
            "Deletes ONE node (on a canvas, with its arrows; a deleted group frees its "
            "members). In an organigram a card with children needs `with_children: true`, "
            "which deletes the whole branch. Prefer memory_update to fix a node: delete only "
            "what is wrong or obsolete."),
        "inputSchema": _schema(_props(id=_ID, with_children={"type": "boolean"}), ["name", "id"]),
    },
]
TOOLS = TOOLS + MEMORY_TOOLS
_NOMBRES_MEMORIA = {t["name"] for t in MEMORY_TOOLS}


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
        return _escribir(folder, nombre_real, obj, f"OK: '{nombre_real}' updated.")

    if name in _NOMBRES_MEMORIA:
        return _memoria(name, args)

    return f"unknown tool: {name}", True


def _escribir(folder, nombre_real, obj, hecho):
    """Escribe un diagrama por /state/write. Lo comparten write_diagram y las memory_*:
    un solo camino de escritura, con las dos guardas que costó aprender."""
    # `origin` importa: sin él el backend marca el mtime como ya visto —la
    # supresión de eco pensada para la web— y el cambio NO se emite por SSE.
    # El agente veía un 200, el usuario no veía nada, y el siguiente sync de la
    # web le pasaba por encima. Decir quién escribe es lo que hace que el
    # canvas se mueva solo (bitácora 2026-09-19).
    res, err = _api("/state/write", {"folder": folder, "name": nombre_real,
                                     "treeJson": obj, "origin": "mcp"})
    if err:
        return err, True
    # Y no se promete lo que no se verificó: si el backend NO lo emitió, la web
    # no se enteró, y decir "ya lo ve en pantalla" sería mentirle al modelo.
    if isinstance(res, dict) and not res.get("emitted"):
        return (f"'{nombre_real}' was written to disk, but the app was NOT notified, "
                "so the user may not see it yet and an app-side save could overwrite "
                "it. Tell the user to reopen the diagram."), True
    return f"{hecho} The user can see it on screen already.", False


def _memoria(name, args):
    """Las memory_*: leen el diagrama FRESCO en cada llamada (así un 409 se arregla
    repitiendo la misma llamada) y, si escriben, lo hacen por el mismo camino."""
    nombre = (args.get("name") or "").strip()
    if not nombre:
        return "`name` is required (use list_diagrams to see them).", True
    hit, err = _buscar(nombre, (args.get("folder") or "").strip() or None)
    if err:
        return err, True
    folder, nombre_real, tj = hit
    try:
        obj = json.loads(tj or "{}")
    except json.JSONDecodeError:
        return f"'{nombre_real}' is not valid JSON on disk; open it in the app first.", True
    try:
        if name == "memory_overview":
            return _mem.overview(obj, args.get("max_nodes") or 300), False
        if name == "memory_search":
            return _mem.search(obj, args.get("query") or "", args.get("limit") or 15), False
        if name == "memory_read":
            ids = args.get("ids")
            if ids is None and args.get("id") is not None:
                ids = [args.get("id")]
            return _mem.read(obj, ids if ids is not None else [], args.get("depth") or 0), False
        if name == "memory_add":
            hecho, nuevo = _mem.add(obj, args.get("title"), args.get("content") or "", args.get("anchor"),
                                    args.get("relation") or "child", args.get("label") or "", args.get("color"))
        elif name == "memory_update":
            hecho, nuevo = _mem.update(obj, args.get("id"), args.get("title"), args.get("content"),
                                       args.get("append"), args.get("color"), args.get("move_under"))
        elif name == "memory_link":
            hecho, nuevo = _mem.link(obj, args.get("from"), args.get("to"), args.get("label") or "")
        elif name == "memory_unlink":
            hecho, nuevo = _mem.unlink(obj, args.get("from"), args.get("to"))
        elif name == "memory_delete":
            hecho, nuevo = _mem.delete(obj, args.get("id"), bool(args.get("with_children")))
        else:
            return f"unknown tool: {name}", True
    except _mem.MemoryToolError as e:
        return str(e), True
    texto, es_error = _escribir(folder, nombre_real, nuevo, hecho[0].upper() + hecho[1:])
    if es_error and "just edited" in texto:
        texto = ("the user just edited this diagram from the app. Repeat the SAME call: "
                 "it reads the diagram fresh, so your change lands on top of theirs.")
    return texto, es_error


# Las descripciones de editor_mcp hablan del "editor project" y del "connector":
# los dos se borraron en F3/F8. Acá el contexto es OTRO —una carpeta que el usuario
# eligió en Ajustes— así que se reescriben las que mentirían. Es la regla de las
# skills: el modelo construye con lo que la descripción le contó que existe.
_REDESCRIBIR = {
    "fs_tree": ("Lists ONE level of the folder the user gave DiagraMinder's MCP "
                "([{name, dir, size}], dirs first, capped at 500). Empty dir = that "
                "folder's root; for subdirs pass their relative path."),
    "fs_exec": ("Runs a shell command with cwd in the folder the user gave "
                "DiagraMinder's MCP (60s timeout). Only available when the user set "
                "the MCP level to 'shell'; a 403 means they did not — don't insist."),
}


def _tools_de_archivos():
    fuera = []
    for t in _fs.TOOLS:
        t = dict(t)
        if t["name"] in _REDESCRIBIR:
            t["description"] = _REDESCRIBIR[t["name"]]
        else:
            t["description"] = t["description"].replace(
                "the editor project", "the folder the user gave DiagraMinder's MCP")
        fuera.append(t)
    return fuera


TOOLS = TOOLS + _tools_de_archivos()
_NOMBRES_FS = {t["name"] for t in _fs.TOOLS}


def _policy():
    """La política vigente, preguntada al backend. Si el backend no contesta se cae al
    nivel MÁS BAJO (solo diagramas) en vez de al más alto: ante la duda, menos permisos.
    No se cachea — apagar el interruptor tiene que valer en la llamada siguiente."""
    pol, err = _api("/mcp/policy")
    if err or not isinstance(pol, dict):
        return dict(_mp.DEFAULT)
    return _mp.normalizar(pol)


def _tools_visibles():
    permitidas = set(_mp.herramientas(_policy()))
    return [t for t in TOOLS if t["name"] in permitidas]


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def configurar(base, token):
    """Apunta este módulo (y el de archivos) a un backend. Lo llaman los DOS
    transportes: el stdio de `--mcp-diagrams` y el HTTP de `POST /mcp`."""
    global BASE, TOKEN
    BASE = (base or "http://127.0.0.1:8765").rstrip("/")
    TOKEN = token or ""
    # Las tools de archivos van por editor_mcp contra ESE backend: AUTH="local" es
    # el header X-DiagraMind-Token, y el projectId reservado es el que tiene como
    # target la carpeta que el usuario eligió (server.py → MCP_PID).
    _fs.BASE, _fs.TOKEN, _fs.AUTH, _fs.PROJECT = BASE, TOKEN, "local", "__mcp__"


def handle(msg):
    """Un mensaje JSON-RPC → su respuesta (dict), o None si no lleva respuesta.

    Está separado del transporte a propósito: el mismo despacho atiende el stdio de
    Claude Code y el POST /mcp del túnel (doc 37 §F19). Duplicarlo garantizaba que
    los dos caminos se fueran separando: el remoto terminaría sin alguna guarda.
    """
    mid = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None                                    # las notificaciones no se responden
    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "diagraminder", "version": "1.0.0"},
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        # La lista se arma con la política DEL MOMENTO, no con la del arranque: si
        # el usuario apaga el MCP mientras el cliente está conectado, la próxima
        # lista ya viene vacía. Y las tools que el nivel no habilita NO aparecen:
        # que no existan se entiende solo; que existan y sean rechazadas, no.
        return _ok(mid, {"tools": _tools_visibles()})
    if method == "tools/call":
        # Una excepción acá NO puede matar el server: el cliente perdería la sesión
        # entera por una tool que falló. Se devuelve como error de la tool y sigue.
        nombre = params.get("name") or ""
        try:
            pol = _policy()
            if not _mp.permite(pol, nombre):
                text, is_err = _mp.motivo(pol, nombre), True
            elif nombre in _NOMBRES_FS:
                text, is_err = _fs.call_tool(nombre, params.get("arguments") or {})
            else:
                text, is_err = call_tool(nombre, params.get("arguments") or {})
        except Exception as e:
            text, is_err = f"the tool failed: {type(e).__name__}: {e}", True
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    token = os.environ.get("DMD_TOKEN") or ""
    if not token:
        print("falta DMD_TOKEN (el token del backend; lo imprime `--mcp-config`)", file=sys.stderr)
        sys.exit(2)
    configurar(os.environ.get("DMD_URL"), token)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = handle(msg)
        if r is not None:
            sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
