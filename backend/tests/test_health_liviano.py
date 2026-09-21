"""`/health` tiene que ser BARATO (bug 2026-09-21).

Es un chequeo de vida: la web lo llama cada 5 segundos desde `localBackend.js`. Tenía
adentro la detección de los CLIs, o sea **un subproceso por cada CLI en cada
request**. En macOS solo se notaba como lentitud; en **Windows** cada subproceso de
una app sin consola ABRE UNA VENTANA, así que la consola aparecía y desaparecía sin
parar — y, peor, `/health` tardaba más que el timeout de 2,5s del latido, así que la
app de escritorio **nunca terminaba de conectarse**.

Este test mide lo único que importa: que `/health` responda rápido y **sin lanzar
procesos**. El conteo de procesos se hace de verdad, mirando los hijos del backend.

    python3 backend/tests/test_health_liviano.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SERVER = os.path.join(BACKEND, "server.py")

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


def hijos(pid):
    """Cuántos procesos hijo tiene el backend ahora mismo (sin dependencias: ps)."""
    try:
        out = subprocess.run(["ps", "-o", "ppid="], capture_output=True, text=True, timeout=10)
        return sum(1 for l in out.stdout.split() if l.strip() == str(pid))
    except Exception:
        return -1


home = tempfile.mkdtemp(prefix="dmhealth-")
port = puerto_libre()

# Un `claude` SEÑUELO al principio del PATH: cada vez que alguien lo ejecuta deja una
# línea en un archivo. Así las invocaciones se CUENTAN, en vez de inferirlas del
# reloj — que es lo que hacía que este test pasara con el bug puesto (en esta máquina
# hay pocos CLIs instalados y el tiempo quedaba justo por debajo del umbral).
bindir = os.path.join(home, "bin")
os.makedirs(bindir, exist_ok=True)
CONTADOR = os.path.join(home, "invocaciones.txt")
senuelo = os.path.join(bindir, "claude")
with open(senuelo, "w", encoding="utf-8") as f:
    f.write("#!/bin/sh\necho x >> %s\necho '1.0.0-senuelo'\n" % CONTADOR)
os.chmod(senuelo, 0o755)


def invocaciones():
    try:
        with open(CONTADOR, encoding="utf-8") as f:
            return len(f.read().split())
    except OSError:
        return 0


env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home,
           PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
base = f"http://127.0.0.1:{port}"
try:
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read()
            break
        except Exception:
            time.sleep(0.25)

    print("\n### A. responde y trae lo que la web necesita")
    d = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
    check("contesta ok", d.get("status") == "ok", json.dumps(d)[:120])
    check("dice su versión", bool(d.get("version")))
    check("trae la lista de CLIs (aunque esté vacía al arrancar)",
          isinstance(d.get("clis"), list), str(type(d.get("clis"))))
    check("y el campo `claude` de compat", isinstance(d.get("claude"), dict))

    # Que la detección EXISTA: en algún momento el refresco de fondo la completa.
    for _ in range(40):
        d = json.loads(urllib.request.urlopen(base + "/health", timeout=5).read())
        if d.get("clis"):
            break
        time.sleep(0.25)
    check("el refresco de fondo termina llenando la lista", bool(d.get("clis")),
          json.dumps(d.get("clis"))[:120])

    print("\n### B. y es BARATO: NINGÚN subproceso por request")
    check("el señuelo se ejecutó alguna vez (si no, no estamos midiendo nada)",
          invocaciones() > 0, f"{invocaciones()} invocaciones")

    # Lo que de verdad importa, y medido contando, no cronometrando: 20 /health
    # seguidos NO pueden ejecutar el binario ni una sola vez. Con la detección
    # adentro eran 20 invocaciones (una por request) y, en Windows, 20 consolas.
    time.sleep(0.6)                       # que termine cualquier refresco de fondo
    antes_inv = invocaciones()
    t0 = time.time()
    for _ in range(20):
        urllib.request.urlopen(base + "/health", timeout=5).read()
    tardo = time.time() - t0
    check("20 /health NO ejecutan el binario de ningún CLI",
          invocaciones() == antes_inv, f"{antes_inv} → {invocaciones()}")
    check("…y tardan menos de 1s en total", tardo < 1.0, f"{tardo:.2f}s")

    # Y que no queden procesos colgando: el latido corre para siempre, un hijo por
    # request se acumularía hasta quedarse sin descriptores.
    antes = hijos(srv.pid)
    for _ in range(20):
        urllib.request.urlopen(base + "/health", timeout=5).read()
    time.sleep(0.5)
    despues = hijos(srv.pid)
    check("no deja procesos hijos colgando", despues <= max(antes, 0) + 1,
          f"{antes} → {despues}")

    print("\n### C. el panel SÍ puede pagar la detección (es una pantalla, no un latido)")
    tok = ""
    for r, _, fs in os.walk(home):
        if "token.txt" in fs:
            tok = open(os.path.join(r, "token.txt")).read().strip()
            break
    st = json.loads(urllib.request.urlopen(f"{base}/panel/status?token={tok}", timeout=30).read())
    check("/panel/status trae los CLIs con su plan de instalación",
          isinstance(st.get("clis"), list) and len(st["clis"]) >= 1 and
          "install" in st["clis"][0], json.dumps(st.get("clis"))[:140])
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
