"""El Code map para el AGENTE (doc 40 §Etapa 5): el mapa de código como memoria.

La idea (del usuario): «si la IA va a tocar un código, que pueda consultarle a esto quién
llama a ese código o a qué llama, así lo usa como memoria y no tiene que andar leyendo
todo». Este módulo tiene las dos mitades:

- **Escanear** (`scan`): lee los .js de una carpeta y corre el MISMO analizador que la web
  (`app/codemap/analyzer.js`) con node. No hay un segundo analizador en Python: dos
  analizadores se separan con el tiempo y el mapa del agente terminaría diciendo otra
  cosa que el de la pantalla. El analizador va como `dist/codemap-node.mjs` (ofuscado,
  con acorn adentro) junto a la web; este repo es PÚBLICO y el fuente no vive acá.
  Cada archivo queda marcado con su mtime y su tamaño (`scan.stamp`): así una consulta
  puede decir si el mapa quedó VIEJO en vez de contestar con un mapa que miente.
- **Consultar** (`overview`, `find`, `who_calls`, `calls`, `impact`): puro, sobre el
  tree.json del codemap. Texto corto con `archivo:línea`, para el modelo (en inglés).

Lo que el análisis estático NO ve (y los textos lo dicen, regla dura de CLAUDE.md: una
herramienta que promete de más miente con autoridad): llamadas dinámicas (`obj[k]()`),
lo que pasa por callbacks de librerías, y el orden real de ejecución. Una llamada
resuelta "por nombre" es menos segura que una por import.
"""
import json
import os
import re
import time

import procs

# Las mismas reglas que la web (codemapMode.js): SKIP_DIRS, MAX_FILES, MAX_BYTES e
# isJsPath. Si cambian allá, cambian acá — si no, la web y el agente ven mapas distintos.
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage", "vendor", ".next", ".cache", "out",
             "venv", "__pycache__", "site-packages", "target"}   # Python y Java: lo generado, no fuente
MAX_FILES = 4000
MAX_BYTES = int(1.5 * 1024 * 1024)
# JS, TypeScript y JSX (paso 3), Python y Java (tree-sitter); un .d.ts son solo tipos. La
# MISMA regla que isJsPath + langOf de la web.
_JS = re.compile(r"\.(m?js|cjs|jsx|m?ts|cts|tsx|py|java)$", re.I)
_MIN = re.compile(r"(\.min\.js|\.d\.[mc]?ts)$", re.I)
# Código que el mapa TODAVÍA no analiza: se cuenta y se dice («2 .py not in the map»). La
# MISMA lista que unsupportedExt de analyzer.js.
_UNSUP = re.compile(r"\.(go|rb|php|cs|kt|kts|rs|swift|scala|vue|svelte|c|cc|cpp|h|hpp)$", re.I)
_UNSUP_DIRS = {"venv", ".venv", "__pycache__"}

KINDS = ["function", "method", "event", "module", "class"]
HOW = {"import": "via import", "local": "same scope", "this": "this.", "name": "BY NAME (less certain)",
       "fires": "FIRES it from code (el.click() / dispatchEvent)",
       "type": "via the DECLARED TYPE (x: Class / new Class())",
       "prop": "calls a PROP the parent passed (<C onX={…}>)",
       "http": "calls the ENDPOINT over HTTP (fetch / axios, matched by method + route)"}


def _is_endpoint(g, i):
    """Un endpoint (`router.post("/x", h)`) es una puerta de entrada como un evento, pero lo
    corre el SERVIDOR en cada request, no el navegador: el texto lo tiene que decir."""
    f = g.fns[i]
    return f["kind"] == "event" and (f.get("event") or {}).get("type") == "request"


def _who_fires(g, i):
    return "the SERVER runs it on each request" if _is_endpoint(g, i) else "the BROWSER fires it"


class CodeMapError(Exception):
    """Un error que el modelo puede corregir solo (el texto va en inglés)."""


# ---------------------------------------------------------------------------------
# escanear

def is_js(name):
    return bool(_JS.search(name)) and not _MIN.search(name)


