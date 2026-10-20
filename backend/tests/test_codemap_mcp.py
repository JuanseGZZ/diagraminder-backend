"""El Code map como MEMORIA del código para el agente (doc 40 §Etapa 5), contra el
backend REAL.

Levanta el server con un HOME temporal y un puerto libre, arma un proyecto JS chico en
disco y habla JSON-RPC por stdio con `--mcp-diagrams`, como Claude Code. Verifica:

- los niveles: consultar un mapa es de «diagrams»; ESCANEAR lee código, es de «files»
  y queda confinado a la carpeta del MCP — chequeado en el SERVIDOR, por la red;
- que el mapa se guarde como un diagrama codemap que la web ve, sin el fuente;
- que las consultas respondan con archivo:línea y no prometan de más;
- que el mapa diga cuándo quedó VIEJO, y que re-escanear lo refresque en el lugar.

El analizador corre con node. Viene con la WEB (dist/codemap-node.mjs, ofuscado); acá se
usa el fuente del checkout de al lado, o el que diga DMN_CODEMAP_RUNNER. Sin node o sin
analizador las secciones que escanean se saltean AVISANDO (no cuentan como verdes).

    python3 backend/tests/test_codemap_mcp.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SERVER = os.path.join(BACKEND, "server.py")
sys.path.insert(0, BACKEND)

import codemap  # noqa: E402
from cli_base import _find_bin  # noqa: E402

ok = fail = 0


def check(nombre, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {nombre}")
    else:
        fail += 1
        print(f"  ❌ {nombre}" + (f" — {extra}" if extra else ""))


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Mcp:
    def __init__(self, env, flag="--mcp-diagrams"):
        self.p = subprocess.Popen([sys.executable, SERVER, flag],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        self.n = 0

    def pedir(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n,
                                       "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        linea = self.p.stdout.readline()
        return json.loads(linea) if linea.strip() else {}

    def tool(self, nombre, args=None):
        r = self.pedir("tools/call", {"name": nombre, "arguments": args or {}})
        if "error" in r:
            return f"[rpc error] {json.dumps(r['error'])}", True
        res = r.get("result") or {}
        return (res.get("content") or [{}])[0].get("text", ""), bool(res.get("isError"))

    def lista(self):
        return sorted(t["name"] for t in (self.pedir("tools/list").get("result") or {}).get("tools", []))

    def cerrar(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


# El analizador: el del checkout (fuente), si existe. El repo del backend es público y
# no lo trae; corriendo al lado del repo de la web, está en ../../app/codemap/.
RUNNER = os.environ.get("DMN_CODEMAP_RUNNER") or os.path.realpath(
    os.path.join(BACKEND, "..", "..", "app", "codemap", "nodeRunner.mjs"))
NODE = _find_bin(["node"])
GIT = _find_bin(["git"])
PUEDE_ESCANEAR = bool(NODE) and os.path.exists(RUNNER)

# Un proyecto chico con lo que importa: imports, un evento, una cadena, un ciclo, un
# nombre repetido (ambigüedad) y cosas que se tienen que saltear.
SECRETO = "SECRETO-DEL-FUENTE-" + str(os.getpid())
PROYECTO = {
    "main.js": (
        'import { save, load } from "./store.js";\n'
        'import { render } from "./view.js";\n'
        'export function boot() {\n'
        '  document.getElementById("save").addEventListener("click", () => {\n'
        '    save(load());\n'
        '  });\n'
        '  render();\n'
        '}\n'
        'boot();\n'),
    "store.js": (
        'import { notify } from "./view.js";\n'
        'export function load() { return JSON.parse(localStorage.getItem("k") || "{}"); }\n'
        'export function save(data) {\n'
        '  localStorage.setItem("k", JSON.stringify(data));\n'
        '  notify("' + SECRETO + '");\n'
        '}\n'),
    "view.js": (
        'import { load } from "./store.js";\n'
        'export function notify(msg) { helper(msg); }\n'
        'export function render() { helper(load()); }\n'
        'function helper(x) { return String(x); }\n'),
    "util/a.js": 'export function helper() { return 1; }\n',
    "node_modules/lib/index.js": 'export function nope() {}\n',
    "dist/bundle.js": 'function nope2() {}\n',
    "vendor.min.js": 'function nope3() {}\n',
}

home = tempfile.mkdtemp(prefix="dmcodemap-")
afuera = tempfile.mkdtemp(prefix="dmcodemap-afuera-")
port = puerto_libre()
env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home, DMN_CODEMAP_RUNNER=RUNNER)
srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
mcp = None
try:
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    tokfile = None
    for raiz, _, archivos in os.walk(home):
        if "token.txt" in archivos:
            tokfile = os.path.join(raiz, "token.txt")
            break
    token = open(tokfile).read().strip() if tokfile else ""
    check("el backend levantó y dejó su token", bool(token))

    def http(path, body):
        req = urllib.request.Request(f"{base}{path}?token={token}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def politica(**patch):
        return http("/mcp/policy", patch)[1]

    root = json.loads(urllib.request.urlopen(f"{base}/config?token={token}").read())["root"]
    # una carpeta que la web ya conoce (con index.json): ahí se crean los mapas nuevos
    os.makedirs(os.path.join(root, "Local"), exist_ok=True)
    with open(os.path.join(root, "Local", "index.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": []}, f)

    codigo = os.path.join(home, "codigo")
    proj = os.path.join(codigo, "proj")
    for rel, src in PROYECTO.items():
        p = os.path.join(proj, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
    with open(os.path.join(afuera, "x.js"), "w", encoding="utf-8") as f:
        f.write("export function fuera() {}\n")

    mcp = Mcp(dict(env, DMD_URL=base, DMD_TOKEN=token))
    mcp.pedir("initialize", {"protocolVersion": "2024-11-05"})

    print("\n### A. los niveles: consultar es de «diagrams», escanear es de «files»")
    tools = mcp.lista()
    consultas = ["code_calls", "code_diff_impact", "code_find", "code_impact", "code_map_overview", "code_quality", "code_who_calls"]
    check("en «diagrams» están las 7 que CONSULTAN un mapa", all(t in tools for t in consultas), str(tools))
    check("pero NO code_map_scan (lee código del disco)", "code_map_scan" not in tools, str(tools))
    txt, err = mcp.tool("code_map_scan", {})
    check("llamarla igual se rechaza explicando cómo destrabarlo", err and "Settings" in txt, txt[:160])
    st, r = http("/codemap/scan", {"path": proj})
    check("y el SERVIDOR también la rechaza por la red (sin origin = el caso más restringido)",
          st == 403, f"{st} {r}")
    txt, err = mcp.tool("code_map_overview", {})
    check("sin mapas, la consulta dice cómo crear uno", err and "code_map_scan" in txt, txt[:160])

    pol = politica(mode="files", root=codigo)
    tools = mcp.lista()
    check("en «files» aparece code_map_scan", pol.get("mode") == "files" and "code_map_scan" in tools, str(tools))

    print("\n### B. el confinamiento lo pone el servidor")
    txt, err = mcp.tool("code_map_scan", {"path": afuera})
    check("una carpeta FUERA de la del MCP se rechaza", err and "outside" in txt, txt[:160])
    txt, err = mcp.tool("code_map_scan", {"path": "../"})
    check("salir con «..» también", err and "outside" in txt, txt[:160])
    try:
        os.symlink(afuera, os.path.join(codigo, "atajo"))
        txt, err = mcp.tool("code_map_scan", {"path": "atajo"})
        check("y un symlink que apunta afuera también (se resuelve con realpath)", err and "outside" in txt, txt[:160])
    except (OSError, NotImplementedError):
        print("  (sin symlinks en este sistema: se saltea ese caso)")
    st, r = http("/codemap/stamp", {"path": afuera, "origin": "mcp"})
    check("la marca de frescura de una carpeta de afuera tampoco", st == 403, f"{st} {r}")

    if not PUEDE_ESCANEAR:
        print(f"\n  ⚠️  SIN node ({NODE}) o sin analizador ({RUNNER}): se saltean C–H.")
    else:
        print("\n### C. escanear crea un diagrama codemap que la app ve")
        txt, err = mcp.tool("code_map_scan", {"path": "proj"})
        check("code_map_scan responde OK con un resumen", not err and "created" in txt and "files" in txt, txt[:200])
        check("…con nombre por defecto «Code map proj»", "Code map proj" in txt, txt[:200])
        txt, err = mcp.tool("list_diagrams")
        filas = json.loads(txt) if not err else []
        check("list_diagrams lo lista como type codemap",
              any(f["name"] == "Code map proj" and f["type"] == "codemap" for f in filas), txt[:200])
        fp = os.path.join(root, "Local", "Code map proj", "tree.json")
        cands = [os.path.join(d, "tree.json") for d, _, fs in os.walk(os.path.join(root, "Local")) if "tree.json" in fs]
        crudo = open(cands[0], encoding="utf-8").read() if cands else ""
        tj = json.loads(crudo) if crudo else {}
        archivos = [f["path"] for f in (tj.get("graph") or {}).get("files", [])]
        check("el mapa tiene los 5 .js del proyecto, con la carpeta adelante (como la web)",
              sorted(archivos) == ["proj/main.js", "proj/store.js", "proj/util/a.js", "proj/view.js"],
              str(archivos))
        check("node_modules, dist y los .min.js quedan afuera", not any("nope" in crudo for _ in [0]), "")
        check("NO guarda el fuente (un literal del código no está en el tree.json)", SECRETO not in crudo)
        sc = tj.get("scan") or {}
        check("guarda la ruta absoluta y la marca de cada archivo (para saber si quedó viejo)",
              sc.get("path") == os.path.realpath(proj) and sorted(sc.get("stamp") or {}) == sorted(
                  ["main.js", "store.js", "util/a.js", "view.js"]), json.dumps(sc)[:200])
        check("y el grafo va COMPACTO como lo guarda la web (fns como arrays)",
              isinstance(tj["graph"]["fns"][0], list), str(tj["graph"]["fns"][:1]))

        print("\n### D. las consultas, con archivo:línea")
        txt, err = mcp.tool("code_map_overview", {})
        check("overview: se elige solo si hay un mapa, y dice que está al día",
              not err and "map is current" in txt, txt[:200])
        check("…y cuenta el ciclo de imports (store ⇄ view)", "Import cycles" in txt and ": 1" in txt, txt[-500:])
        check("…con TODOS sus archivos (cortar la lista mandaba al agente a reconstruirlo con grep)",
              "2 files: proj/store.js, proj/view.js" in txt, txt[-500:])
        txt, err = mcp.tool("code_find", {"query": "save"})
        check("code_find da path:line y callers", not err and "proj/store.js:3  save" in txt and "←" in txt, txt[:300])
        txt, err = mcp.tool("code_find", {"kind": "event"})
        check("code_find por kind encuentra el evento por su ELEMENTO", not err and "#save · click" in txt, txt[:300])
        txt, err = mcp.tool("code_who_calls", {"symbol": "save"})
        check("code_who_calls: lo llama el listener del click, con la línea de la llamada",
              not err and "#save · click" in txt and "call at proj/main.js:5" in txt, txt[:400])
        txt, err = mcp.tool("code_who_calls", {"symbol": "proj/main.js:4"})
        check("a un evento nadie lo llama: lo dispara el navegador, y dice dónde se registra",
              not err and "BROWSER" in txt and "boot" in txt, txt[:300])
        txt, err = mcp.tool("code_calls", {"symbol": "boot", "depth": 3})
        check("code_calls sigue la cadena y lista los eventos que registra",
              not err and "render" in txt and "REGISTERS 1" in txt, txt[:500])
        txt, err = mcp.tool("code_who_calls", {"symbol": "helper"})
        check("un nombre repetido es AMBIGUO y lista candidatos como path:line",
              err and "ambiguous" in txt and "proj/view.js:4" in txt and "proj/util/a.js:1" in txt, txt[:300])
        txt, err = mcp.tool("code_who_calls", {"symbol": "proj/view.js:helper"})
        check("…y con path:name se resuelve", not err and "notify" in txt and "render" in txt, txt[:300])
        txt, err = mcp.tool("code_impact", {"target": "save"})
        check("code_impact de una función: callers, el evento que la alcanza y que es exportada",
              not err and "Direct callers: 1" in txt and "event listener" in txt and "EXPORTED" in txt, txt[:600])
        check("…y no promete de más: dice qué no ve el análisis estático", "Not seen" in txt and "obj[k]()" in txt, txt[-300:])
        txt, err = mcp.tool("code_impact", {"target": "store.js"})
        check("code_impact de un archivo: quién lo importa, qué se usa de afuera y el ciclo",
              not err and "Imported by 2" in txt and "CYCLE" in txt and "save" in txt, txt[:600])
        check("…y el ciclo con sus archivos, no solo cuántos son", "proj/view.js. Adding" in txt, txt[-400:])
        txt, err = mcp.tool("code_quality", {})
        check("code_quality: el ciclo como hallazgo ALTO, con los archivos que lo forman",
              not err and "[HIGH] Import cycles" in txt and "proj/store.js" in txt and "proj/view.js" in txt, txt[:600])
        check("…con los puntos calientes y su puntaje explicado", "Hotspots" in txt and "import cycle ×1" in txt, txt[:600])
        check("…y dice qué mide cada regla (no es una nota)", "not a verdict" in txt and "What the rules measure" in txt, txt[-400:])
        txt, err = mcp.tool("code_quality", {"file": "main.js"})
        check("code_quality de un archivo sin hallazgos dice (none)", not err and "(none)" in txt, txt[:400])
        txt, err = mcp.tool("code_impact", {"target": "save"})
        check("code_impact suma el tamaño y la complejidad de la función", "parameters, cyclomatic complexity" in txt, txt[:300])
        txt, err = mcp.tool("code_impact", {"target": "noexiste"})
        check("un símbolo que no existe manda a code_find", err and "code_find" in txt, txt[:200])

        print("\n### E. el mapa dice cuándo quedó VIEJO, y re-escanear lo refresca en el lugar")
        time.sleep(1.1)
        with open(os.path.join(proj, "store.js"), "a", encoding="utf-8") as f:
            f.write("export function extra() { return save({}); }\n")
        txt, err = mcp.tool("code_impact", {"target": "save"})
        check("después de editar, la consulta avisa STALE con el archivo", "STALE" in txt and "store.js" in txt, txt[:300])
        txt, err = mcp.tool("code_map_scan", {"path": "proj"})
        check("re-escanear la misma carpeta REFRESCA el mismo mapa", not err and "refreshed" in txt, txt[:200])
        txt, _ = mcp.tool("list_diagrams")
        check("…sin crear otro", sum(1 for f in json.loads(txt) if f["type"] == "codemap") == 1, txt[:200])
        txt, err = mcp.tool("code_who_calls", {"symbol": "save"})
        check("y ya ve lo nuevo, al día", not err and "extra" in txt and "map is current" in txt, txt[:400])

        print("\n### F. un codemap no se lee entero ni se escribe a mano")
        txt, err = mcp.tool("read_diagram", {"name": "Code map proj"})
        check("read_diagram de un codemap devuelve el resumen, no 400 KB de arrays",
              not err and txt.startswith("Code map of") and '"fns"' not in txt, txt[:160])
        txt, err = mcp.tool("write_diagram", {"name": "Code map proj", "json": '{"type":"codemap"}'})
        check("write_diagram sobre un codemap se rechaza (se regenera con code_map_scan)",
              err and "code_map_scan" in txt, txt[:160])
        txt, err = mcp.tool("diagram_schema", {"type": "codemap"})
        check("diagram_schema de codemap explica que no se escribe a mano", not err and "code_map_scan" in txt, txt[:160])

        lienzo = {"type": "freestyle", "lastIdCharged": 0, "lastArrowId": 0, "lastShapeId": 0,
                   "attachments": {}, "nodos": [], "flechas": [], "formas": []}
        txt, err = mcp.tool("create_diagram", {"name": "Flow: save()", "folder": "Local", "json": json.dumps(lienzo)})
        txt2, _ = mcp.tool("list_diagrams")
        check("create_diagram contesta con el nombre que QUEDA en disco («Flow: save()» → «Flow save»)",
              not err and "'Flow save'" in txt and any(f["name"] == "Flow save" for f in json.loads(txt2)), txt[:160])

        print("\n### G. con menos permisos, la frescura es «desconocida», nunca «al día»")
        politica(mode="diagrams")
        txt, err = mcp.tool("code_map_overview", {})
        check("en «diagrams» la consulta anda pero NO afirma que está al día",
              not err and "freshness unknown" in txt and "map is current" not in txt, txt[:300])
        politica(mode="files", root=codigo)

        print("\n### H. un mapa escaneado en el navegador, y dos mapas")
        web = {"type": "codemap", "scan": {"root": "w", "at": 1, "files": 1, "skipped": 0},
               "graph": {"version": 1, "root": "w", "files": [{"path": "w/a.js", "lines": 2}],
                         "fns": [["(module)", 0, 1, 3], ["f", 0, 1, 0, 0]], "calls": [], "imports": [],
                         "cycles": [], "classes": [], "uses": [], "lens": [1, 1], "exported": [],
                         "stats": {"resolved": 0, "unresolved": 0, "events": 0}}}
        d = os.path.join(root, "Local", "Del navegador")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tree.json"), "w", encoding="utf-8") as f:
            json.dump(web, f)
        txt, err = mcp.tool("code_map_overview", {})
        check("con dos mapas y sin `map`, pide cuál (no elige a dedo)",
              err and "several code maps" in txt and "Del navegador" in txt, txt[:300])
        txt, err = mcp.tool("code_map_overview", {"map": "Del navegador"})
        check("el del navegador no tiene marcas: dice que no puede saber si está al día",
              not err and "Scanned in the browser" in txt, txt[:300])
        txt, err = mcp.tool("code_quality", {"map": "Del navegador"})
        check("un mapa de antes de la calidad pide re-escanear (no inventa hallazgos)",
              err and "code_map_scan" in txt, txt[:200])

    if PUEDE_ESCANEAR:
        print("\n### H2. un mapa del NAVEGADOR (sin ruta) se adopta al re-escanear, si es el mismo proyecto")
        web2 = os.path.join(codigo, "web2")
        os.makedirs(web2, exist_ok=True)
        with open(os.path.join(web2, "a.js"), "w", encoding="utf-8") as f:
            f.write("export function uno() { return 1; }\n")

        def mapa_nav(nombre, archivos):
            d = os.path.join(root, "Local", nombre)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "tree.json"), "w", encoding="utf-8") as f:
                json.dump({"type": "codemap", "scan": {"root": "web2", "at": 1, "files": len(archivos), "skipped": 0},
                           "graph": dict(web["graph"], root="web2", files=[{"path": a, "lines": 1} for a in archivos],
                                         fns=[["(module)", 0, 1, 3]], lens=[1])}, f)
        mapa_nav("Otro web2", ["web2/zzz.js", "web2/yyy.js"])     # se llama igual, pero es OTRO proyecto
        mapa_nav("Mio web2", ["web2/a.js"])
        txt, err = mcp.tool("code_map_scan", {"path": "web2"})
        check("re-escanear la carpeta refresca el mapa del navegador que coincide (no crea otro)",
              not err and "'Mio web2' refreshed" in txt, txt[:200])
        txt, _ = mcp.tool("code_map_overview", {"map": "Mio web2"})
        check("…y desde ahí tiene ruta: el agente sabe si está al día", "map is current" in txt, txt[:200])
        txt, err = mcp.tool("code_map_scan", {"path": "web2", "map": "Otro web2"})
        check("el homónimo de OTRO proyecto no se pisa (menos de la mitad de sus archivos)",
              err and "different folder" in txt, txt[:200])
        txt, _ = mcp.tool("list_diagrams")
        check("…y no se creó un mapa nuevo de web2", not any(f["name"] == "Code map web2" for f in json.loads(txt)), txt[:300])

    print("\n### I. sin node o sin analizador: un error que el modelo entiende (sin instalar nada)")
    try:
        codemap.scan(proj, None, RUNNER)
        check("sin node avisa", False, "no tiró")
    except codemap.CodeMapError as e:
        check("sin node: pide al usuario instalarlo, y dice que no lo instale el agente",
              "nodejs.org" in str(e) and "do not install" in str(e), str(e))
    try:
        codemap.scan(proj, NODE or "node", os.path.join(home, "no-existe.mjs"))
        check("sin analizador avisa", False, "no tiró")
    except codemap.CodeMapError as e:
        check("sin analizador (el backend solo, sin la web): lo dice", "no code analyzer" in str(e), str(e))

    if PUEDE_ESCANEAR and GIT:
        print("\n### J. code_diff_impact: qué toca un cambio y qué acciones del usuario hay que probar")
        politica(mode="files", root=codigo)
        def git(*a):
            return subprocess.run([GIT, *a], cwd=proj, capture_output=True, text=True)
        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
        src = open(os.path.join(proj, "store.js"), encoding="utf-8").read()
        with open(os.path.join(proj, "store.js"), "w", encoding="utf-8") as f:
            f.write(src.replace("JSON.stringify(data)", "JSON.stringify(data ?? {})"))
        with open(os.path.join(proj, "nuevo.js"), "w", encoding="utf-8") as f:
            f.write('import { load } from "./store.js";\nexport function nuevo() { return load(); }\n')
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj"})
        check("con el mapa VIEJO avisa que las líneas pueden no coincidir (escanear primero)",
              "older than the code" in txt, txt[:400])
        mcp.tool("code_map_scan", {"path": "proj"})
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj"})
        check("base HEAD: la línea cambiada cae en su función (save, línea 4)",
              not err and "proj/store.js:3  save  [lines 4]" in txt, txt[:500])
        check("…con quién la llama (el evento, y extra() de la sección E)", "2 direct caller(s): proj/main.js:4  #save · click [event], proj/store.js:7  extra" in txt, txt[txt.find("Changed"):][:700].replace("\n", " | "))
        check("…y la ACCIÓN del usuario que llega ahí — qué probar", "what to test: 1" in txt and "#save · click" in txt.split("what to test")[1], txt[-600:])
        check("un archivo nuevo SIN seguir por git también cuenta (lo que el agente acaba de crear)",
              "proj/nuevo.js:2  nuevo  [new]" in txt, txt[:600])
        check("…y no promete de más: lo que el estático no sigue", "not followed" in txt, txt[-300:])
        diff = git("diff", "--relative", "HEAD").stdout
        politica(mode="diagrams")
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj", "diff": diff})
        check("con `diff` como texto anda en «diagrams» (no lee el disco)",
              not err and "proj/store.js:3  save  [lines 4]" in txt, txt[:400])
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj"})
        check("…pero correr git en «diagrams» se rechaza, y dice cómo seguir",
              err and "files" in txt and "pass the diff" in txt, txt[:300])
        st, r = http("/codemap/diff", {"path": proj})
        check("…y el SERVIDOR también lo rechaza por la red", st == 403, f"{st} {r}")
        politica(mode="files", root=codigo)
        trampa = os.path.join(home, "pisado.txt")
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj", "base": f"--output={trampa}"})
        check("una «ref» que es una opción de git se rechaza y no escribe nada",
              err and "not a git ref" in txt and not os.path.exists(trampa), txt[:200])
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj", "base": "no-existe"})
        check("una ref que no existe: el error de git, y que se puede pasar el diff", err and "pass the diff" in txt, txt[:200])
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj", "diff": "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"})
        check("lo que no es JS se lista aparte, sin inventar impacto",
              not err and "not JavaScript" in txt and "README.md" in txt and "No changed line" in txt, txt[:400])
        txt, err = mcp.tool("code_diff_impact", {"map": "Code map proj", "diff": "diff --git a/store.js b/store.js\n--- a/store.js\n+++ b/store.js\n@@ -2,0 +3,0 @@\n"})
        bloque = codemap.parse_diff("--- a/x.js\n+++ b/x.js\n@@ -5,2 +4,0 @@\n-a\n-b\n")
        check("un hunk que solo BORRA se ubica en la línea que le sigue en el archivo nuevo", bloque["x.js"]["removed_at"] == {5} and not bloque["x.js"]["changed"], str(bloque))
        bloque = codemap.parse_diff("--- a/x.js\n+++ b/x.js\n@@ -1,3 +1,4 @@\n ctx\n-old\n+new1\n+new2\n ctx\n")
        check("con contexto: cuenta las líneas del lado nuevo", bloque["x.js"]["changed"] == {2, 3}, str(bloque))
        # contra app/ REAL, la cadena que el agente no encontraba (bench t6): importTree ←
        # importAsNewTree ← <input> · change, REGISTRADO en openDmerPicker ← #open-diagram ·
        # click, que el Home DISPARA con .click() desde #home-open
        app_dir = os.path.realpath(os.path.join(BACKEND, "..", "..", "app"))
        if os.path.isdir(app_dir):
            ga = codemap.Graph(json.loads(codemap.scan(app_dir, NODE, RUNNER)))
            txt = codemap.impact(ga, "fresh", "app/trees/treeStore.js:importTree")
            check("app/: code_impact de importTree llega a #open-diagram (quien registra el <input>) y a #home-open (que lo dispara)",
                  "#open-diagram · click" in txt and "#home-open · click" in txt, txt[:900])

    print("\n### K. el MCP de los agentes de la APP (`--mcp-code`): solo code_*, en SUS carpetas")
    # independiente de la política del MCP del usuario (ese es otro interruptor)
    politica(mode="diagrams")
    code = Mcp(dict(env, DMD_URL=base, DMD_TOKEN=token, DMD_SCAN_ROOTS=proj), "--mcp-code")
    try:
        code.pedir("initialize", {"protocolVersion": "2024-11-05"})
        tl = code.lista()
        check("lista SOLO las 8 code_* (con escanear), aunque el MCP del usuario esté en «diagrams»",
              tl == sorted(["code_map_scan", "code_map_overview", "code_find", "code_who_calls",
                            "code_calls", "code_impact", "code_quality", "code_diff_impact"]), str(tl))
        txt, err = code.tool("list_diagrams")
        check("una tool que no es del code map no existe acá", err and "unknown" in txt, txt[:120])
        txt, err = code.tool("code_map_scan", {"path": afuera})
        check("escanear FUERA de sus carpetas se rechaza en el servidor", err and "outside" in txt, txt[:160])
        if PUEDE_ESCANEAR:
            txt, err = code.tool("code_map_scan", {})
            check("sin `path`, escanea su única carpeta (y refresca el mapa que ya existe)",
                  not err and "refreshed" in txt, txt[:200])
            txt, err = code.tool("code_impact", {"map": "Code map proj", "target": "save"})
            check("consulta con frescura VERIFICADA (su carpeta)", not err and "map is current" in txt, txt[:200])
            if GIT:
                txt, err = code.tool("code_diff_impact", {"map": "Code map proj"})
                check("y el diff de SU carpeta también (git lo corre el servidor, dentro de sus raíces)",
                      not err and "proj/store.js:3  save" in txt, txt[:300])
    finally:
        code.cerrar()
    if PUEDE_ESCANEAR:
        c2 = Mcp(dict(env, DMD_URL=base, DMD_TOKEN=token, DMD_SCAN_ROOTS=""), "--mcp-code")
        c2.pedir("initialize", {})
        txt, err = c2.tool("code_map_scan", {"map": "Code map proj"})
        check("un agente SIN carpetas igual puede REFRESCAR un mapa que el usuario ya tiene",
              not err and "refreshed" in txt, txt[:200])
        txt, err = c2.tool("code_map_scan", {"path": proj + "/util"})
        check("…pero no escanear una carpeta nueva", err and "outside" in txt, txt[:200])
        c2.cerrar()
    politica(mode="files", root=codigo)

    print("\n### L. cómo se lo dan el chat y el orquestador a Claude Code")
    import claude as _claude
    import orch_cli as _oc
    spec = {"node_id": "n1", "msg": "x", "system": "S", "model": None, "effort": None, "add_dirs": [],
            "confinado": False, "mcp": {}, "mcp_env": {"url": base, "token": "tok"}, "session": None,
            "code_roots": [proj]}
    for conf in (False, True):
        cmd, cfg = _oc.ORCH_CLIS["local"].build("/bin/claude", dict(spec, confinado=conf))
        servers = json.load(open(cfg))["mcpServers"] if cfg else {}
        allowed = cmd[cmd.index("--allowedTools") + 1] if "--allowedTools" in cmd else ""
        tag = "confinado" if conf else "no confinado"
        check(f"orquestador ({tag}): server dmcode con SUS carpetas, por --mcp-code",
              "dmcode" in servers and "--mcp-code" in servers["dmcode"]["args"] and
              servers["dmcode"]["env"]["DMD_SCAN_ROOTS"] == proj, json.dumps(servers)[:200])
        check(f"orquestador ({tag}): las code_* van pre-aprobadas (headless no hay a quién preguntar)",
              "mcp__dmcode__code_impact" in allowed and "mcp__dmcode__code_map_scan" in allowed, allowed[:200])
        if cfg:
            os.remove(cfg)
    cmd, cfg = _oc.ORCH_CLIS["local"].build("/bin/claude", dict(spec, code_roots=None))
    check("un CLI sin code map (code_roots None) no recibe el server", not cfg or "dmcode" not in open(cfg).read())
    check("agy no promete el code map (no sabe cargar este MCP)",
          not getattr(_oc.ORCH_CLIS["local-antigravity"], "code_map", False))
    run = {"id": "r1", "local_url": base, "local_token": "tok"}
    cmd, _ = _claude.ClaudeAdapter().build_cmd(run, "/bin/claude", "hola", home, "Local", "X", "auto",
                                               None, None, editor_target=proj)
    cfg = cmd[cmd.index("--mcp-config") + 1]
    servers = json.load(open(cfg))["mcpServers"]
    allowed = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--allowedTools"]
    check("chat: server dmcode, que escanea solo el editor en foco",
          servers.get("dmcode", {}).get("env", {}).get("DMD_SCAN_ROOTS") == proj, json.dumps(servers)[:200])
    check("chat: UN solo --allowedTools, con las code_*", len(allowed) == 1 and "mcp__dmcode__code_find" in allowed[0], str(allowed))
    sysp = cmd[cmd.index("--append-system-prompt") + 1]
    check("chat: el preámbulo dice que un codemap no se lee ni se edita a mano", "codemap" in sysp and "NEVER read or edit" in sysp)
    os.remove(cfg)

    BUNDLE = os.path.realpath(os.path.join(BACKEND, "..", "..", "dist", "codemap-node.mjs"))
    if NODE and os.path.exists(BUNDLE):
        print("\n### M. el backend SOLO (sin la web) se baja el analizador del sitio")
        import http.server as _hs
        import threading
        sirve = {"ok": True}

        class H(_hs.BaseHTTPRequestHandler):
            def do_GET(self):
                if not sirve["ok"]:
                    self.send_response(503); self.end_headers(); return
                if self.path.endswith("/portal.mjs"):
                    body = b"<html>login to the wifi</html>" * 100
                else:
                    body = open(BUNDLE, "rb").read()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        web_srv = _hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=web_srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{web_srv.server_address[1]}"
        home2 = tempfile.mkdtemp(prefix="dmcodemap-solo-")

        def backend_solo(codemap_url):
            port2 = puerto_libre()
            env2 = {k: v for k, v in env.items() if k not in ("DMN_CODEMAP_RUNNER", "DMN_WEB_DIR")}
            env2.update(HOME=home2, USERPROFILE=home2, LOCALAPPDATA=home2, DMN_CODEMAP_URL=codemap_url)
            p2 = subprocess.Popen([sys.executable, SERVER, "--port", str(port2), "--no-ui"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env2)
            b2 = f"http://127.0.0.1:{port2}"
            for _ in range(60):
                try:
                    urllib.request.urlopen(b2 + "/health", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.25)
            t2 = ""
            for raiz, _, archivos in os.walk(home2):
                if "token.txt" in archivos:
                    t2 = open(os.path.join(raiz, "token.txt")).read().strip()
            return p2, b2, t2

        def escanear_solo(b2, t2):
            req = urllib.request.Request(f"{b2}/codemap/scan?token={t2}", method="POST",
                                         data=json.dumps({"path": proj, "origin": "web"}).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        try:
            p2, b2, t2 = backend_solo(url + "/portal.mjs")
            st, r = escanear_solo(b2, t2)
            check("un 200 que NO es el analizador (un portal del wifi) no se guarda ni se ejecuta",
                  st == 422 and "no code analyzer" in r.get("error", ""), f"{st} {str(r)[:160]}")
            p2.terminate(); p2.wait(timeout=5)
            p2, b2, t2 = backend_solo(url + "/dist/codemap-node.mjs")
            st, r = escanear_solo(b2, t2)
            check("sin la web, escanear igual anda: se baja el analizador", st == 200 and "tree" in r, f"{st} {str(r)[:160]}")
            cache = [os.path.join(d, f) for d, _, fs in os.walk(home2) for f in fs if f == "codemap-node.mjs"]
            check("…y queda guardado en la carpeta de datos", len(cache) == 1, str(cache))
            sirve["ok"] = False
            if cache:
                viejo = time.time() - 3 * 24 * 3600
                os.utime(cache[0], (viejo, viejo))       # vencido Y sin red
            st, r = escanear_solo(b2, t2)
            check("sin red, sigue con el que tenía (aunque esté vencido)", st == 200 and "tree" in r, f"{st} {str(r)[:160]}")
            p2.terminate(); p2.wait(timeout=5)
        finally:
            web_srv.shutdown()
            shutil.rmtree(home2, ignore_errors=True)

    print("\n### J. apagado, no hay nada")
    politica(enabled=False)
    check("con el MCP apagado no se lista ninguna code_*",
          not any(t.startswith("code_") for t in mcp.lista()), str(mcp.lista()))
    st, r = http("/codemap/scan", {"path": proj, "origin": "mcp"})
    check("y el servidor rechaza escanear", st == 403, f"{st} {r}")

finally:
    if mcp:
        mcp.cerrar()
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(afuera, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
