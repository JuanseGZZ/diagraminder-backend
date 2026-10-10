"""Mock server del modo Object (doc 23 §Mock): el backend al REVÉS.

Un Fetch de Object LLAMA a una API; un nodo Endpoint (`objendpoint`) la FINGE: método +
ruta (`/users/:id`) + status + headers + demora, y la respuesta es el objeto o la
colección al que apunta su flecha `body`. Esas rutas se sirven acá, en

    http://127.0.0.1:<puerto>/mock/<projectId>/<ruta>

y cualquier programa de la máquina (tu front, un test, curl, los propios Fetch del
diagrama) les puede pegar.

**El JSON lo arma la WEB, no este módulo.** Serializar el grafo (herencia, colecciones,
árboles, ciclos con `$ref`) ya vive en `objectModel.js`; reescribirlo en Python serían
dos serializadores que se separan con el tiempo. La web compila la tabla de rutas con
los bodies ya serializados y la publica (`POST /mock/publish`); acá solo se guarda,
se matchea y se responde. Consecuencia, y está documentada: si el diagrama cambia con
la web cerrada (un agente por MCP), el mock sirve lo último que la web publicó hasta
que vuelva a abrirse ese proyecto.

Lógica pura (sin HTTP): el server traduce. Errores → MockError(code, msg).
"""
import json
import os
import re
import threading
import time
from urllib.parse import unquote

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "ANY")
MAX_ROUTES = 300                 # por proyecto
MAX_PROJECT_BYTES = 5 * 1024 * 1024   # la tabla de un proyecto, serializada
MAX_DELAY_MS = 10_000
MAX_HEADERS = 30
LOG_SIZE = 50                    # pedidos recientes por proyecto (solo en memoria)
LOG_BODY_CHARS = 2000

_PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PARAM_RE = re.compile(r"^:([A-Za-z_][A-Za-z0-9_]*)$")


class MockError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def valid_pid(pid):
    return isinstance(pid, str) and bool(_PID_RE.match(pid))


def split_path(path):
    """`/users/42/` → ["users", "42"]. Decodifica cada segmento por separado: un
    `%2F` adentro de un id no tiene que partirlo en dos."""
    return [unquote(s) for s in (path or "").split("/") if s != ""]


def normalize_route(r):
    """Valida y normaliza una ruta publicada por la web. Lo que no cierra es un 400:
    la web es la única que publica, así que un dato raro es un bug, no un usuario."""
    if not isinstance(r, dict):
        raise MockError(400, "route must be an object")
    method = str(r.get("method") or "GET").upper()
    if method not in METHODS:
        raise MockError(400, f"invalid method: {method}")
    path = str(r.get("path") or "").strip()
    if not path.startswith("/") or len(path) > 300:
        raise MockError(400, f"invalid path: {path!r} (must start with /)")
    segs = split_path(path)
    for s in segs:
        if s.startswith(":") and not _PARAM_RE.match(s):
            raise MockError(400, f"invalid path parameter: {s!r}")
    try:
        status = int(r.get("status") or 200)
    except (TypeError, ValueError):
        status = 200
    if not 100 <= status <= 599:
        raise MockError(400, f"invalid status: {status}")
    try:
        delay = int(r.get("delayMs") or 0)
    except (TypeError, ValueError):
        delay = 0
    headers = {}
    for k, v in list((r.get("headers") or {}).items())[:MAX_HEADERS]:
        k = str(k).strip()
        # un header con salto de línea partiría la respuesta HTTP en dos
        if k and not re.search(r"[\r\n:]", k):
            headers[k] = re.sub(r"[\r\n]", " ", str(v))
    raw = r.get("raw")
    return {
        "id": r.get("id"),
        "name": str(r.get("name") or "")[:120],
        "method": method,
        "path": "/" + "/".join(segs),
        "status": status,
        "headers": headers,
        "delayMs": max(0, min(delay, MAX_DELAY_MS)),
        "body": r.get("body"),
        "raw": raw if isinstance(raw, str) and raw != "" else None,
        "byParam": bool(r.get("byParam")),
    }