def walk(root, unsupported=None):
    """{ruta relativa con '/': (absoluta, mtime_ms, size)} y cuántos se saltearon.
    No sigue symlinks de carpetas: un link a `/` no convierte el escaneo en el disco."""
    out, skipped = {}, 0
    unsupported = {} if unsupported is None else unsupported
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and x not in _UNSUP_DIRS and not x.startswith("."))
        for f in sorted(files):
            if not is_js(f):
                m = _UNSUP.search(f)
                if m:
                    unsupported[m.group(1).lower()] = unsupported.get(m.group(1).lower(), 0) + 1
                continue
            full = os.path.join(d, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if len(out) >= MAX_FILES or st.st_size > MAX_BYTES:
                skipped += 1
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = (full, int(st.st_mtime * 1000), st.st_size)
    return out, skipped


def stamp(root):
    """La marca actual de cada .js de la carpeta: {rel: [mtime_ms, size]}."""
    files, _ = walk(root)
    return {rel: [m, s] for rel, (_, m, s) in files.items()}


def scan(root, node_bin, runner):
    """Escanea `root` (absoluta) y devuelve el tree.json del codemap como TEXTO — el
    mismo que guarda la web. Texto y no dict: re-serializarlo con indent triplicaba
    el tamaño (son arrays de miles de elementos)."""
    if not node_bin:
        raise CodeMapError("scanning code needs Node.js, and it is not installed on this "
                           "computer (or not on a path DiagraMinder knows). Ask the user to "
                           "install it from nodejs.org — do not install it yourself.")
    if not runner or not os.path.exists(runner):
        raise CodeMapError("this DiagraMinder backend has no code analyzer: it ships with the "
                           "desktop app, and the backend alone downloads it from diagraminder.com "
                           "the first time — that download failed (no internet?). Ask the user to "
                           "retry with internet, use the desktop app, or scan the folder from the "
                           "Code map mode.")
    root = os.path.realpath(root)
    unsupported = {}   # «2 .py not in the map»: lo que el lenguaje todavía no permite analizar
    files, skipped = walk(root, unsupported)
    if not files:
        raise CodeMapError(f"no JavaScript, TypeScript, Python or Java files in {root} (node_modules, dist, "
                           "vendor, build, venv, target, minified files and .d.ts are skipped).")
    base = os.path.basename(root) or "project"
    sources = {}
    for rel, (full, _, _) in files.items():
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            sources[f"{base}/{rel}"] = fh.read()
    scan_info = {"root": base, "at": int(time.time() * 1000), "files": len(files),
                 "skipped": skipped, "path": root, "by": "backend",
                 "unsupported": unsupported,
                 "stamp": {rel: [m, s] for rel, (_, m, s) in files.items()}}
    payload = {"root": base, "sources": sources, "scan": scan_info}
    # los alias de un monorepo TS (`compilerOptions.paths`): el texto va crudo, lo entiende el
    # MISMO código que en la web (aliasesFromTsconfig)
    for cfg_name in ("tsconfig.json", "jsconfig.json"):
        cfg_path = os.path.join(root, cfg_name)
        if os.path.isfile(cfg_path) and os.path.getsize(cfg_path) < 512 * 1024:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
                payload["tsconfig"] = fh.read()
            break
    p = procs.run([node_bin, runner], input=json.dumps(payload),
                  capture_output=True, text=True, encoding="utf-8", timeout=300)
    if p.returncode != 0 or not (p.stdout or "").startswith("{"):
        raise CodeMapError("the analyzer failed: " + ((p.stderr or "").strip().splitlines() or ["no output"])[-1][:300])
    return p.stdout


def freshness(tree, current):
    """¿El mapa sigue diciendo la verdad? `current` es stamp() de la carpeta, o None si
    no se pudo mirar. Devuelve (estado, texto): fresh | stale | unknown."""
    sc = tree.get("scan") or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime((sc.get("at") or 0) / 1000)) if sc.get("at") else "?"
    old = sc.get("stamp")
    if not isinstance(old, dict):
        return "unknown", (f"Scanned in the browser on {when}: DiagraMinder can't tell whether the "
                           "code changed since. Treat it as possibly stale; code_map_scan on the "
                           "project folder gives a map that tracks changes.")
    if current is None:
        return "unknown", (f"Scanned on {when} from {sc.get('path')}; freshness unknown (that folder "
                           "is not one DiagraMinder's MCP may look at: that needs level 'files' with the folder inside its root).")
    changed = sorted(r for r in old if r in current and list(current[r]) != list(old[r]))
    gone = sorted(r for r in old if r not in current)
    new = sorted(r for r in current if r not in old)
    if not (changed or gone or new):
        return "fresh", f"Scanned on {when}; no source file changed since — the map is current."
    parts = []
    for label, lst in (("changed", changed), ("new", new), ("deleted", gone)):
        if lst:
            parts.append(f"{len(lst)} {label} ({', '.join(lst[:6])}{', …' if len(lst) > 6 else ''})")
    return "stale", ("⚠ STALE: since the scan on " + when + ", " + "; ".join(parts) +
                     ". Call code_map_scan to refresh before trusting lines and edges in those files.")


# ---------------------------------------------------------------------------------
# el grafo, en memoria

