"""El mock del modo Object (doc 23 §Mock): el backend al REVÉS.

La web publica una tabla de rutas (método + ruta + status + headers + body ya
serializado) y el backend las sirve en /mock/<projectId>/<ruta> para que cualquier
programa de la máquina les pegue. Acá: la lógica pura (match, respuesta) y el server
REAL (HOME temporal, puerto libre), sin red.

    python3 backend/tests/test_mocks.py
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

import mocks  # noqa: E402

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


print("\n### A. lógica pura")
R = [mocks.normalize_route(r) for r in [
    {"id": 1, "method": "GET", "path": "/users", "body": [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Juan"}]},
    {"id": 2, "method": "GET", "path": "/users/:id", "byParam": True,
     "body": [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Juan"}]},
    {"id": 3, "method": "GET", "path": "/users/me", "body": {"id": 0, "name": "yo"}},
    {"id": 4, "method": "ANY", "path": "/ping", "raw": "pong"},
    {"id": 5, "method": "POST", "path": "/users", "status": 201, "body": {"ok": True}},
    {"id": 6, "method": "DELETE", "path": "/users/:id", "status": 204},
]]
r, p, _ = mocks.match(R, "GET", "/users/me")
check("una ruta literal le gana a una con parámetro (/users/me antes que /users/:id)", r and r["id"] == 3)
r, p, _ = mocks.match(R, "GET", "/users/2")
check("el parámetro se captura", r and r["id"] == 2 and p == {"id": "2"}, str(p))
st, h, data = mocks.build_response(r, p)
check("byParam: devuelve EL elemento de la colección (comparando como texto: '2' == 2)",
      st == 200 and json.loads(data) == {"id": 2, "name": "Juan"}, data)
r, p, _ = mocks.match(R, "GET", "/users/9")
st, h, data = mocks.build_response(r, p)
check("byParam sin coincidencia → 404 que dice qué se buscó", st == 404 and json.loads(data)["params"] == {"id": "9"})
r, p, _ = mocks.match(R, "GET", "/users/")
check("la barra final no importa", r and r["id"] == 1)
r, p, _ = mocks.match(R, "HEAD", "/users")
check("HEAD lo atiende la ruta GET", r and r["id"] == 1)
r, p, _ = mocks.match(R, "PATCH", "/ping")
check("ANY atiende cualquier método", r and r["id"] == 4)
r, p, allow = mocks.match(R, "PUT", "/users")
check("la ruta existe con otro método → None + los permitidos (para el 405)",
      r is None and allow == ["GET", "POST"], str(allow))
r, p, allow = mocks.match(R, "GET", "/nada")
check("una ruta que no existe → None sin allow (404)", r is None and allow is None)
st, h, data = mocks.build_response(mocks.match(R, "GET", "/ping")[0], {})
check("cuerpo crudo que no es JSON → text/plain", data == b"pong" and h["Content-Type"].startswith("text/plain"))
st, h, data = mocks.build_response(mocks.normalize_route({"path": "/x", "raw": '{"a":1}'}), {})
check("cuerpo crudo que ES JSON → application/json", h["Content-Type"].startswith("application/json"))
st, h, data = mocks.build_response(mocks.normalize_route(
    {"path": "/x", "body": {"a": 1}, "headers": {"content-type": "application/vnd.api+json"}}), {})
check("un Content-Type del usuario no se pisa", "Content-Type" not in h and h["content-type"] == "application/vnd.api+json", str(h))
st, h, data = mocks.build_response(R[5], {"id": "1"})
check("sin body: respuesta vacía con el status pedido (DELETE → 204)", st == 204 and data == b"")
r, p, _ = mocks.match([mocks.normalize_route({"path": "/files/:name"})], "GET", "/files/a%2Fb.txt")
check("un %2F adentro de un segmento NO lo parte", r and p == {"name": "a/b.txt"}, str(p))
for malo, por in ((({"path": "users"}), "sin / al principio"), ({"path": "/a/:1x"}, "parámetro inválido"),
                  ({"path": "/a", "method": "FETCH"}, "método inválido"), ({"path": "/a", "status": 700}, "status fuera de rango")):
    try:
        mocks.normalize_route(malo)
        check(f"rechaza una ruta {por}", False)
    except mocks.MockError as e:
        check(f"rechaza una ruta {por}", e.code == 400)
n = mocks.normalize_route({"path": "/a", "headers": {"X-A": "1\r\nSet-Cookie: x", "Bad\nName": "v"}, "delayMs": 999999})
check("un header no puede partir la respuesta HTTP (sin \\r\\n)", n["headers"] == {"X-A": "1  Set-Cookie: x"}, str(n["headers"]))
check("la demora tiene tope", n["delayMs"] == mocks.MAX_DELAY_MS)


def levantar(home, port):
    env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home, XDG_DATA_HOME=os.path.join(home, ".local", "share"))
    srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    return srv


def parar(srv):
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()


def pedir(url, method="GET", body=None, headers=None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


home = tempfile.mkdtemp(prefix="dmmock-")
port = puerto_libre()
srv = levantar(home, port)
try:
    base = f"http://127.0.0.1:{port}"
    token = ""
    for raiz, _, archivos in os.walk(home):
        if "token.txt" in archivos:
            token = open(os.path.join(raiz, "token.txt")).read().strip()
    PID = "lq8abc12"
    rutas = [
        {"id": 7, "name": "list users", "method": "GET", "path": "/users",
         "headers": {"X-Total-Count": "2"}, "body": [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Juan"}]},
        {"id": 8, "name": "one user", "method": "GET", "path": "/users/:id", "byParam": True,
         "body": [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Juan"}]},
        {"id": 9, "name": "create", "method": "POST", "path": "/users", "status": 201, "body": {"id": 3}},
        {"id": 10, "name": "slow", "method": "GET", "path": "/slow", "delayMs": 400, "raw": "ok"},
        {"id": 11, "name": "upd", "method": "PUT", "path": "/users/:id", "body": {"updated": True}},
        {"id": 12, "name": "patch", "method": "PATCH", "path": "/users/:id", "body": {"patched": True}},
        {"id": 13, "name": "del", "method": "DELETE", "path": "/users/:id", "status": 204},
    ]

    print("\n### B. publicar (con token) y servir (sin token)")
    st, _, _ = pedir(f"{base}/mocks/publish", "POST", {"projectId": PID, "routes": rutas},
                     {"Content-Type": "application/json"})
    check("publicar SIN el token local → 401 (cualquier web podría pisarte el mock)", st == 401)
    st, _, b = pedir(f"{base}/mocks/publish?token={token}", "POST",
                     {"projectId": PID, "name": "API", "routes": rutas}, {"Content-Type": "application/json"})
    check("publicar con token", st == 200 and json.loads(b)["routes"] == 7, b)
    st, h, b = pedir(f"{base}/mock/{PID}/users")
    check("GET sirve el body publicado, sin token", st == 200 and json.loads(b) == rutas[0]["body"], b)
    check("con el Content-Type de JSON, el header propio y CORS abierto",
          h.get("Content-Type", "").startswith("application/json") and h.get("X-Total-Count") == "2"
          and h.get("Access-Control-Allow-Origin") == "*", str(h))
    st, _, b = pedir(f"{base}/mock/{PID}/users/2")
    check("GET /users/:id elige el elemento de la colección", st == 200 and json.loads(b)["name"] == "Juan", b)
    st, _, b = pedir(f"{base}/mock/{PID}/users", "POST", {"name": "Eva"}, {"Content-Type": "application/json"})
    check("POST → el status configurado (201)", st == 201 and json.loads(b) == {"id": 3}, b)
    st, _, b = pedir(f"{base}/mock/{PID}/users/1", "PUT", {"x": 1})
    check("PUT", st == 200 and json.loads(b) == {"updated": True}, b)
    st, _, b = pedir(f"{base}/mock/{PID}/users/1", "PATCH", {"x": 1})
    check("PATCH", st == 200 and json.loads(b) == {"patched": True}, b)
    st, _, b = pedir(f"{base}/mock/{PID}/users/1", "DELETE")
    check("DELETE → 204 vacío", st == 204 and b == b"", b)
    st, h, b = pedir(f"{base}/mock/{PID}/users", "HEAD")
    check("HEAD: los headers sin el cuerpo", st == 200 and b == b"" and h.get("X-Total-Count") == "2")
    t0 = time.time()
    st, _, b = pedir(f"{base}/mock/{PID}/slow")
    check("la demora se respeta (400 ms)", st == 200 and time.time() - t0 >= 0.38, f"{time.time() - t0:.2f}s")
    st, h, b = pedir(f"{base}/mock/{PID}/users", "DELETE")
    check("método no configurado en una ruta que existe → 405 con Allow",
          st == 405 and "GET" in h.get("Allow", "") and "POST" in h.get("Allow", ""), str(h.get("Allow")))
    st, _, b = pedir(f"{base}/mock/{PID}/nada")
    j = json.loads(b)
    check("ruta inexistente → 404 que LISTA las rutas que sí hay", st == 404 and "GET /users" in j.get("routes", []), b)
    st, _, b = pedir(f"{base}/mock/otroProyecto/users")
    check("proyecto sin mock → 404 que dice cómo publicarlo", st == 404 and "hint" in json.loads(b), b)

    print("\n### C. lo que entra de afuera, y el preflight")
    st, _, b = pedir(f"{base}/mock/{PID}/users", headers={"Host": "algo.trycloudflare.com"})
    check("detrás del túnel (Host público) → 403: abrir el túnel del MCP no publica el mock", st == 403, b)
    st, _, b = pedir(f"{base}/mock/{PID}/users", headers={"Host": f"localhost:{port}"})
    check("Host localhost sí", st == 200)
    st, h, _ = pedir(f"{base}/mock/{PID}/users", "OPTIONS",
                     headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "PATCH",
                              "Access-Control-Request-Headers": "authorization, content-type"})
    check("preflight: todos los métodos y los headers que pide el front",
          st == 204 and "PATCH" in h.get("Access-Control-Allow-Methods", "")
          and h.get("Access-Control-Allow-Headers") == "authorization, content-type", str(h))
    st, _, _ = pedir(f"{base}/projects/tree", "PUT")
    check("PUT fuera de /mock sigue sin existir (405)", st == 405)

    print("\n### D. el log de pedidos")
    st, _, b = pedir(f"{base}/mocks/log?projectId={PID}")
    check("el log pide token", st == 401)
    st, _, b = pedir(f"{base}/mocks/log?projectId={PID}&token={token}")
    log = json.loads(b)["log"]
    post = [e for e in log if e["method"] == "POST"]
    check("registra cada pedido con su status", any(e["path"] == "/users/2" and e["status"] == 200 for e in log), str(log)[:300])
    check("y el CUERPO que mandó tu app (para ver qué te llegó)", post and json.loads(post[0]["body"]) == {"name": "Eva"}, str(post)[:200])
    check("el 404 también queda (sin ruta)", any(e["path"] == "/nada" and e["status"] == 404 and e["routeId"] is None for e in log))
    check("el pedido del túnel NO llega al log", not any(e for e in log if e["status"] == 403))
    pedir(f"{base}/mocks/logclear?token={token}", "POST", {"projectId": PID}, {"Content-Type": "application/json"})
    st, _, b = pedir(f"{base}/mocks/log?projectId={PID}&token={token}")
    check("limpiar el log", json.loads(b)["log"] == [])

    print("\n### E. publicar mal, y persistencia")
    st, _, b = pedir(f"{base}/mocks/publish?token={token}", "POST",
                     {"projectId": "../x", "routes": rutas}, {"Content-Type": "application/json"})
    check("projectId inválido → 400", st == 400)
    st, _, b = pedir(f"{base}/mocks/publish?token={token}", "POST",
                     {"projectId": PID, "routes": [{"path": "sin-barra"}]}, {"Content-Type": "application/json"})
    check("una ruta mala → 400 y NO pisa la tabla anterior",
          st == 400 and pedir(f"{base}/mock/{PID}/users")[0] == 200)
    parar(srv)
    srv = levantar(home, port)
    st, _, b = pedir(f"{base}/mock/{PID}/users/1")
    check("el mock sobrevive a reiniciar el backend", st == 200 and json.loads(b)["name"] == "Ana", b)
    pedir(f"{base}/mocks/publish?token={token}", "POST", {"projectId": PID, "routes": []},
          {"Content-Type": "application/json"})
    st, _, _ = pedir(f"{base}/mock/{PID}/users")
    check("publicar una lista vacía lo saca (sin API fantasma)", st == 404)
finally:
    parar(srv)
    shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