def match(routes, method, path):
    """La ruta que atiende `method path`. Devuelve (route, params, allow):
    - (route, {params}, None) si hay una;
    - (None, None, [métodos]) si la RUTA existe pero no con ese método (→ 405);
    - (None, None, None) si no existe (→ 404).
    Si varias matchean gana la más ESPECÍFICA (más segmentos literales: `/users/me`
    le gana a `/users/:id`), y a igualdad, el método exacto le gana a ANY."""
    method = method.upper()
    segs = split_path(path)
    best, best_key, allow = None, None, set()
    for r in routes:
        rsegs = split_path(r["path"])
        if len(rsegs) != len(segs):
            continue
        params, literales, ok = {}, 0, True
        for rs, s in zip(rsegs, segs):
            m = _PARAM_RE.match(rs)
            if m:
                params[m.group(1)] = s
            elif rs == s:
                literales += 1
            else:
                ok = False
                break
        if not ok:
            continue
        rm = r["method"]
        if rm != "ANY" and rm != method and not (method == "HEAD" and rm == "GET"):
            allow.add(rm)
            continue
        key = (literales, rm != "ANY")
        if best_key is None or key > best_key:
            best, best_key = (r, params), key
    if best:
        return best[0], best[1], None
    return None, None, (sorted(allow) if allow else None)


def _pick(body, params):
    """`GET /users/:id` sobre una colección: el elemento cuyos campos coinciden con
    TODOS los parámetros de la ruta (comparando como texto: el path siempre es texto
    y el id del JSON puede ser número)."""
    for el in body:
        if isinstance(el, dict) and all(str(el.get(k)) == v for k, v in params.items()):
            return el
    return None


def build_response(route, params):
    """(status, headers, bytes) para una ruta ya matcheada."""
    headers = dict(route.get("headers") or {})
    has_ct = any(k.lower() == "content-type" for k in headers)
    body = route.get("body")
    status = route["status"]
    if route.get("byParam") and params and isinstance(body, list):
        el = _pick(body, params)
        if el is None:
            data = json.dumps({"error": "not found", "params": params}).encode("utf-8")
            return 404, {"Content-Type": "application/json; charset=utf-8"}, data
        body = el
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if not has_ct:
            headers["Content-Type"] = "application/json; charset=utf-8"
    elif route.get("raw") is not None:
        data = route["raw"].encode("utf-8")
        if not has_ct:
            try:
                json.loads(route["raw"])
                headers["Content-Type"] = "application/json; charset=utf-8"
            except ValueError:
                headers["Content-Type"] = "text/plain; charset=utf-8"
    else:
        data = b""
    return status, headers, data


class MockStore:
    """Las tablas de rutas por proyecto. Persisten en `mocks.json` (sobreviven a un
    reinicio del backend: tu front sigue teniendo su API falsa aunque no hayas abierto
    la web todavía). El log de pedidos NO persiste: es para mirar qué mandó tu app
    recién, no un historial."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.projects = {}
        self.logs = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.projects = {k: v for k, v in data.items() if valid_pid(k) and isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.projects, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def publish(self, pid, name, routes):
        """Reemplaza la tabla del proyecto. Una lista vacía lo saca (no deja una API
        fantasma de un proyecto al que le borraste todos los endpoints)."""
        if not valid_pid(pid):
            raise MockError(400, "invalid projectId")
        if not isinstance(routes, list):
            raise MockError(400, "routes must be a list")
        if len(routes) > MAX_ROUTES:
            raise MockError(413, f"too many routes (max {MAX_ROUTES})")
        norm = [normalize_route(r) for r in routes]
        entry = {"name": str(name or "")[:120], "routes": norm, "ts": int(time.time() * 1000)}
        if len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) > MAX_PROJECT_BYTES:
            raise MockError(413, "the mock is too big (max 5 MB per project)")
        with self.lock:
            if norm:
                self.projects[pid] = entry
            else:
                self.projects.pop(pid, None)
            self._save()
        return {"ok": True, "routes": len(norm)}

    def get(self, pid):
        with self.lock:
            return self.projects.get(pid)

    def record(self, pid, entry):
        with self.lock:
            log = self.logs.setdefault(pid, [])
            log.append(entry)
            del log[:-LOG_SIZE]

    def log(self, pid):
        with self.lock:
            return list(self.logs.get(pid, []))

    def clear_log(self, pid):
        with self.lock:
            self.logs.pop(pid, None)


def log_entry(method, path, query, status, route, req_body):
    txt = req_body.decode("utf-8", "replace") if isinstance(req_body, bytes) else (req_body or "")
    if len(txt) > LOG_BODY_CHARS:
        txt = txt[:LOG_BODY_CHARS] + f"… (+{len(txt) - LOG_BODY_CHARS} chars)"
    return {
        "ts": int(time.time() * 1000), "method": method, "path": path, "query": query or "",
        "status": status, "routeId": route.get("id") if route else None,
        "routeName": route.get("name") if route else None, "body": txt,
    }