class Graph:
    """El grafo de un tree.json de codemap, con índices de entrada y salida."""

    def __init__(self, tree):
        g = (tree or {}).get("graph")
        if not isinstance(g, dict) or not isinstance(g.get("fns"), list):
            raise CodeMapError("this code map was never scanned (it is empty). Call code_map_scan.")
        self.tree = tree
        self.files = [f.get("path", "?") for f in g.get("files") or []]
        self.file_err = {i: f.get("error") for i, f in enumerate(g.get("files") or []) if f.get("error")}
        self.fns = []
        for a in g["fns"]:
            if isinstance(a, dict):        # ya expandido
                self.fns.append(a)
                continue
            a = list(a) + [None] * 8
            f = {"name": a[0], "file": a[1], "line": a[2], "kind": KINDS[a[3]] if isinstance(a[3], int) and 0 <= a[3] < len(KINDS) else "function"}
            if isinstance(a[4], int) and a[4] >= 0:
                f["parent"] = a[4]
            if a[5]:
                f["cls"] = a[5]
            if f["kind"] == "event":
                f["event"] = {"target": a[6] or "", "type": a[7] or ""}
            self.fns.append(f)
        self.calls = g.get("calls") or []
        self.out = [[] for _ in self.fns]
        self.inc = [[] for _ in self.fns]
        for c in self.calls:
            fr, to = c[0], c[1]
            if 0 <= fr < len(self.fns) and 0 <= to < len(self.fns):
                self.out[fr].append(c)
                self.inc[to].append(c)
        self.imports = g.get("imports") or []
        self.cycles = g.get("cycles") or []
        self.classes = g.get("classes") or []
        self.lens = g.get("lens") or []
        self.params = g.get("params")       # etapa 7: ausentes en un mapa de antes
        self.cx = g.get("cx")
        self.quality = g.get("quality")
        self.exported = set(g.get("exported") or [])
        self.uses = {u[0]: u[1] for u in g.get("uses") or [] if isinstance(u, list) and len(u) == 2}
        self.registers = {}            # función → eventos que registra
        for i, f in enumerate(self.fns):
            if f["kind"] == "event" and f.get("parent") is not None:
                self.registers.setdefault(f["parent"], []).append(i)
                # «Quién registra un listener» NO se recorre como si lo llamara: se probó
                # (bench t6) y lo que redibuja una barra pasaba por «llegar» a lo que hace su
                # botón. Registrar es una condición previa, no la acción. Los casos en que
                # quien registra también DISPARA (`input.click()` del selector de archivo) los
                # conecta el analizador con un borde «fires».
        self.root = (tree.get("scan") or {}).get("root") or g.get("root") or ""

    # --- nombres ---
    def where(self, i):
        f = self.fns[i]
        return f"{self.files[f['file']]}:{f['line']}"

    def label(self, i):
        f = self.fns[i]
        k = f["kind"]
        tag = "" if k in ("function", "method") else f" [{k}]"
        return f"{f['name']}{tag}"

    def row(self, i):
        return f"{self.where(i)}  {self.label(i)}"

    # --- resolver lo que pide el modelo ---
    def _norm_path(self, s):
        s = s.strip().replace("\\", "/")
        path = (self.tree.get("scan") or {}).get("path")
        if path:
            p = path.replace("\\", "/").rstrip("/") + "/"
            if s.startswith(p):
                s = s[len(p):]
        return s[2:] if s.startswith("./") else s

    def match_files(self, s):
        s = self._norm_path(s)
        if not s:
            return []
        exact = [i for i, p in enumerate(self.files) if p == s or p == f"{self.root}/{s}"]
        if exact:
            return exact
        return [i for i, p in enumerate(self.files) if p.endswith("/" + s)]

    def resolve(self, symbol):
        """Un símbolo → índice de función. Acepta `name`, `Class.method`, `path:name`,
        `path:line` y `#id`. Ambiguo o desconocido → CodeMapError con candidatos
        escritos de forma que se puedan pasar tal cual."""
        s = (symbol or "").strip()
        if not s:
            raise CodeMapError("`symbol` is required: a function name, Class.method, path:name or path:line.")
        if re.fullmatch(r"#\d+", s):
            i = int(s[1:])
            if 0 <= i < len(self.fns):
                return i
            raise CodeMapError(f"there is no {s} in this map.")
        cands = None
        # las extensiones salen de _JS: una lista aparte ya se quedó atrás dos veces (TS, Python)
        m = re.fullmatch(r"(.+?\.(?:" + _JS.pattern[3:-2] + r"))(?::|#)(.+)", s, re.I)
        if m:
            fidx = self.match_files(m.group(1))
            if not fidx:
                raise CodeMapError(f"no file '{m.group(1)}' in this map. Use code_find to look for it.")
            rest = m.group(2).strip()
            inf = [i for i, f in enumerate(self.fns) if f["file"] in fidx]
            if rest.isdigit():
                line = int(rest)
                exact = [i for i in inf if self.fns[i]["line"] == line and self.fns[i]["kind"] != "module"]
                if exact:
                    cands = exact
                else:
                    # la función que CONTIENE esa línea (la más interna)
                    inside = [i for i in inf if self.fns[i]["kind"] != "module" and
                              self.fns[i]["line"] <= line < self.fns[i]["line"] + (self.lens[i] if i < len(self.lens) else 1)]
                    inside.sort(key=lambda i: -self.fns[i]["line"])
                    cands = inside[:1]
            else:
                cands = [i for i in inf if self.fns[i]["name"] == rest or self.fns[i]["name"].endswith("." + rest)]
        else:
            cands = [i for i, f in enumerate(self.fns) if f["name"] == s and f["kind"] != "module"]
            if not cands:
                cands = [i for i, f in enumerate(self.fns) if f["kind"] == "method" and f["name"].endswith("." + s)]
        if not cands:
            raise CodeMapError(f"nothing called '{s}' in this map. Use code_find to search (it is "
                               "fuzzy), and pass what it returns.")
        if len(cands) > 1:
            lista = "\n".join(f"  {self.where(i)}  {self.label(i)}" for i in cands[:15])
            more = f"\n  … and {len(cands) - 15} more" if len(cands) > 15 else ""
            raise CodeMapError(f"'{s}' is ambiguous ({len(cands)} matches). Pass one as path:line:\n{lista}{more}")
        return cands[0]

    # --- recorridos ---
    def walk_dir(self, start, forward, depth, limit=150):
        """BFS en UNA dirección (mezclar las dos traía a los 'hermanos': doc 40).
        No atraviesa el código de módulo salvo que sea el punto de partida."""
        seen = {start: 0}
        order = []
        frontier = [start]
        truncated = False
        for d in range(1, depth + 1):
            nxt = []
            for u in frontier:
                if d > 1 and self.fns[u]["kind"] == "module":
                    continue
                for c in (self.out[u] if forward else self.inc[u]):
                    v = c[1] if forward else c[0]
                    if v in seen:
                        continue
                    if len(order) >= limit:
                        truncated = True
                        break
                    seen[v] = d
                    order.append((d, u, v, c))
                    nxt.append(v)
            frontier = nxt
            if not frontier:
                break
        return order, truncated


# ---------------------------------------------------------------------------------
# las consultas (texto para el modelo, en inglés)

def _header(g, fresh_text):
    sc = g.tree.get("scan") or {}
    where = sc.get("path") or sc.get("root") or g.root
    return f"Code map of {where} — {fresh_text}"


