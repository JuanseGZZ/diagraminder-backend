"""El túnel que saca el MCP de esta máquina (doc 37 §F19).

Claude **web** no puede hablarle a `127.0.0.1`: corre en los servidores de Anthropic,
no en tu compu. Para que lea tus diagramas hace falta una URL pública que entre acá —
y eso es un túnel.

Se usa `cloudflared` con un túnel efímero (`--url`): no pide cuenta, no pide dominio y
la URL muere con el proceso. Esa caducidad **es la feature**: una URL que sobrevive al
programa es una puerta abierta que nadie recuerda haber dejado.

Lo que NO hace, a propósito:

- **No instala nada.** Si no hay `cloudflared`, se dice dónde bajarlo y listo. Bajar y
  ejecutar un binario de internet a espaldas del usuario es exactamente lo que este
  programa promete no hacer.
- **No abre el túnel solo.** Se prende a mano en Ajustes, igual que el MCP. Exponer tu
  máquina no puede ser un default.

La autenticación de lo que entra por el túnel NO vive acá: la hace `mcp_oauth`. El
túnel es un caño; el portero está del otro lado.
"""
import procs
import os
import re
import shutil
import subprocess
import threading
import time

# La URL que cloudflared imprime al levantar el túnel efímero.
_RE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

DESCARGA = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"

_estado = {"on": False, "url": "", "error": "", "desde": 0}
_proc = None
_lock = threading.Lock()


def disponible():
    """La ruta de cloudflared, o "" si no está."""
    return shutil.which("cloudflared") or ""


def comando_instalar():
    """El comando concreto para ESTE sistema. Un link a una página de descargas hace
    que la persona tenga que averiguar cuál de los seis archivos le toca; un comando
    que se copia y se pega, no."""
    import platform
    so = platform.system()
    if so == "Darwin":
        return "brew install cloudflared"
    if so == "Windows":
        return "winget install --id Cloudflare.cloudflared"
    return "sudo apt install cloudflared   # o el binario de la página de Cloudflare"


def estado():
    d = dict(_estado)
    exe = disponible()
    d["installed"] = bool(exe)
    d["path"] = exe
    d["download"] = DESCARGA
    d["install"] = comando_instalar()
    d["version"] = _version(exe) if exe else ""
    if d["on"] and d["desde"]:
        d["uptime"] = int(time.time() - d["desde"])
    return d


def _version(exe):
    try:
        out = procs.run([exe, "--version"], capture_output=True, text=True, timeout=6)
        return (out.stdout or out.stderr or "").strip().splitlines()[0][:60]
    except Exception:
        return ""


def _leer_salida(p):
    """cloudflared escribe la URL en stderr, entre mucho ruido. Se lee en un hilo
    porque si nadie vacía el pipe, el proceso se traba cuando el buffer se llena
    (la misma mordida que documenta la bitácora en los runs del orquestador)."""
    try:
        for linea in iter(p.stderr.readline, ""):
            if not linea:
                break
            m = _RE_URL.search(linea)
            if m and not _estado["url"]:
                _estado["url"] = m.group(0)
    except Exception:
        pass


def abrir(timeout=25):
    """Levanta el túnel. (ok, url_o_error)."""
    global _proc
    with _lock:
        if _estado["on"] and _estado["url"]:
            return True, _estado["url"]
        exe = disponible()
        if not exe:
            _estado["error"] = ("cloudflared no está instalado. Bajalo de "
                                f"{DESCARGA} y volvé a probar.")
            return False, _estado["error"]
        _estado.update(on=False, url="", error="", desde=0)
        try:
            _proc = procs.popen(
                [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{_puerto()}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
        except Exception as e:
            _estado["error"] = f"no pude lanzar cloudflared: {e}"
            return False, _estado["error"]
        threading.Thread(target=_leer_salida, args=(_proc,), daemon=True).start()
        limite = time.time() + timeout
        while time.time() < limite:
            if _estado["url"]:
                _estado.update(on=True, desde=time.time())
                return True, _estado["url"]
            if _proc.poll() is not None:
                _estado["error"] = "cloudflared se cerró antes de dar una URL"
                return False, _estado["error"]
            time.sleep(0.25)
        cerrar()
        _estado["error"] = f"cloudflared no dio una URL en {timeout}s"
        return False, _estado["error"]


def cerrar():
    """Baja el túnel. La URL deja de existir en el momento."""
    global _proc
    with _lock:
        if _proc is not None:
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
            _proc = None
        _estado.update(on=False, url="", desde=0)
    return True


# El puerto lo fija server.py al arrancar: este módulo no lo puede adivinar y no debe
# importar server.py (sería un ciclo).
_PUERTO = 8765


def set_puerto(p):
    global _PUERTO
    _PUERTO = int(p)


def _puerto():
    return _PUERTO
