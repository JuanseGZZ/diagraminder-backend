"""`POST /projects/move`: mover un proyecto de carpeta SIN perder lo que vive en su directorio.

Por qué existe (2026-10-20): mover un file de carpeta en la web era, para el mirror,
escribir el tree.json en la carpeta nueva y que el manifiesto podara la vieja con un
`rmtree`. Adentro del directorio también viven el historial de versiones del código
(`source-versions/`) y los adjuntos del chat (`tempFiles/`): se perdían. Ahora la web pide
el movimiento antes del sync y el backend RENOMBRA el directorio.

Levanta el server real con un HOME temporal y un puerto libre.

    python3 backend/tests/test_projects_move.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server.py")
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


home = tempfile.mkdtemp(prefix="dmmove-")
port = puerto_libre()
env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home)
srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
try:
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    tok = next(open(os.path.join(d, "token.txt")).read().strip() for d, _, fs in os.walk(home) if "token.txt" in fs)

    def post(path, body):
        req = urllib.request.Request(f"{base}{path}?token={tok}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    root = json.loads(urllib.request.urlopen(f"{base}/config?token={tok}").read())["root"]
    tj = json.dumps({"type": "cart", "nodes": []})

    print("\n### mover conserva lo que vive en el directorio del proyecto")
    post("/projects/sync", {"id": "a1", "name": "Ideas", "type": "cart", "folder": "Origen", "treeJson": tj})
    pdir = os.path.join(root, "Origen", "Ideas")
    os.makedirs(os.path.join(pdir, "source-versions"), exist_ok=True)
    os.makedirs(os.path.join(pdir, "tempFiles"), exist_ok=True)
    open(os.path.join(pdir, "source-versions", "v1.json"), "w").write("{}")
    open(os.path.join(pdir, "tempFiles", "foto.png"), "w").write("x")
    st, r = post("/projects/move", {"fromFolder": "Origen", "fromName": "Ideas", "toFolder": "Destino", "toName": "Ideas"})
    dst = os.path.join(root, "Destino", "Ideas")
    check("200 y moved: el directorio está en la carpeta nueva", st == 200 and r.get("moved") and os.path.isdir(dst), f"{st} {r}")
    check("…con su historial de versiones y sus adjuntos (no solo el tree.json)",
          os.path.exists(os.path.join(dst, "source-versions", "v1.json")) and os.path.exists(os.path.join(dst, "tempFiles", "foto.png")))
    check("…y ya no está en la vieja", not os.path.exists(pdir))
    # lo que hace la web después: sync + manifiesto (el manifiesto poda lo que la carpeta no tiene)
    post("/projects/sync", {"id": "a1", "name": "Ideas", "type": "cart", "folder": "Destino", "treeJson": tj})
    post("/projects/manifest", {"folders": [{"name": "Origen", "projects": []}, {"name": "Destino", "projects": [{"id": "a1", "name": "Ideas", "type": "cart"}]}]})
    check("el sync y el manifiesto que siguen no lo borran", os.path.exists(os.path.join(dst, "source-versions", "v1.json")))

    print("\n### nunca pisa, y no se sale de la carpeta de proyectos")
    post("/projects/sync", {"id": "b1", "name": "Ideas", "type": "cart", "folder": "Origen", "treeJson": tj})
    st, r = post("/projects/move", {"fromFolder": "Origen", "fromName": "Ideas", "toFolder": "Destino", "toName": "Ideas"})
    check("si en el destino ya hay uno con ese nombre: 409, y los dos quedan como estaban",
          st == 409 and os.path.isdir(os.path.join(root, "Origen", "Ideas")) and os.path.exists(os.path.join(dst, "source-versions", "v1.json")), f"{st} {r}")
    st, r = post("/projects/move", {"fromFolder": "Origen", "fromName": "No existe", "toFolder": "Destino", "toName": "X"})
    check("lo que nunca se guardó en disco: 200 sin mover (el sync lo crea)", st == 200 and r.get("moved") is False, f"{st} {r}")
    st, r = post("/projects/move", {"fromFolder": "Origen", "fromName": "Ideas", "toFolder": "../../afuera", "toName": "../x"})
    afuera = os.path.realpath(os.path.join(root, "..", "..", "afuera"))
    check("un nombre con ../ no sale de la carpeta de proyectos (safe_name)",
          not os.path.exists(afuera) and all(os.path.realpath(os.path.join(root, d)).startswith(os.path.realpath(root)) for d in os.listdir(root)), f"{st} {r}")
    st, r = post("/projects/move", {"fromFolder": "Origen"})
    check("sin nombres: 400", st == 400, f"{st} {r}")
finally:
    srv.terminate()

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