def overview(g, fresh_text, max_files=60):
    f_count = len(g.files)
    kinds = {}
    for f in g.fns:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    lines = [_header(g, fresh_text), "",
             f"{f_count} files · {kinds.get('function', 0)} functions · {kinds.get('method', 0)} methods · "
             f"{len(g.classes)} classes · {kinds.get('event', 0)} event listeners · {len(g.calls)} call edges"]
    unsup = (g.tree.get("scan") or {}).get("unsupported") or {}
    if unsup:
        lines.append("NOT in the map (languages not analyzed yet — don't assume they have no callers): " +
                     ", ".join(f"{n} .{e}" for e, n in sorted(unsup.items())))
    if g.file_err:
        lines.append(f"Files that did not parse (their code is NOT in the map): " +
                     ", ".join(f"{g.files[i]}:{(e or {}).get('line') or '?'}" for i, e in list(g.file_err.items())[:10]))
    # carpetas
    dirs = {}
    for i, p in enumerate(g.files):
        d = p.rsplit("/", 1)[0] if "/" in p else "."
        dirs.setdefault(d, []).append(i)
    lines += ["", "Folders (files):"]
    for d in sorted(dirs)[:max_files]:
        lines.append(f"  {d}/ ({len(dirs[d])})")
    # hubs
    fan_in = sorted((i for i, f in enumerate(g.fns) if f["kind"] in ("function", "method")),
                    key=lambda i: -len({c[0] for c in g.inc[i]}))[:12]
    lines += ["", "Most called (callers): " + "; ".join(
        f"{g.label(i)} ({len({c[0] for c in g.inc[i]})}) {g.where(i)}" for i in fan_in if g.inc[i])]
    imported = {}
    for e in g.imports:
        if isinstance(e[1], int) and e[1] >= 0:
            imported.setdefault(e[1], set()).add(e[0])
    top = sorted(imported, key=lambda k: -len(imported[k]))[:10]
    lines.append("Most imported files: " + "; ".join(f"{g.files[k]} ({len(imported[k])})" for k in top))
    if g.cycles:
        # la lista ENTERA: «14 files incl. a, b, c, d…» hacía que el agente reconstruyera el
        # ciclo con 41 greps (bench, 2026-10-20)
        lines.append(f"Import cycles (static imports; each is a strongly connected component): {len(g.cycles)}")
        lines += [f"  {len(c)} files: {_cycle_files(g, c)}" for c in g.cycles[:5]]
    lines += ["", "Next: code_find to locate something, code_impact before editing it, "
                  "code_who_calls / code_calls to follow a chain."]
    return "\n".join(lines)


def find(g, fresh_text, query="", kind="", file="", limit=40):
    q = (query or "").strip().lower()
    kind = (kind or "").strip().lower()
    if kind and kind not in KINDS + ["file"]:
        raise CodeMapError(f"`kind` must be one of: {', '.join(KINDS + ['file'])}.")
    fidx = None
    if file:
        fidx = set(g.match_files(file))
        if not fidx:
            raise CodeMapError(f"no file '{file}' in this map.")
    out = [_header(g, fresh_text), ""]
    if kind == "file" or (not kind and q and fidx is None):
        fm = [i for i, p in enumerate(g.files) if not q or q in p.lower()]
        cap = limit if kind == "file" else 10
        for i in fm[:cap]:
            n = sum(1 for f in g.fns if f["file"] == i and f["kind"] != "module")
            out.append(f"  {g.files[i]}  [file, {n} functions/events]")
        if kind == "file":
            if not fm:
                out.append("  (no files match)")
            elif len(fm) > cap:
                out.append(f"  … {len(fm) - cap} more")
            return "\n".join(out)
    hits = []
    for i, f in enumerate(g.fns):
        if f["kind"] == "module" and kind != "module":
            continue
        if kind and f["kind"] != kind:
            continue
        if fidx is not None and f["file"] not in fidx:
            continue
        name = f["name"].lower()
        if q and q not in name and q not in g.files[f["file"]].lower():
            continue
        score = 0 if name == q or name.endswith("." + q) else (1 if q and name.startswith(q) else 2)
        hits.append((score, f["file"], f["line"], i))
    hits.sort()
    for _, _, _, i in hits[:limit]:
        n_in = len({c[0] for c in g.inc[i]})
        n_out = len({c[1] for c in g.out[i]})
        extra = ""
        f = g.fns[i]
        if f["kind"] == "event" and f.get("parent") is not None:
            extra = f", registered in {g.label(f['parent'])}"
        out.append(f"  {g.row(i)}  (←{n_in} callers, →{n_out} calls{extra})")
    if not hits:
        out.append("  (nothing matches)")
    elif len(hits) > limit:
        out.append(f"  … {len(hits) - limit} more: narrow with `file` or `kind`.")
    out += ["", "Pass any of these to the other code_* tools as `symbol` (path:line is never ambiguous)."]
    return "\n".join(out)


def _call_lines(c):
    """Las líneas de una llamada: TODAS si se repite (c[5], desde 2026-10-20), si no la única.
    «call at 1801 ×2» sin la otra línea mandaba al agente a grepear (bench/codemap_agent.py)."""
    return c[5] if len(c) > 5 and isinstance(c[5], list) and c[5] else [c[3]]


def _cycle_files(g, c, cap=40):
    return ", ".join(g.files[x] for x in c[:cap]) + (f" … (+{len(c) - cap})" if len(c) > cap else "")


def _no_callers(g, i):
    """Qué se puede afirmar de algo sin callers. Una función común (no método) que NO se
    exporta: el analizador cuenta como uso llamarla Y nombrarla como valor (`{k: fn}`,
    `[fn]`, `return fn`, `x.cb = fn`, pasarla de argumento), así que no hay camino estático
    a ella. Decirlo con esas letras importa: el «NOT necessarily dead» a secas hacía que el
    agente lo verificara todo con grep y pagara la consulta dos veces (bench, 2026-10-20)."""
    f = g.fns[i]
    # `tree.render = function…` también es kind «function», pero se llama por despacho
    # (`x.render()`): con punto en el nombre vale lo de un método. Medido en app/: sin esta
    # distinción, 13 funciones vivas salían «unused»; con ella quedan las 4 muertas de verdad.
    if f["kind"] == "function" and "." not in f["name"] and i not in g.exported:
        return ("no callers and no references: it is not exported, and nothing in the project calls it "
                "or uses it as a value (passing it as an argument, `{k: fn}`, `[fn]`, `return fn` and "
                "`x.cb = fn` all count). Only string-based access could still reach it — `window[name]`, "
                "eval, an `onclick=\"…\"` attribute in HTML. Unless the code does that, it is unused.")
    if f["kind"] == "method" or "." in f["name"]:
        return ("no callers resolved — but this is a METHOD (or a function stored in a property): calls like `obj." + f["name"].split(".")[-1] +
                "()` are matched only when the method name is unique in the project, so dynamic dispatch "
                "(polymorphism) is not seen. Grep the method name before concluding it is unused.")
    return ("no callers in the project — it is EXPORTED, so code outside this map (another package, "
            "tests, HTML) may call it.")


