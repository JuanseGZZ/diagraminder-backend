"""La bandeja de entrada de la biblioteca (2026-10-08): el agente CREA documentos.

Antes un agente no podía agregar nada a un proyecto `documents` (la skill le decía
«que lo suba el usuario»). Ahora deja el archivo en `documents/inbox/` y el backend lo
ingiere: blob por hash, entrada en el manifiesto, y el watcher se lo emite a la web.

Contra el backend REAL (HOME temporal, puerto libre), sin red.

    python3 backend/tests/test_docs_inbox.py
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SERVER = os.path.join(BACKEND, "server.py")
sys.path.insert(0, BACKEND)

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


def escribir(path, data, viejo=True):
    """Un archivo como lo deja un agente. `viejo` = ya terminó de escribirse (mtime de
    hace unos segundos); si no, el ingest tiene que esperarlo."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if viejo:
        t = time.time() - 5
        os.utime(path, (t, t))


def esperar(cond, segundos=6):
    fin = time.time() + segundos
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


home = tempfile.mkdtemp(prefix="dminbox-")
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
    token = ""
    for raiz, _, archivos in os.walk(home):
        if "token.txt" in archivos:
            token = open(os.path.join(raiz, "token.txt")).read().strip()
    root = json.loads(urllib.request.urlopen(f"{base}/config?token={token}").read())["root"]

    # una biblioteca con un documento del usuario, y un canvas (que NO tiene bandeja)
    local = os.path.join(root, "Local")
    lib = os.path.join(local, "Biblio")
    can = os.path.join(local, "Canvas")
    os.makedirs(lib)
    os.makedirs(can)
    with open(os.path.join(local, "index.json"), "w") as f:
        json.dump({"projects": [{"id": "dLIB", "name": "Biblio", "type": "documents"},
                                {"id": "dCAN", "name": "Canvas", "type": "freestyle"}]}, f)
    previo = b"lo que subio el usuario"
    hp = hashlib.sha256(previo).hexdigest()
    os.makedirs(os.path.join(lib, "documents"))
    with open(os.path.join(lib, "documents", hp), "wb") as f:
        f.write(previo)
    manifest0 = {"type": "documents", "lastId": 1, "dirs": [],
                 "docs": [{"id": 1, "name": "resumen.md", "mime": "text/markdown", "size": len(previo),
                           "hash": hp, "ts": 1, "dir": ""}]}
    with open(os.path.join(lib, "tree.json"), "w") as f:
        json.dump(manifest0, f)
    with open(os.path.join(can, "tree.json"), "w") as f:
        json.dump({"type": "freestyle", "nodos": []}, f)
    time.sleep(1.2)                                   # que el watcher los dé por vistos

    eventos = []

    def escuchar():
        try:
            with urllib.request.urlopen(f"{base}/state/stream?since=0&token={token}", timeout=25) as r:
                for linea in r:
                    linea = linea.decode().strip()
                    if linea.startswith("data:"):
                        try:
                            eventos.append(json.loads(linea[5:].strip()))
                        except Exception:
                            pass
        except Exception:
            pass

    threading.Thread(target=escuchar, daemon=True).start()
    time.sleep(0.6)

    def manifest():
        with open(os.path.join(lib, "tree.json"), encoding="utf-8") as f:
            return json.load(f)

    print("\n### A. un archivo en la bandeja entra a la biblioteca")
    texto = b"# Resumen\n\nlo que hizo el agente\n"
    h = hashlib.sha256(texto).hexdigest()
    inbox = os.path.join(lib, "documents", "inbox")
    escribir(os.path.join(inbox, "resumen.md"), texto)
    check("el watcher lo ingiere solo", esperar(lambda: len(manifest()["docs"]) == 2), json.dumps(manifest())[:200])
    m = manifest()
    nuevo = m["docs"][-1] if len(m["docs"]) == 2 else {}
    check("el blob queda guardado por su hash", os.path.isfile(os.path.join(lib, "documents", h)))
    check("con el esquema EXACTO de la web (id, name, mime, size, hash, ts, dir)",
          set(nuevo) == {"id", "name", "mime", "size", "hash", "ts", "dir"} and nuevo["hash"] == h
          and nuevo["size"] == len(texto) and nuevo["mime"] == "text/markdown" and nuevo["id"] == 2
          and m["lastId"] == 2, json.dumps(nuevo))
    check("el nombre ya usado NO pisa el del usuario: «resumen (2).md»", nuevo.get("name") == "resumen (2).md", nuevo.get("name"))
    check("el documento del usuario sigue intacto", m["docs"][0] == manifest0["docs"][0])
    check("la bandeja queda vacía", os.listdir(inbox) == [], str(os.listdir(inbox)))
    check("y aparece en la vista legible by-name/ (así el agente verifica)",
          os.path.isfile(os.path.join(lib, "documents", "by-name", "resumen (2).md")))
    check("la web se ENTERA: el cambio se emite por SSE con el doc nuevo",
          esperar(lambda: any(e.get("id") == "dLIB" and h in (e.get("treeJson") or "") for e in eventos), 4),
          f"{len(eventos)} eventos")

    print("\n### B. carpetas virtuales")
    escribir(os.path.join(inbox, "papers", "2026", "tabla.csv"), b"a,b\n1,2\n")
    check("una subcarpeta de la bandeja es una carpeta virtual",
          esperar(lambda: any(d.get("name") == "tabla.csv" for d in manifest()["docs"])))
    m = manifest()
    d = next((x for x in m["docs"] if x.get("name") == "tabla.csv"), {})
    check("con su dir y TODOS sus prefijos en `dirs` (como los lista la web)",
          d.get("dir") == "papers/2026" and "papers" in m["dirs"] and "papers/2026" in m["dirs"], json.dumps(m["dirs"]))
    check("la subcarpeta vacía se limpia", not os.path.exists(os.path.join(inbox, "papers")))

    print("\n### C. lo que NO se ingiere (todavía, o nunca)")
    escribir(os.path.join(inbox, "a-medias.txt"), b"todavia escribiendo", viejo=False)
    escribir(os.path.join(inbox, ".DS_Store"), b"x")
    time.sleep(0.3)
    check("un archivo recién escrito espera (podría estar a medio escribir)",
          os.path.exists(os.path.join(inbox, "a-medias.txt"))
          and not any(x.get("name") == "a-medias.txt" for x in manifest()["docs"]))
    check("…y entra en cuanto se asienta",
          esperar(lambda: any(x.get("name") == "a-medias.txt" for x in manifest()["docs"]), 5))
    check("los archivos ocultos del sistema se ignoran",
          not any(x.get("name") == ".DS_Store" for x in manifest()["docs"]))
    grande = os.path.join(inbox, "enorme.bin")
    with open(grande, "wb") as f:
        f.truncate(101 * 1024 * 1024)                 # disperso: no ocupa disco de verdad
    t = time.time() - 5
    os.utime(grande, (t, t))
    check("uno de más de 100 MB no entra y deja una nota que lo explica",
          esperar(lambda: os.path.exists(grande + ".TOO-BIG.txt"))
          and not any(x.get("name") == "enorme.bin" for x in manifest()["docs"]))
    os.remove(grande)
    os.remove(grande + ".TOO-BIG.txt")
    escribir(os.path.join(can, "documents", "inbox", "x.md"), b"no soy una biblioteca")
    time.sleep(1.5)
    check("en un proyecto que NO es documents la bandeja no se toca",
          os.path.exists(os.path.join(can, "documents", "inbox", "x.md"))
          and json.load(open(os.path.join(can, "tree.json")))["type"] == "freestyle")

    print("\n### D. la poda de la web no se come lo recién ingerido")
    # La web poda con SU manifiesto. Si sincroniza justo antes de enterarse del nuevo,
    # manda un keep sin ese hash: el blob no se puede ir.
    req = urllib.request.Request(f"{base}/docs/gc?token={token}", method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps({"projectId": "dLIB", "keep": [hp]}).encode())
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    check("un gc con el manifiesto VIEJO no borra el blob nuevo (gracia de 60 s)",
          os.path.isfile(os.path.join(lib, "documents", h)) and h not in (resp.get("removed") or []),
          json.dumps(resp)[:200])

    print("\n### E. la skill se lo cuenta al agente")
    import skills
    s = skills.SKILLS["diagramind-documents"]
    check("la skill de documents explica la bandeja (si no, el agente no sabe que existe)",
          "documents/inbox/" in s and "by-name" in s)
    check("y ya no le dice que no puede agregar archivos",
          "upload it themselves" not in s and "Do not edit the manifest to add" not in s)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
