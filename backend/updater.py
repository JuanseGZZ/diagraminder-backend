"""Avisar que hay una versión nueva, y aplicarla (doc 37 §F17).

Por qué se puede actualizar sin miedo: **los datos viven afuera del programa**. Los
proyectos, el orquestador y el token están en `app_dir()` (~/Library/Application
Support/DiagraMind y equivalentes), no adentro del binario. Reemplazar el ejecutable
no toca nada de eso — es la razón de que esto sea un swap de un archivo y no una
migración.

Qué NO hace, a propósito:
  - No actualiza solo. Pregunta. Cambiarle el programa a alguien mientras trabaja es
    exactamente lo que hace que la gente desactive las actualizaciones.
  - No verifica firma. Todavía no hay firma de código; lo que sí hace es **probar el
    binario nuevo antes de pisar el viejo** (ver `_probar`), que ataja el fallo real y
    frecuente: una descarga cortada.
  - En Windows no cambia el archivo: un .exe en ejecución no se puede reemplazar. Ahí
    devuelve la URL y que la persona lo baje — decirle "listo" y no haber hecho nada
    sería peor.
"""
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = "JuanseGZZ/diagraminder-app"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGINA = f"https://github.com/{REPO}/releases/latest"

_cache = {"t": 0, "data": None}
CACHE_S = 3600          # una consulta por hora alcanza: GitHub limita por IP


def _ctx():
    """Contexto TLS que funciona también donde Python no tiene store de
    certificados. Pasa de verdad: en las instalaciones de python.org en macOS,
    urlopen contra HTTPS falla con CERTIFICATE_VERIFY_FAILED hasta que alguien corre
    `Install Certificates.command`. Un updater que siempre dice "no pude consultar"
    es un updater que nadie usa, así que si hay `certifi` (lo trae PyInstaller en el
    binario) se usa ese bundle; si no, el default del sistema.
    Lo que NO se hace es desactivar la verificación: un canal sin verificar para
    BAJAR UN EJECUTABLE es exactamente el peor lugar donde ahorrarse eso."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _nombre_asset():
    if sys.platform == "darwin":
        return "DiagraMinder-mac"
    if os.name == "nt":
        return "DiagraMinder-win.exe"
    return "DiagraMinder-linux"


def _tupla(v):
    """'v1.2.3' → (1,2,3). Lo que no parsea va último, para no 'actualizar' a algo raro."""
    nums = []
    for parte in (v or "").lstrip("vV").split("."):
        d = "".join(c for c in parte if c.isdigit())
        nums.append(int(d) if d else 0)
    return tuple(nums or [0])


def check(actual, forzar=False):
    """{current, latest, hasUpdate, url, canApply} o {error}. Cacheado."""
    ahora = time.time()
    if not forzar and _cache["data"] and ahora - _cache["t"] < CACHE_S:
        ultimo = _cache["data"]
    else:
        try:
            req = urllib.request.Request(API, headers={
                "User-Agent": "DiagraMinder", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8, context=_ctx()) as r:
                ultimo = json.loads(r.read().decode())
            _cache["t"], _cache["data"] = ahora, ultimo
        except Exception as e:
            return {"error": f"no pude consultar las versiones ({e})"}

    tag = (ultimo.get("tag_name") or "").strip()
    url = None
    for a in ultimo.get("assets") or []:
        if a.get("name") == _nombre_asset():
            url = a.get("browser_download_url")
            break
    # Sin versión actual (modo desarrollo o backend solo) NO se ofrece nada: no hay
    # contra qué comparar, y avisar de una "actualización" ahí sería ruido.
    hay = bool(actual and tag and _tupla(tag) > _tupla(actual))
    return {"current": actual, "latest": tag, "hasUpdate": hay,
            "url": url or PAGINA, "page": PAGINA,
            "canApply": bool(hay and url and getattr(sys, "frozen", False) and os.name != "nt"),
            "notes": (ultimo.get("body") or "")[:600]}


def _probar(ruta):
    """¿El archivo que bajamos es un binario que arranca? Ataja la descarga cortada,
    que es el fallo de verdad. `--mcp-config` sirve de humo: no abre puertos ni
    ventanas, imprime y sale."""
    try:
        p = subprocess.run([ruta, "--mcp-config"], capture_output=True, timeout=45)
        return p.returncode == 0 and b"mcpServers" in p.stdout
    except Exception:
        return False


def apply(actual):
    """Baja, prueba y reemplaza el binario. Devuelve (ok, mensaje).
    El caller reinicia: acá NO se hace execv para que el server alcance a responder."""
    info = check(actual, forzar=True)
    if info.get("error"):
        return False, info["error"]
    if not info.get("hasUpdate"):
        return False, "ya estás en la última versión"
    if not getattr(sys, "frozen", False):
        return False, "esto solo aplica al ejecutable (en desarrollo, actualizá con git)"
    if os.name == "nt":
        return False, f"en Windows hay que bajarlo a mano (un .exe en uso no se puede reemplazar): {info['page']}"

    destino = os.path.realpath(sys.executable)
    tmp = destino + ".nuevo"
    try:
        with urllib.request.urlopen(urllib.request.Request(
                info["url"], headers={"User-Agent": "DiagraMinder"}), timeout=300, context=_ctx()) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception as e:
        _borrar(tmp)
        return False, f"falló la descarga: {e}"

    if not _probar(tmp):
        _borrar(tmp)
        return False, "el archivo descargado no arranca (descarga cortada). No se tocó nada."

    # El viejo se guarda al lado hasta que el nuevo ocupe su lugar: si el rename del
    # medio falla, queda algo a lo que volver en vez de ningún binario.
    viejo = destino + ".viejo"
    try:
        _borrar(viejo)
        os.replace(destino, viejo)
        os.replace(tmp, destino)
    except Exception as e:
        if os.path.exists(viejo) and not os.path.exists(destino):
            try:
                os.replace(viejo, destino)
            except Exception:
                pass
        _borrar(tmp)
        return False, f"no pude reemplazar el programa: {e}"
    _borrar(viejo)
    return True, f"actualizado a {info['latest']}. Cerrá y volvé a abrir DiagraMinder."


def _borrar(p):
    try:
        os.remove(p)
    except Exception:
        pass