def _chain(g, start, forward, depth):
    order, truncated = g.walk_dir(start, forward, depth)
    lines = []
    for d, u, v, c in order:
        how = HOW.get(c[2], c[2])
        n = f" ×{c[4]}" if len(c) > 4 and c[4] and c[4] > 1 else ""
        call_at = f"{g.files[g.fns[c[0]]['file']]}:{', '.join(str(x) for x in _call_lines(c))}"
        lines.append(f"{'  ' * d}{'→' if forward else '←'} {g.row(v)}   (call at {call_at}, {how}{n})")
    return lines, truncated


def who_calls(g, fresh_text, symbol, depth=2):
    i = g.resolve(symbol)
    depth = max(1, min(int(depth or 2), 6))
    f = g.fns[i]
    out = [_header(g, fresh_text), "", f"Who calls {g.row(i)} (up to {depth} levels):"]
    if f["kind"] == "event":
        p = f.get("parent")
        out.append(f"  An entry point: {_who_fires(g, i)}" +
                   (f"; it is registered in {g.row(p)}." if p is not None else "."))
        if not g.inc[i]:
            return "\n".join(out)
        out.append("  It is ALSO reached from code (el.click() / dispatchEvent / a prop / an HTTP call) — those chains:")
    lines, truncated = _chain(g, i, False, depth)
    out += lines or ["  (" + _no_callers(g, i) + ")"]
    if truncated:
        out.append("  … truncated: lower `depth`.")
    if any(c[2] == "name" for _, _, _, c in g.walk_dir(i, False, depth)[0]):
        out.append("Edges marked BY NAME were matched only by the function's name: check them.")
    return "\n".join(out)


def calls(g, fresh_text, symbol, depth=2):
    i = g.resolve(symbol)
    depth = max(1, min(int(depth or 2), 6))
    out = [_header(g, fresh_text), "", f"What {g.row(i)} calls (up to {depth} levels):"]
    lines, truncated = _chain(g, i, True, depth)
    out += lines or ["  (no calls into the project)"]
    if truncated:
        out.append("  … truncated: lower `depth`.")
    regs = g.registers.get(i)
    if regs:
        out.append(f"It also REGISTERS {len(regs)} event listener(s): " + "; ".join(g.row(e) for e in regs[:10]))
    used = g.uses.get(i)
    if used:
        out.append("Uses from outside the project: " + ", ".join(used))
    return "\n".join(out)


def _registered(g, e):
    p = g.fns[e].get("parent")
    if p is None:
        return ""
    if g.fns[p]["kind"] == "module":
        return "  (registered at module load)"
    return f"  (registered in {g.label(p)})"


def _entry_points(g, i, depth=8, limit=25):
    """Las puertas de entrada que terminan corriendo `i`: eventos (y código de módulo)
    hacia atrás. Es lo que responde «qué acción del usuario pasa por acá»."""
    order, _ = g.walk_dir(i, False, depth, limit=2000)
    reached = [v for _, _, v, _ in order]
    ev = [v for v in reached if g.fns[v]["kind"] == "event"]
    mod = [v for v in reached if g.fns[v]["kind"] == "module"]
    # una función que solo llama un evento es "registrada" por quien registra al evento
    return ev[:limit], len(ev), mod


