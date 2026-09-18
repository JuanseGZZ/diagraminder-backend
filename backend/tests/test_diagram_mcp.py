"""El MCP de los DIAGRAMAS, contra el backend REAL (doc 37 §F18).

Levanta el server de verdad con un HOME temporal y un puerto libre, siembra un par de
diagramas en disco y habla JSON-RPC por stdio con `--mcp-diagrams`, igual que haría
Claude Code. No hay mocks: si el contrato con `/state` o `/state/write` cambia, esto
se cae.

    python3 backend/tests/test_diagram_mcp.py
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


class Mcp:
    """Cliente JSON-RPC mínimo por stdio."""

    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, SERVER, "--mcp-diagrams"],
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
        if "error" in r:                       # error de PROTOCOLO, no de la tool
            return f"[rpc error] {json.dumps(r['error'])}", True
        res = r.get("result") or {}
        return (res.get("content") or [{}])[0].get("text", ""), bool(res.get("isError"))

    def cerrar(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


home = tempfile.mkdtemp(prefix="dmmcp-")
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

    # token del backend (lo deja en su carpeta de datos)
    tokfile = None
    for raiz, _, archivos in os.walk(home):
        if "token.txt" in archivos:
            tokfile = os.path.join(raiz, "token.txt")
            break
    token = open(tokfile).read().strip() if tokfile else ""
    check("el backend levantó y dejó su token", bool(token))

    # --- sembrar dos diagramas en disco, como los deja el mirror ---
    root = json.loads(urllib.request.urlopen(f"{base}/config?token={token}").read())["root"]
    for carpeta, nombre, tipo in [("Local", "Mapa", "cart"), ("Local", "Pendientes", "activities")]:
        d = os.path.join(root, carpeta, nombre)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tree.json"), "w", encoding="utf-8") as f:
            json.dump({"type": tipo, "sembrado": True}, f)

    mcp = Mcp(dict(env, DMD_URL=base, DMD_TOKEN=token))

    print("\n### A. el handshake")
    r = mcp.pedir("initialize", {"protocolVersion": "2024-11-05"})
    check("initialize responde", (r.get("result") or {}).get("serverInfo", {}).get("name") == "diagraminder",
          json.dumps(r)[:120])
    tools = [t["name"] for t in (mcp.pedir("tools/list").get("result") or {}).get("tools", [])]
    check("declara las 4 tools",
          sorted(tools) == ["diagram_schema", "list_diagrams", "read_diagram", "write_diagram"], str(tools))

    print("\n### B. ver lo que hay")
    txt, err = mcp.tool("list_diagrams")
    if err or not txt.startswith("["):
        print("     (respuesta cruda:", txt[:300], ")")
    filas = json.loads(txt) if (not err and txt.startswith("[")) else []
    check("list_diagrams trae los dos sembrados", len(filas) == 2, txt[:160])
    check("y dice de qué tipo es cada uno",
          {f["name"]: f["type"] for f in filas} == {"Mapa": "cart", "Pendientes": "activities"}, txt[:160])

    txt, err = mcp.tool("read_diagram", {"name": "Mapa"})
    check("read_diagram devuelve el tree.json", not err and json.loads(txt).get("sembrado") is True, txt[:120])

    txt, err = mcp.tool("read_diagram", {"name": "NoExiste"})
    check("un nombre que no existe da error CON la lista de los que sí", err and "Mapa" in txt, txt[:160])

    print("\n### C. el esquema no se inventa")
    txt, err = mcp.tool("diagram_schema", {"type": "cart"})
    check("diagram_schema devuelve el de verdad (el de skills.py)", not err and len(txt) > 200, txt[:100])
    txt, err = mcp.tool("diagram_schema", {"type": "inventado"})
    check("y un tipo que no existe avisa cuáles hay", err and "cart" in txt, txt[:160])

    print("\n### D. escribir, que es el punto")
    nuevo = {"type": "cart", "escrito_por": "el agente"}
    txt, err = mcp.tool("write_diagram", {"name": "Mapa", "json": json.dumps(nuevo)})
    check("write_diagram responde OK", not err, txt[:160])
    en_disco = json.load(open(os.path.join(root, "Local", "Mapa", "tree.json"), encoding="utf-8"))
    check("y el archivo del disco quedó con lo nuevo", en_disco.get("escrito_por") == "el agente", str(en_disco)[:120])
    txt, _ = mcp.tool("read_diagram", {"name": "Mapa"})
    check("releerlo devuelve lo mismo que se escribió", json.loads(txt).get("escrito_por") == "el agente")

    print("\n### E. las guardas")
    txt, err = mcp.tool("write_diagram", {"name": "Mapa", "json": "{ esto no es json"})
    check("un JSON roto se rechaza y no toca el disco", err and "JSON" in txt, txt[:120])
    txt, err = mcp.tool("write_diagram", {"name": "Mapa", "json": json.dumps({"type": "activities"})})
    check("cambiar el TIPO del diagrama se rechaza", err and "cart" in txt, txt[:160])
    en_disco = json.load(open(os.path.join(root, "Local", "Mapa", "tree.json"), encoding="utf-8"))
    check("…y el disco siguió intacto después de los dos rechazos",
          en_disco.get("escrito_por") == "el agente", str(en_disco)[:120])
    txt, err = mcp.tool("write_diagram", {"name": "Mapa"})
    check("sin `json` avisa qué falta", err and "json" in txt.lower(), txt[:120])

    mcp.cerrar()
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
