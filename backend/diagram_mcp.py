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
# «full» = el MCP del usuario (`--mcp-diagrams`: lo pega en su cliente, lo gobierna la
# política de Ajustes). «code» = el de los agentes de la PROPIA app (`--mcp-code`: el
# chat y el orquestador): solo las code_*, y escanear solo dentro de SCAN_ROOTS (las
# carpetas que ese agente ya tiene) o refrescar un mapa que el usuario ya tiene.
PROFILE = "full"
SCAN_ROOTS = ()

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
            "orchestrator (a team of AI agents), treeQuestionary, documents, codemap (a "
            "scanned map of a JavaScript/TypeScript code base — query it with the code_* tools, never "
            "read it whole)."
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
    {
        "name": "create_diagram",
        "description": (
            "Creates a NEW diagram with the JSON you pass, and it appears in the user's app "
            "LIVE, without reloading. Call diagram_schema for its type first and build the "
            "JSON exactly to it: a diagram with invented fields opens EMPTY. `folder` must "
            "be one the user already has (list_diagrams shows them); if omitted it goes to "
            "the folder their diagrams are in, or 'Local'. The name must be new in that "
            "folder — to change an existing diagram use write_diagram."
        ),
        "inputSchema": _schema(
            {"name": _STR,
             "folder": {"type": "string", "description": "an existing folder (optional)"},
             "json": {"type": "string", "description": "the COMPLETE tree.json, with its `type`"}},
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

# ---------------------------------------------------------------------------------
# El CODE MAP como memoria del código (doc 40 §Etapa 5, codemap.py): en vez de leer el
# repo entero para saber qué rompe un cambio, el agente le pregunta al mapa. Las de
# consulta leen un diagrama (nivel «diagrams»); escanear lee CÓDIGO del disco, así que
# es del nivel «files» y queda confinado a la carpeta del MCP (lo chequea el servidor).
_MAPA = {"map": {"type": "string", "description": "the code map's name (list_diagrams, type codemap); optional when there is only one, or when `path` says which folder"},
         "map_folder": {"type": "string", "description": "the app folder the map lives in (optional if its name is unique)"}}
_SYMBOL = {"type": "string", "description": "a function: name, Class.method, path:name or path:line (path:line is never ambiguous)"}
_DEPTH = {"type": "integer", "description": "levels to follow, 1-6 (default 2)"}

_COMO_CODIGO = (
    " The code map is a MEMORY of how the code is wired: ask it instead of reading whole "
    "files. It is static analysis: it can't see dynamic calls (obj[k]()), callbacks passed "
    "to libraries or calls from HTML, and edges marked BY NAME are less certain.")

CODE_TOOLS = [
    {
        "name": "code_map_scan",
        "description": (
            "Scans a JavaScript or TypeScript (also JSX/TSX) project folder and saves its code map (files, imports, "
            "functions, who calls whom, event listeners), which also shows up in the user's "
            "app. Rescanning the same folder UPDATES its map: call it again after you edit "
            "code, so the map doesn't lie to you or to the next agent. `path` is relative to "
            "the folder the user gave DiagraMinder's MCP (default: that folder) and must be "
            "inside it. node_modules, dist, vendor, build, minified files and .d.ts are skipped."),
        "inputSchema": _schema({"path": {"type": "string", "description": "the project folder (default: the MCP folder)"},
                                "name": {"type": "string", "description": "name for a NEW map (default 'Code map: <folder>')"},
                                "map_folder": {"type": "string", "description": "app folder for a new map (optional)"}}, []),
    },
    {
        "name": "code_map_overview",
        "description": (
            "The shape of a code base in a few lines: folders, the most called functions, the "
            "most imported files, import cycles, files that did not parse — and whether the "
            "map is still FRESH (it tells you if code changed since the scan). Start here." + _COMO_CODIGO),
        "inputSchema": _schema(dict(_MAPA), []),
    },
    {
        "name": "code_find",
        "description": (
            "Finds functions, methods, classes, event listeners or files by name (substring, "
            "case-insensitive), each as path:line with its number of callers and calls. "
            "Filter with `kind` (function, method, event, class, module, file) and `file`. "
            "Use it to get the exact symbol to pass to the other code_* tools."),
        "inputSchema": _schema({**_MAPA, "query": _STR,
                                "kind": {"type": "string", "enum": ["function", "method", "event", "class", "module", "file"]},
                                "file": {"type": "string", "description": "only inside this file (path or its end)"},
                                "limit": {"type": "integer", "description": "default 40"}}, []),
    },
    {
        "name": "code_who_calls",
        "description": (
            "Who calls a function, and who calls those, up to `depth` levels — each with "
            "path:line, the line of the call and how it was resolved. An event listener has "
            "no callers (the browser fires it): it tells you where it is registered."),
        "inputSchema": _schema({**_MAPA, "symbol": _SYMBOL, "depth": _DEPTH}, ["symbol"]),
    },
    {
        "name": "code_calls",
        "description": (
            "What a function (or an event listener) calls, and what those call, up to "
            "`depth` levels — the chain of activation, with path:line. Also lists the event "
            "listeners it registers and what it uses from outside the project."),
        "inputSchema": _schema({**_MAPA, "symbol": _SYMBOL, "depth": _DEPTH}, ["symbol"]),
    },
    {
        "name": "code_impact",
        "description": (
            "Call it BEFORE you change a function or a file: what depends on it. For a "
            "function: its direct callers (with the call lines), how many indirect ones, the "
            "event listeners — user actions — that end up running it, whether it is exported. "
            "For a file: who imports it, which of its functions are used from other files, "
            "the events elsewhere that reach it, and whether it is in an import cycle. This is "
            "the list of places to check after your edit, without reading the whole repo." + _COMO_CODIGO),
        "inputSchema": _schema({**_MAPA, "target": {"type": "string", "description": "a file path, or a symbol (name, Class.method, path:name, path:line)"}},
                               ["target"]),
    },
    {
        "name": "code_quality",
        "description": (
            "Code quality findings of the map, most serious first: complex, long and "
            "many-parameter functions, functions that call too much or are used from many "
            "files, big files, files with many responsibilities or imports, import cycles — "
            "each with path:line, the measured value and the threshold, plus the files that "
            "concentrate them (hotspots). Pass `file` for one file. These are pointers to look "
            "at, not a verdict: use them to pick what to refactor, then code_impact before "
            "touching it."),
        "inputSchema": _schema({**_MAPA, "file": {"type": "string", "description": "only findings in this file"},
                                "limit": {"type": "integer", "description": "default 30"}}, []),
    },
    {
        "name": "code_diff_impact",
        "description": (
            "What a CHANGE touches: give it a diff and it maps every changed line to the "
            "function or event listener that contains it, with each one's direct callers, and "
            "lists the user actions (event listeners) that reach any changed code — the list of "
            "what to test, and of what a reviewer should look at. Pass `diff` (a unified diff, "
            "e.g. the output of `git diff`), or `base` (a git ref: the backend runs `git diff "
            "<base>` in the map's folder, uncommitted changes and new untracked files included; "
            "that reads code from disk, so it needs the MCP at level 'files'). Line numbers are "
            "the NEW side: scan after editing (code_map_scan) so the map matches the code." + _COMO_CODIGO),
        "inputSchema": _schema({**_MAPA, "diff": {"type": "string", "description": "a unified diff (git diff output)"},
                                "base": {"type": "string", "description": "a git ref to diff the map's folder against (default HEAD) — used when `diff` is not given"},
                                "limit": {"type": "integer", "description": "max changed functions listed (default 40)"}}, []),
    },
]
TOOLS = TOOLS + CODE_TOOLS
_NOMBRES_CODIGO = {t["name"] for t in CODE_TOOLS}


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
        if tipo.lower() == "codemap":
            return ("A codemap is not written by hand: it is the output of code_map_scan (the "
                    "analyzer's graph). Create or refresh one with code_map_scan and query it "
                    "with code_map_overview / code_find / code_impact / code_who_calls / code_calls."), False
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
        if _es_codemap(tj):
            # 400 KB de arrays compactos no le sirven a nadie en el contexto, y escribirlo
            # a mano rompería el grafo: el mapa se consulta y se regenera, no se edita.
            if name == "write_diagram":
                return ("'" + nombre_real + "' is a code map: it is generated, not written. "
                        "Refresh it with code_map_scan."), True
            return _codigo("code_map_overview", {"map": nombre_real, "map_folder": folder})
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

    if name == "create_diagram":
        return _crear(args)

    if name in _NOMBRES_MEMORIA:
        return _memoria(name, args)

    if name in _NOMBRES_CODIGO:
        return _codigo(name, args)

    return f"unknown tool: {name}", True


def _safe_name(nombre):
    from util import safe_name
    return safe_name(nombre)


def _es_codemap(tj):
    try:
        return '"codemap"' in (tj or "")[:200] and json.loads(tj).get("type") == "codemap"
    except Exception:
        return False


def _mapas():
    """[(carpeta, nombre, obj)] de los codemaps que hay."""
    todos, err = _proyectos()
    if err:
        return None, err
    out = []
    for f, n, tj in todos:
        if _es_codemap(tj):
            try:
                out.append((f, n, json.loads(tj)))
            except Exception:
                pass
    return out, None


def _elegir_mapa(args):
    """El mapa al que se refiere la llamada: por nombre, o el único que hay. Ambiguo →
    error con la lista (elegir a dedo responde sobre el código equivocado)."""
    mapas, err = _mapas()
    if err:
        return None, err
    if not mapas:
        return None, ("the user has no code map yet. Create one with code_map_scan (needs the "
                      "MCP at level 'files'), or ask the user to scan the project in the app's "
                      "Code map mode.")
    nombre = (args.get("map") or "").strip()
    carpeta = (args.get("map_folder") or "").strip()
    cand = [m for m in mapas if (not nombre or m[1] == nombre) and (not carpeta or m[0] == carpeta)]
    if not cand:
        return None, (f"no code map named '{nombre}'. Code maps: " +
                      ", ".join(f"{f}/{n}" for f, n, _ in mapas))
    if len(cand) > 1:
        return None, ("there are several code maps; pass `map` (and `map_folder` if needed): " +
                      "; ".join(f"{n} (folder {f}, code at {(o.get('scan') or {}).get('path') or (o.get('scan') or {}).get('root') or '?'})"
                                for f, n, o in cand))
    return cand[0], None


def _origen(mapas=()):
    """Cómo se presenta este MCP ante /codemap/*: el del usuario como «mcp» (lo
    gobierna la política), el de la app como «agent» con sus carpetas (`roots`, con
    todo lo de adentro) y las de los mapas que ya existen (`maps`, SOLO esa ruta: se
    refrescan, no abren sus subcarpetas)."""
    if PROFILE == "code":
        return {"origin": "agent", "roots": list(SCAN_ROOTS), "maps": [m for m in mapas if m]}
    return {"origin": "mcp"}


def _frescura(obj):
    """El texto de frescura del mapa: lo pregunta al SERVIDOR (que aplica la política);
    si no deja mirar la carpeta, «desconocida» — nunca «fresco» sin haberlo visto."""
    import codemap as _cm
    sc = obj.get("scan") or {}
    actual = None
    if isinstance(sc.get("stamp"), dict) and sc.get("path"):
        res, err = _api("/codemap/stamp", {"path": sc["path"], **_origen([sc["path"]])})
        if not err and isinstance(res, dict):
            actual = res.get("stamp")
    return _cm.freshness(obj, actual)[1]


def _codigo(name, args):
    import codemap as _cm
    if name == "code_map_scan":
        return _escanear(args)
    hit, err = _elegir_mapa(args)
    if err:
        return err, True
    _, _, obj = hit
    try:
        g = _cm.Graph(obj)
        fresco = _frescura(obj)
        if name == "code_map_overview":
            return _cm.overview(g, fresco), False
        if name == "code_find":
            return _cm.find(g, fresco, args.get("query") or "", args.get("kind") or "",
                            args.get("file") or "", int(args.get("limit") or 40)), False
        if name == "code_who_calls":
            return _cm.who_calls(g, fresco, args.get("symbol"), args.get("depth") or 2), False
        if name == "code_calls":
            return _cm.calls(g, fresco, args.get("symbol"), args.get("depth") or 2), False
        if name == "code_impact":
            return _cm.impact(g, fresco, args.get("target") or args.get("symbol")), False
        if name == "code_quality":
            return _cm.quality(g, fresco, args.get("file") or "", int(args.get("limit") or 30)), False
        if name == "code_diff_impact":
            diff, untracked = args.get("diff") or "", []
            if not diff.strip():
                path = (obj.get("scan") or {}).get("path")
                if not path:
                    return ("this map was scanned in the browser, so DiagraMinder doesn't know its folder: "
                            "pass the diff as `diff` (e.g. the output of `git diff`)."), True
                # el SERVIDOR corre git y decide si se puede (lee código: nivel «files»)
                res, err = _api("/codemap/diff", {"path": path, "base": args.get("base") or "HEAD", **_origen([path])})
                if err:
                    return err + " — or pass the diff yourself as `diff`.", True
                diff, untracked = res.get("diff") or "", res.get("untracked") or []
                if not diff.strip() and not untracked:
                    return f"No changes in {path} against {args.get('base') or 'HEAD'}.", False
            return _cm.diff_impact(g, fresco, diff, untracked, int(args.get("limit") or 40)), False
    except _cm.CodeMapError as e:
        return str(e), True
    return f"unknown tool: {name}", True


def _escanear(args):
    """code_map_scan: el SERVIDOR lee y analiza (y decide si se puede); acá se guarda
    el resultado como un diagrama codemap — el mismo de esa carpeta si ya existe, así
    re-escanear es «recargar» y no un mapa nuevo cada vez."""
    mapas, err = _mapas()
    if err:
        return err, True
    pedido = (args.get("path") or "").strip()
    if PROFILE == "code":
        # Un agente de la app escanea lo SUYO (SCAN_ROOTS) o refresca un mapa que el
        # usuario ya tiene: refrescar no le abre nada nuevo, crear uno de cualquier
        # carpeta sí. Una ruta relativa cuelga de su primera carpeta.
        conocidas = [(m[2].get("scan") or {}).get("path") for m in mapas]
        if not pedido and args.get("map"):
            pedido = next(((m[2].get("scan") or {}).get("path") or "" for m in mapas if m[1] == args["map"]), "")
        if not pedido:
            if len(SCAN_ROOTS) != 1:
                return ("pass `path`: the project folder to scan (absolute), or `map` to refresh an "
                        "existing code map. Code maps: " + (", ".join(f"{n} ({p})" for (_, n, _), p in zip(mapas, conocidas) if p) or "none")), True
            pedido = SCAN_ROOTS[0]
        elif not os.path.isabs(pedido) and SCAN_ROOTS:
            pedido = os.path.join(SCAN_ROOTS[0], pedido)
        res, err = _api("/codemap/scan", {"path": pedido, **_origen([p for p in conocidas if p])})
    else:
        res, err = _api("/codemap/scan", {"path": pedido, "origin": "mcp"})
    if err:
        return err, True
    texto, ruta = res.get("tree"), res.get("path")
    nombre = (args.get("name") or "").strip()
    previo = [m for m in mapas if (m[2].get("scan") or {}).get("path") == ruta]
    if nombre:
        previo = [m for m in mapas if m[1] == nombre] or []
    obj = json.loads(texto)
    pedido_mapa = (args.get("map") or "").strip()
    if pedido_mapa and not nombre:
        # el mapa que se pidió, y no otro: aunque haya uno de esta carpeta
        hit = [m for m in mapas if m[1] == pedido_mapa]
        if not hit:
            return f"no code map named '{pedido_mapa}'.", True
        suya = (hit[0][2].get("scan") or {}).get("path")
        if suya and suya != ruta:
            return (f"'{pedido_mapa}' is the map of {suya}, not of {ruta}: it was NOT overwritten. "
                    "Scan its own folder to refresh it, or pass a new `name`."), True
        if not suya and _solapamiento(hit[0][2], obj) < 0.5:
            return (f"'{pedido_mapa}' was scanned from a different folder than {ruta} (less than half of "
                    "its files match), so it was NOT overwritten. Pass a new `name` to create a separate map."), True
        previo = hit
    if not previo:
        # Un mapa escaneado en el NAVEGADOR no sabe su ruta (el selector no la da): solo
        # el nombre de la carpeta. Se adopta —se refresca ese en vez de crear otro— si
        # coincide el nombre Y al menos la mitad de sus archivos: dos proyectos que se
        # llaman `app` no son el mismo, y pisar el mapa equivocado sería peor que duplicar.
        cands = [(m, _solapamiento(m[2], obj)) for m in mapas if not (m[2].get("scan") or {}).get("path")
                 and ((m[2].get("scan") or {}).get("root") or (m[2].get("graph") or {}).get("root")) == os.path.basename(ruta)]
        buenos = sorted([c for c in cands if c[1] >= 0.5], key=lambda c: -c[1])
        if len(buenos) == 1 or (len(buenos) > 1 and buenos[0][1] > buenos[1][1]):
            previo = [buenos[0][0]]
    resumen = (f"{(obj.get('scan') or {}).get('files')} files, {len(obj['graph']['fns'])} functions/events, "
               f"{len(obj['graph']['calls'])} call edges")
    if previo:
        carpeta, nombre_real, _ = previo[0]
        res2, err = _api("/state/write", {"folder": carpeta, "name": nombre_real, "treeJson": texto, "origin": "mcp"})
        if err:
            return err, True
        aviso = "" if (isinstance(res2, dict) and res2.get("emitted")) else " (the app was not notified; it shows the new map when reopened)"
        return (f"OK: code map '{nombre_real}' refreshed from {ruta} — {resumen}{aviso}. "
                "Query it with code_map_overview / code_find / code_impact."), False
    carpeta = (args.get("map_folder") or "").strip()
    if not carpeta:
        todos, err = _proyectos()
        if err:
            return err, True
        hay = {f for f, _, _ in todos}
        carpeta = hay.pop() if len(hay) == 1 else "Local"
    # El nombre que queda es el de la CARPETA en disco (safe_name: sin «:» ni «/»).
    # Contestar con otro haría que el agente busque un mapa que no existe.
    nombre = _safe_name(nombre or f"Code map {os.path.basename(ruta) or 'project'}")
    res2, err = _api("/state/write", {"folder": carpeta, "name": nombre, "treeJson": texto,
                                      "origin": "mcp", "create": True})
    if err:
        return err, True
    aviso = "" if (isinstance(res2, dict) and res2.get("emitted")) else " (the app was not notified; it shows up when the user reconnects)"
    return (f"OK: code map '{nombre}' created in folder '{carpeta}' from {ruta} — {resumen}{aviso}. "
            "Query it with code_map_overview / code_find / code_impact; after you edit code, "
            "call code_map_scan again to refresh it."), False


def _solapamiento(viejo, nuevo):
    """Qué parte de los archivos de un mapa viejo está en el escaneo nuevo (0..1). Las
    rutas del grafo van con la carpeta adelante (`app/x.js`); las de la marca, sin ella."""
    def rels(o):
        return {p.split("/", 1)[1] if "/" in p else p for p in
                (f.get("path", "") for f in ((o.get("graph") or {}).get("files") or []))}
    a = rels(viejo)
    b = set(((nuevo.get("scan") or {}).get("stamp") or {}).keys()) or rels(nuevo)
    return len(a & b) / len(a) if a else 0.0


def _tipos_con_esquema():
    try:
        import skills
    except Exception:
        return None
    return {k.replace("diagramind-", "") for k in skills.SKILLS
            if k.startswith("diagramind-") and k != "diagramind-format"}


def _crear(args):
    """create_diagram: lo nuevo va por el MISMO /state/write, con `create`. El backend
    es el que se niega a pisar uno existente y el que lo registra para que la web lo
    adopte: acá solo se valida lo que el modelo puede corregir solo."""
    # El disco guarda el nombre saneado (safe_name: «Flow: x» queda «Flow x») y es ese
    # el que después devuelve list_diagrams: se contesta con ESE, no con el pedido.
    nombre = _safe_name((args.get("name") or "").strip()) if (args.get("name") or "").strip() else ""
    if not nombre:
        return "`name` is required: the new diagram's name.", True
    crudo = args.get("json")
    if not crudo:
        return ("`json` is required: the COMPLETE tree.json. Call diagram_schema for its "
                "type and build it to that shape."), True
    try:
        obj = json.loads(crudo) if isinstance(crudo, str) else crudo
    except json.JSONDecodeError as e:
        return f"that is not valid JSON: {e}", True
    tipos = _tipos_con_esquema() or set()
    if not isinstance(obj, dict) or obj.get("type") not in tipos:
        return (f"the JSON must be an object whose `type` is one of: {', '.join(sorted(tipos))}. "
                "Call diagram_schema for that type."), True
    carpeta = (args.get("folder") or "").strip()
    if not carpeta:
        todos, err = _proyectos()
        if err:
            return err, True
        hay = {f for f, _, _ in todos}
        carpeta = hay.pop() if len(hay) == 1 else "Local"
    res, err = _api("/state/write", {"folder": carpeta, "name": nombre, "treeJson": obj,
                                     "origin": "mcp", "create": True})
    if err:
        return err, True
    if isinstance(res, dict) and not res.get("emitted"):
        return (f"'{nombre}' was written to disk, but the app was NOT notified, so it "
                "will not show up yet. Tell the user to reconnect the app."), True
    return (f"OK: '{nombre}' ({obj['type']}) created in folder '{carpeta}'. The app was "
            "notified and adds it to the user's diagram list. From now on use "
            "read_diagram / write_diagram (or memory_*) with this name."), False


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
    if PROFILE == "code":
        return list(CODE_TOOLS)
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


def configurar(base, token, profile="full", scan_roots=()):
    """Apunta este módulo (y el de archivos) a un backend. Lo llaman los DOS
    transportes: el stdio de `--mcp-diagrams` y el HTTP de `POST /mcp` — y, con
    profile «code», el `--mcp-code` de los agentes de la app."""
    global BASE, TOKEN, PROFILE, SCAN_ROOTS
    BASE = (base or "http://127.0.0.1:8765").rstrip("/")
    TOKEN = token or ""
    PROFILE = profile if profile in ("full", "code") else "full"
    SCAN_ROOTS = tuple(os.path.realpath(r) for r in scan_roots if r)
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
            if PROFILE == "code":
                # el agente de la app: solo las code_*, sin la política del MCP del
                # usuario (lo que puede leer lo deciden sus carpetas, en el servidor)
                if nombre not in _NOMBRES_CODIGO:
                    text, is_err = f"unknown tool: {nombre}", True
                else:
                    text, is_err = call_tool(nombre, params.get("arguments") or {})
                return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
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


def main(profile="full"):
    token = os.environ.get("DMD_TOKEN") or ""
    if not token:
        print("falta DMD_TOKEN (el token del backend; lo imprime `--mcp-config`)", file=sys.stderr)
        sys.exit(2)
    roots = [r for r in (os.environ.get("DMD_SCAN_ROOTS") or "").split(os.pathsep) if r]
    configurar(os.environ.get("DMD_URL"), token, profile, roots)

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