def impact(g, fresh_text, target):
    """«¿Qué toco si cambio esto?» Para un archivo o un símbolo."""
    t = (target or "").strip()
    if not t:
        raise CodeMapError("`target` is required: a file path or a symbol.")
    out = [_header(g, fresh_text), ""]
    is_file_like = bool(_JS.search(t))
    fidx = g.match_files(t) if is_file_like else []
    if is_file_like and len(fidx) > 1:
        raise CodeMapError(f"'{t}' matches several files: " + ", ".join(g.files[x] for x in fidx[:10]) + ". Be more specific.")
    if fidx:
        fi = fidx[0]
        out.append(f"Impact of changing the FILE {g.files[fi]}:")
        if fi in g.file_err:
            out.append(f"  ⚠ it did not parse ({(g.file_err[fi] or {}).get('message')}): its code is not in the map.")
        importers = sorted({e[0] for e in g.imports if e[1] == fi})
        # cuál es dinámico: sin la marca, el agente grepeaba `import(` para comprobarlo
        # e[3]: 0 estático · 1 solo import() dinámico · 2 solo tipos (`import type`, TS)
        kind = {}
        for e in g.imports:
            if e[1] == fi:
                k = e[3] if len(e) > 3 and e[3] in (1, 2) else 0
                kind[e[0]] = min(kind.get(e[0], k), k)
        tag = {1: " (dynamic import() only)", 2: " (type-only import: gone at runtime)"}
        names = [g.files[x] + tag.get(kind.get(x, 0), "") for x in importers]
        out.append(f"- Imported by {len(importers)} file(s) — the complete list, static and dynamic: " +
                   (", ".join(names[:60]) or "none") + (f" … (+{len(names) - 60})" if len(names) > 60 else ""))
        # quién usa qué, de afuera
        mine = [i for i, f in enumerate(g.fns) if f["file"] == fi]
        used_out = []
        for i in mine:
            ext = sorted({c[0] for c in g.inc[i] if g.fns[c[0]]["file"] != fi})
            if ext:
                used_out.append((i, ext))
        out.append(f"- {len(used_out)} of its functions are called from OTHER files:")
        for i, ext in used_out[:30]:
            out.append(f"    {g.row(i)}  ← " + ", ".join(g.where(x) for x in ext[:6]) + (f" (+{len(ext) - 6})" if len(ext) > 6 else ""))
        evs = [i for i in mine if g.fns[i]["kind"] == "event"]
        if evs:
            out.append(f"- It registers {len(evs)} event listener(s) itself.")
        reach = set()
        for i in mine:
            if g.fns[i]["kind"] in ("function", "method"):
                reach.update(_entry_points(g, i, 6, 400)[0])
        reach = [e for e in reach if g.fns[e]["file"] != fi]
        if reach:
            out.append(f"- {len(reach)} event listener(s) in other files end up running code from this file: " +
                       "; ".join(g.row(e) for e in sorted(reach)[:15]) + (" …" if len(reach) > 15 else ""))
        cyc = [c for c in g.cycles if fi in c]
        if cyc:
            out.append(f"- ⚠ It is in an import CYCLE of {len(cyc[0])} files (static imports; the strongly connected "
                       f"component): {_cycle_files(g, cyc[0])}. Adding an import here can make it worse "
                       "(in a bundle, a cycle used during load fails with 'Cannot access X before initialization').")
        return "\n".join(out)

    i = g.resolve(t)
    f = g.fns[i]
    out.append(f"Impact of changing {g.row(i)}:")
    if g.cx and f["kind"] not in ("module", "class"):
        out.append(f"- Size: {g.lens[i] if i < len(g.lens) else '?'} lines, {g.params[i] if g.params else '?'} "
                   f"parameters, cyclomatic complexity {g.cx[i]}.")
    if f["kind"] == "event":
        p = f.get("parent")
        fired = sorted({c[0] for c in g.inc[i]})
        out.append(f"- It is an entry point: {_who_fires(g, i)}" +
                   (f"; the code also reaches it from: {', '.join(g.row(x) for x in fired[:10])}" if fired else "; nothing in the code reaches it") +
                   "." + (f" Registered in {g.row(p)}." if p is not None else ""))
        lines, _ = _chain(g, i, True, 2)
        out.append("- What it runs (2 levels):")
        out += ["  " + x for x in lines[:30]] or ["    (nothing in the project)"]
        return "\n".join(out)
    direct = sorted({c[0] for c in g.inc[i]})
    by_name = [c for c in g.inc[i] if c[2] == "name"]
    other_files = sorted({g.files[g.fns[x]["file"]] for x in direct if g.fns[x]["file"] != f["file"]})
    out.append(f"- Direct callers: {len(direct)}" + (f" (from {len(other_files)} other file(s): {', '.join(other_files[:10])})" if other_files else ""))
    for x in direct[:25]:
        cs = [c for c in g.inc[i] if c[0] == x]
        ls = sorted({ln for c in cs for ln in _call_lines(c)})
        out.append(f"    {g.row(x)}  (call at line {', '.join(str(ln) for ln in ls)}{', BY NAME' if any(c[2] == 'name' for c in cs) else ''})")
    if len(direct) > 25:
        out.append(f"    … {len(direct) - 25} more")
    order, truncated = g.walk_dir(i, False, 3, limit=500)
    indirect = {v for d, _, v, _ in order if d > 1}
    if indirect:
        out.append(f"- Indirect callers (2–3 levels up): {len(indirect)}{'+' if truncated else ''}")
    ev, n_ev, mod = _entry_points(g, i)
    if n_ev:
        # las acciones del usuario y los endpoints, por separado: «qué pruebo en la pantalla» y
        # «qué requests pruebo» son dos listas distintas
        ui = [e for e in ev if not _is_endpoint(g, e)]
        eps = [e for e in ev if _is_endpoint(g, e)]
        if ui:
            out.append(f"- Reached from {len(ui)} user action(s) (event listeners) — what to test in the UI:")
            out += [f"    {g.row(e)}{_registered(g, e)}" for e in ui]
        if eps:
            out.append(f"- Reached from {len(eps)} ENDPOINT(s) — the requests that run this code:")
            out += [f"    {g.row(e)}" for e in eps]
        if n_ev > len(ev):
            out.append(f"    … {n_ev - len(ev)} more")
    if mod:
        out.append("- Also run at module load from: " + ", ".join(g.files[g.fns[m]["file"]] for m in mod[:8]))
    if i in g.exported:
        out.append("- It is EXPORTED: code outside this map (other packages, tests, HTML) may call it too.")
    regs = g.registers.get(i)
    if regs:
        out.append(f"- It registers {len(regs)} event listener(s): " + "; ".join(g.row(e) for e in regs[:8]))
    if not direct and not n_ev:
        out.append("- " + _no_callers(g, i)[0].upper() + _no_callers(g, i)[1:])
    if by_name:
        out.append(f"- {len(by_name)} caller edge(s) were matched BY NAME only: verify them.")
    out.append("These lists are complete for every call the analysis resolves (by import, scope or this; "
               "BY NAME is flagged), and follow `el.click()` / `dispatchEvent(new …Event(\"x\"))` when the "
               "element is #id, document or window. Not seen: computed calls (obj[k]()), events fired on "
               "other elements, and HTML attributes. After you edit, call code_map_scan so the map stays true.")
    return "\n".join(out)


def quality(g, fresh_text, file="", limit=30):
    """code_quality: los hallazgos que calculó el analizador (app/codemap/quality.js) al
    escanear — las reglas viven en UN lugar, en JS; acá solo se formatean."""
    q = g.quality
    if not isinstance(q, dict):
        raise CodeMapError("this map has no quality data (it was scanned before that existed). "
                           "Refresh it with code_map_scan.")
    rules = q.get("rules") or {}
    sev_name = ["info", "warn", "HIGH"]
    st = q.get("stats") or {}
    out = [_header(g, fresh_text), "",
           "Quality findings — pointers to look at, not a verdict. Each says the rule, the measured "
           "value and the threshold.",
           f"{st.get('functions', '?')} functions · average cyclomatic complexity {st.get('avgCx', '?')} · "
           f"highest {st.get('maxCx', '?')} · {st.get('unresolved', 0)} calls could not be resolved (dynamic)."]
    fidx = None
    if file:
        fidx = set(g.match_files(file))
        if not fidx:
            raise CodeMapError(f"no file '{file}' in this map.")

    def where(rule, idx):
        on = (rules.get(rule) or {}).get("on")
        if on == "fn":
            return g.row(idx), {g.fns[idx]["file"]}
        if on == "file":
            return g.files[idx], {idx}
        cyc = g.cycles[idx] if idx < len(g.cycles) else []
        return (f"{len(cyc)} files importing each other: {_cycle_files(g, cyc)}"), set(cyc)

    if not fidx:
        hs = q.get("hotspots") or []
        if hs:
            out += ["", "Hotspots (score = 3 per HIGH finding + 1 per warning):"]
            for fi, score, by in hs[:10]:
                out.append(f"  {g.files[fi]}  score {score}: " +
                           ", ".join(f"{(rules.get(r) or {}).get('short') or r} ×{n}" for r, n in by.items()))
    out += ["", "Findings, most serious first:" if not fidx else f"Findings in {', '.join(g.files[x] for x in fidx)}:"]
    shown = 0
    for rule, sev, idx, val in q.get("findings") or []:
        text, files = where(rule, idx)
        if fidx and not (files & fidx):
            continue
        if shown >= limit:
            shown += 1
            continue
        r = rules.get(rule) or {}
        thr = f"≥ {r.get('warn')}" if sev < 2 or not r.get("high") else f"≥ {r.get('high')}"
        medida = "" if r.get("on") == "cycle" else f"  ({val}, threshold {thr})"
        out.append(f"  [{sev_name[sev]}] {r.get('title', rule)}: {text}{medida}")
        shown += 1
    if shown == 0:
        out.append("  (none)")
    elif shown > limit:
        out.append(f"  … {shown - limit} more: raise `limit` or pass `file`.")
    used = sorted({f[0] for f in q.get("findings") or []})
    if used:
        out += ["", "What the rules measure:"] + [f"  {(rules.get(r) or {}).get('title', r)}: {(rules.get(r) or {}).get('why', '')}" for r in used]
    return "\n".join(out)


# ---------------------------------------------------------------------------------
# el impacto de un DIFF (doc 40 §Etapa 9): «¿qué toca este cambio y qué hay que probar?»

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# Una ref de git que se le pasa a `git diff`: sin espacios y sin arrancar con «-» (que
# git leería como una opción: `--output=…` escribiría un archivo).
_REF = re.compile(r"^[A-Za-z0-9_./~^@{}:+-]{1,200}$")


def parse_diff(text):
    """Un diff unificado → {ruta: {"changed": {líneas nuevas}, "removed_at": {líneas},
    "new": bool, "deleted": bool}}. Las líneas son del lado NUEVO (el código como está),
    que es el que tiene el mapa si se escaneó después del cambio. Una línea borrada se
    ubica donde estaba en el archivo nuevo: así cae en la función que la perdió."""
    files, cur, new_line = {}, None, 0
    old_path = None
    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            cur, old_path = None, None
            continue
        if raw.startswith("--- "):
            p = raw[4:].strip().split("\t")[0]
            old_path = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip().split("\t")[0]
            deleted = p == "/dev/null"
            path = old_path if deleted else (p[2:] if p.startswith("b/") else p)
            if not path:
                cur = None
                continue
            cur = files.setdefault(path, {"changed": set(), "removed_at": set(), "new": old_path is None, "deleted": deleted})
            continue
        m = _HUNK.match(raw)
        if m and cur is not None:
            new_line = int(m.group(1))
            # «+N,0» = un hunk que solo borra, DESPUÉS de la línea N: lo borrado se ubica en
            # la que sigue (N+1), igual que un «-» en un diff con contexto. Al final de una
            # función esa línea es su «}», así que cae adentro.
            if m.group(2) == "0":
                new_line += 1
            continue
        if cur is None or not raw:
            continue
        if raw[0] == "+":
            cur["changed"].add(new_line)
            new_line += 1
        elif raw[0] == "-":
            cur["removed_at"].add(new_line)
        elif raw[0] == " ":
            new_line += 1
    return files


def git_diff(folder, base, git_bin):
    """`git diff <base>` de la carpeta del mapa (cambios sin commitear contra HEAD, por
    defecto), con rutas relativas a ella, + los archivos nuevos que git todavía no sigue
    (`git diff` no los muestra y son justamente lo que un agente acaba de crear)."""
    base = (base or "HEAD").strip()
    if not _REF.match(base) or base.startswith("-"):
        raise CodeMapError(f"'{base}' is not a git ref (a branch, tag, commit or HEAD~N).")
    if not git_bin:
        raise CodeMapError("git is not installed (or not on a path DiagraMinder knows): pass the "
                           "unified diff as `diff` instead.")
    p = procs.run([git_bin, "diff", "--no-color", "--no-ext-diff", "--unified=0", "--relative", base, "--"],
                  cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if p.returncode != 0:
        msg = ((p.stderr or "").strip().splitlines() or ["git diff failed"])[-1][:300]
        raise CodeMapError(f"git diff {base} failed in {folder}: {msg}. Pass the diff as `diff` instead.")
    u = procs.run([git_bin, "ls-files", "--others", "--exclude-standard", "--", "."],
                  cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    untracked = [l.strip() for l in (u.stdout or "").splitlines() if l.strip()] if u.returncode == 0 else []
    return p.stdout, untracked


def _innermost(g, file_idx, line, spans):
    """La función/método/evento más interno que contiene `line`, o None (código suelto
    del módulo). `spans` = [(inicio, fin, idx)] de ese archivo."""
    best = None
    for a, b, i in spans:
        if a <= line <= b and (best is None or a >= best[0]):
            best = (a, b, i)
    return best[2] if best else None


def diff_impact(g, fresh_text, diff_text, untracked=(), limit=40):
    """code_diff_impact: cada línea cambiada → la función que la contiene; de cada una,
    quién la llama y qué ACCIONES del usuario llegan a ella. La unión de esas acciones es
    lo que hay que probar después del cambio."""
    parsed = parse_diff(diff_text)
    for p in untracked or ():
        parsed.setdefault(p, {"changed": set(), "removed_at": set(), "new": True, "deleted": False})
    if not parsed:
        raise CodeMapError("the diff is empty: nothing changed (or `diff` was not a unified diff).")
    out = [_header(g, fresh_text), ""]
    if "STALE" in fresh_text:
        out += ["⚠ The map is older than the code: line numbers in the diff may not match it, and "
                "functions added since the scan are missing. Call code_map_scan first, then this "
                "again.", ""]
    not_js, not_in_map, deleted = [], [], []
    touched = {}            # fn idx → {"lines": set, "whole": bool}
    module_files = set()    # archivos con cambios en código suelto del módulo
    for path, d in sorted(parsed.items()):
        if not _JS.search(path) or _MIN.search(path):
            not_js.append(path)
            continue
        fidx = g.match_files(path)
        if len(fidx) != 1:
            (deleted if d["deleted"] else not_in_map).append(path + (" (matches several files)" if len(fidx) > 1 else ""))
            continue
        fi = fidx[0]
        if d["deleted"]:
            deleted.append(g.files[fi])
            continue
        spans = [(f["line"], f["line"] + (g.lens[i] if i < len(g.lens) else 1) - 1, i)
                 for i, f in enumerate(g.fns) if f["file"] == fi and f["kind"] in ("function", "method", "event")]
        if d["new"] and not d["changed"]:
            # nuevo y sin diff de líneas (untracked): el archivo entero es nuevo
            for _, _, i in spans:
                touched.setdefault(i, {"lines": set(), "whole": True})
            module_files.add(fi)
            continue
        for ln in sorted(d["changed"] | d["removed_at"]):
            i = _innermost(g, fi, ln, spans)
            if i is None:
                module_files.add(fi)
            else:
                touched.setdefault(i, {"lines": set(), "whole": False})["lines"].add(ln)

    if not touched and not module_files:
        out.append("No changed line falls in code of this map.")
    actions = {}            # evento → funciones cambiadas que lo alcanzan
    rows = sorted(touched, key=lambda i: (g.files[g.fns[i]["file"]], g.fns[i]["line"]))
    if rows:
        out.append(f"Changed functions and listeners: {len(rows)}")
    for n, i in enumerate(rows):
        f = g.fns[i]
        t = touched[i]
        if f["kind"] == "event":
            actions.setdefault(i, set()).add(i)
        else:
            for e in _entry_points(g, i, 8, 2000)[0]:
                actions.setdefault(e, set()).add(i)
        if n >= limit:
            continue
        what = "new" if t["whole"] else "lines " + _ranges(t["lines"])
        callers = sorted({c[0] for c in g.inc[i] if c[0] != i})
        bits = []
        if f["kind"] == "event":
            bits.append(f"an entry point: {_who_fires(g, i)}" + _registered(g, i))
        else:
            bits.append(f"{len(callers)} direct caller(s)" + (": " + ", ".join(g.row(x) for x in callers[:5]) +
                                                            (f" (+{len(callers) - 5})" if len(callers) > 5 else "") if callers else ""))
            if i in g.exported:
                bits.append("EXPORTED (code outside the map may call it)")
            if any(c[2] == "name" for c in g.inc[i]):
                bits.append("some callers matched BY NAME")
        out.append(f"  {g.row(i)}  [{what}] — " + "; ".join(bits))
    if len(rows) > limit:
        out.append(f"  … {len(rows) - limit} more changed functions (raise `limit`).")
    if module_files:
        out.append("")
        out.append("Changed module-level code (runs when the file loads), in: " + ", ".join(g.files[x] for x in sorted(module_files)))
        for fi in sorted(module_files):
            imp = sorted({e[0] for e in g.imports if e[1] == fi})
            if imp:
                out.append(f"  {g.files[fi]} is imported by {len(imp)}: " + ", ".join(g.files[x] for x in imp[:20]) +
                           (f" … (+{len(imp) - 20})" if len(imp) > 20 else ""))
    out.append("")
    if actions:
        out.append(f"User actions and endpoints that reach the changed code — what to test: {len(actions)}")
        for e in sorted(actions, key=lambda e: (g.files[g.fns[e]["file"]], g.fns[e]["line"]))[:max(limit, 40)]:
            via = sorted(actions[e] - {e})
            out.append(f"  {g.row(e)}{_registered(g, e)}" +
                       (f"  via {', '.join(g.label(x) for x in via[:3])}{' …' if len(via) > 3 else ''}" if via else "  (changed itself)"))
        if len(actions) > max(limit, 40):
            out.append(f"  … {len(actions) - max(limit, 40)} more")
    elif rows:
        out.append("No event listener reaches the changed code statically (it may run at load, be "
                   "exported for outside callers, or be reached dynamically).")
    for label, lst in (("Deleted files (their callers are not in this map any more)", deleted),
                       ("JS/TS files NOT in the map (new since the scan? call code_map_scan)", not_in_map),
                       ("Other changed files (not JS/TS: not analyzed)", not_js)):
        if lst:
            out.append(f"{label}: " + ", ".join(lst[:30]) + (f" … (+{len(lst) - 30})" if len(lst) > 30 else ""))
    out.append("Static analysis: `el.click()` and dispatched events are followed only on #id, document or "
               "window; computed calls (obj[k]()) and HTML attributes are not followed — a user action "
               "reached only that way is not in the list.")
    return "\n".join(out)


def _ranges(lines):
    """{3,4,5,9} → «3-5, 9»."""
    xs = sorted(lines)
    out, start, prev = [], None, None
    for x in xs:
        if start is None:
            start = prev = x
        elif x == prev + 1:
            prev = x
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = x
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else str(start))
    return ", ".join(out[:8]) + (" …" if len(out) > 8 else "")
