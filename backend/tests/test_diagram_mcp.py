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

    print("\n### F. el interruptor y los niveles (doc 37 §F19)")

    def politica(**patch):
        """POST /mcp/policy → la política resultante, leída del propio backend."""
        req = urllib.request.Request(
            f"{base}/mcp/policy?token={token}", data=json.dumps(patch).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def lista():
        return sorted(t["name"] for t in (mcp.pedir("tools/list").get("result") or {}).get("tools", []))

    pol = json.loads(urllib.request.urlopen(f"{base}/mcp/policy?token={token}").read())
    check("por default está PRENDIDO y en diagramas (no rompe a quien ya lo usaba)",
          pol["enabled"] is True and pol["mode"] == "diagrams", json.dumps(pol))

    # Apagarlo tiene que valer EN VIVO: el cliente MCP ya está conectado y con token.
    politica(enabled=False)
    check("apagado, tools/list queda vacía sin reiniciar nada", lista() == [], str(lista()))
    txt, err = mcp.tool("read_diagram", {"name": "Mapa"})
    check("y una tool llamada igual se rechaza", err and "turned off" in txt, txt[:140])
    check("el rechazo dice DÓNDE prenderlo", "Settings" in txt, txt[:140])

    politica(enabled=True)
    check("al prenderlo vuelven las 4 de diagramas", lista() == sorted(
        ["diagram_schema", "list_diagrams", "read_diagram", "write_diagram"]), str(lista()))
    txt, err = mcp.tool("read_diagram", {"name": "Mapa"})
    check("…y vuelve a funcionar", not err, txt[:100])

    # Subir de nivel SIN carpeta no puede abrir el disco entero.
    pol = politica(mode="files")
    check("pedir 'files' sin carpeta NO abre archivos: cae a diagramas",
          pol["mode"] == "diagrams", json.dumps(pol))

    try:
        politica(root=os.path.join(home, "no-existe-esta-carpeta"))
        check("una carpeta inexistente se rechaza", False, "no tiró 400")
    except urllib.error.HTTPError as e:
        check("una carpeta inexistente se rechaza con 400", e.code == 400, str(e.code))

    codigo = os.path.join(home, "codigo")
    os.makedirs(codigo, exist_ok=True)
    pol = politica(mode="files", root=codigo)
    check("con carpeta elegida, 'files' sí queda", pol["mode"] == "files", json.dumps(pol))
    check("y aparecen las tools de archivos", "fs_read" in lista() and "fs_write" in lista(), str(lista()))
    check("pero NO la de ejecutar comandos", "fs_exec" not in lista(), str(lista()))
    txt, err = mcp.tool("fs_exec", {"cmd": "echo hola"})
    check("llamar fs_exec en nivel 'files' se rechaza", err and "shell" in txt, txt[:160])

    pol = politica(mode="shell")
    check("en 'shell' sí aparece fs_exec", pol["mode"] == "shell" and "fs_exec" in lista(), str(lista()))
    check("y las de diagramas siguen estando (los niveles son acumulativos)",
          "write_diagram" in lista(), str(lista()))

    # La política se PERSISTE: no vive solo en memoria del proceso.
    cfg = json.load(open(os.path.join(os.path.dirname(root), "config.json"), encoding="utf-8"))
    check("queda guardada en config.json", (cfg.get("mcp") or {}).get("mode") == "shell",
          json.dumps(cfg.get("mcp")))

    print("\n### G. las tools de archivos, de verdad y confinadas")
    politica(mode="files", root=codigo)
    with open(os.path.join(codigo, "hola.py"), "w", encoding="utf-8") as f:
        f.write("print('uno')\n")

    txt, err = mcp.tool("fs_tree", {})
    check("fs_tree ve la carpeta elegida", not err and "hola.py" in txt, txt[:160])
    txt, err = mcp.tool("fs_read", {"path": "hola.py"})
    check("fs_read trae el contenido real", not err and "print('uno')" in txt, txt[:160])
    txt, err = mcp.tool("fs_write", {"path": "sub/nuevo.txt", "content": "creado por el agente"})
    check("fs_write crea el archivo (y los directorios del medio)", not err, txt[:160])
    check("…y está en el DISCO, donde el usuario lo ve",
          open(os.path.join(codigo, "sub", "nuevo.txt"), encoding="utf-8").read() == "creado por el agente")
    txt, err = mcp.tool("fs_edit", {"path": "hola.py", "old": "uno", "new": "dos"})
    check("fs_edit reemplaza el texto exacto", not err, txt[:160])
    check("…y el archivo quedó cambiado",
          "print('dos')" in open(os.path.join(codigo, "hola.py"), encoding="utf-8").read())
    txt, err = mcp.tool("fs_grep", {"q": "dos"})
    check("fs_grep encuentra dentro de la carpeta", not err and "hola.py" in txt, txt[:160])

    # Lo que de verdad importa: que NO se pueda salir de la carpeta elegida.
    afuera = os.path.join(home, "secreto.txt")
    with open(afuera, "w", encoding="utf-8") as f:
        f.write("esto NO lo puede leer el agente")
    txt, err = mcp.tool("fs_read", {"path": "../secreto.txt"})
    check("un `..` no saca al agente de la carpeta", err, txt[:160])
    txt, err = mcp.tool("fs_read", {"path": afuera})
    check("una ruta ABSOLUTA de afuera tampoco", err, txt[:160])
    txt, err = mcp.tool("fs_write", {"path": "../colado.txt", "content": "x"})
    check("y tampoco se puede ESCRIBIR afuera", err, txt[:160])
    check("…nada se creó afuera", not os.path.exists(os.path.join(home, "colado.txt")))

    # El portero es del SERVIDOR: se prueba por la RED, sin pasar por el MCP.
    politica(mode="diagrams")
    req = urllib.request.Request(
        f"{base}/fs/write?token={token}",
        data=json.dumps({"projectId": "__mcp__", "path": "x.txt", "content": "x"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        check("bajar el nivel bloquea /fs por la RED, no solo en el MCP", False, "dejó escribir")
    except urllib.error.HTTPError as e:
        check("bajar el nivel bloquea /fs por la RED, no solo en el MCP", e.code == 403, str(e.code))
    check("…y el archivo no se creó", not os.path.exists(os.path.join(codigo, "x.txt")))

    mcp.cerrar()

    print("\n### G2. la config que la UI muestra es la MISMA que imprime el flag")
    cfg_http = json.loads(urllib.request.urlopen(f"{base}/mcp/config?token={token}").read())
    srv_cfg = cfg_http.get("config", {}).get("mcpServers", {}).get("diagraminder", {})
    check("/mcp/config devuelve un mcpServers armado", bool(srv_cfg), json.dumps(cfg_http)[:140])
    check("…con la dirección REAL del backend (el puerto que está usando)",
          srv_cfg.get("env", {}).get("DMD_URL") == base, srv_cfg.get("env", {}).get("DMD_URL"))
    check("…y con el token, que es lo que no se puede adivinar",
          srv_cfg.get("env", {}).get("DMD_TOKEN") == token)
    # Y que el flag imprima lo mismo: si la pantalla y el comando divergen, alguien
    # copia el que no anda y no hay forma de saber cuál era.
    salida = subprocess.run([sys.executable, SERVER, "--port", str(port), "--mcp-config"],
                            capture_output=True, text=True, env=env, timeout=30)
    try:
        cfg_flag = json.loads(salida.stdout)
    except Exception:
        cfg_flag = {}
    check("`--mcp-config` imprime la MISMA forma que la UI",
          list((cfg_flag.get("mcpServers") or {}).keys()) == ["diagraminder"], salida.stdout[:140])
    check("…y con el mismo token",
          (cfg_flag.get("mcpServers", {}).get("diagraminder", {})
           .get("env", {}).get("DMD_TOKEN")) == token)

    print("\n### H. el MCP REMOTO: OAuth y HTTP (doc 37 §F19)")
    import base64 as _b64, hashlib as _hh, urllib.parse as _up

    def http(metodo, ruta, cuerpo=None, headers=None, form=False):
        datos = None
        h = dict(headers or {})
        if cuerpo is not None:
            if form:
                datos = _up.urlencode(cuerpo).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                datos = json.dumps(cuerpo).encode()
                h["Content-Type"] = "application/json"
        r = urllib.request.Request(base + ruta, data=datos, headers=h, method=metodo)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw or "{}"), dict(resp.headers)
                except Exception:
                    return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw or "{}"), dict(e.headers)
            except Exception:
                return e.code, raw, dict(e.headers)

    # 1) discovery: sin token, porque es justo lo que se usa para saber cómo autenticarse
    st, meta, _ = http("GET", "/.well-known/oauth-protected-resource")
    check("la metadata del recurso se sirve SIN token", st == 200 and meta.get("resource", "").endswith("/mcp"),
          f"{st} {meta}")
    st, asm, _ = http("GET", "/.well-known/oauth-authorization-server")
    check("la del servidor de autorización también", st == 200 and asm.get("token_endpoint", "").endswith("/oauth/token"))
    check("y exige PKCE S256", asm.get("code_challenge_methods_supported") == ["S256"], str(asm.get("code_challenge_methods_supported")))

    # 2) el MCP remoto arranca APAGADO: exponer la máquina no puede ser un default
    st, r, h = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"Authorization": "Bearer " + token})
    check("con `remote` apagado, /mcp rechaza aunque el bearer sea válido", st == 403, f"{st} {r}")

    urllib.request.urlopen(urllib.request.Request(
        f"{base}/mcp/policy?token={token}", data=json.dumps({"remote": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()

    # 3) sin bearer → 401 y el WWW-Authenticate que dice dónde está la metadata
    st, r, h = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check("sin bearer, /mcp da 401", st == 401, str(st))
    check("…y el 401 dice DÓNDE está la metadata (si no, el cliente no puede arrancar)",
          "resource_metadata" in (h.get("WWW-Authenticate") or ""), h.get("WWW-Authenticate", ""))
    st, r, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"Authorization": "Bearer no-es-el-token"})
    check("un bearer inventado tampoco entra", st == 401, str(st))

    # 4) el flujo OAuth entero, como lo haría Claude web
    st, cli, _ = http("POST", "/oauth/register",
                      {"redirect_uris": ["http://127.0.0.1:9/cb"], "client_name": "Claude test"})
    check("el cliente se registra solo (no hay id que copiar a mano)", st == 201 and cli.get("client_id"), f"{st} {cli}")
    verifier = "un-verifier-larguito-de-prueba-123456"
    challenge = _b64.urlsafe_b64encode(_hh.sha256(verifier.encode()).digest()).decode().rstrip("=")
    q = {"client_id": cli["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
         "response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
         "state": "xyz"}
    st, pagina, _ = http("GET", "/oauth/authorize?" + _up.urlencode(q))
    check("la pantalla de consentimiento aparece", st == 200 and "passphrase" in str(pagina), str(st))
    check("…y dice QUÉ va a poder hacer, no solo pide una clave",
          "diagramas" in str(pagina).lower(), str(pagina)[:200])

    st, cuerpo, _ = http("POST", "/oauth/authorize", dict(q, passphrase="incorrecta"), form=True)
    check("con la contraseña equivocada no da código", st == 401, str(st))

    # el redirect lleva el code: urlopen lo seguiría, así que se lee el Location a mano
    import http.client as _hc
    u = _up.urlparse(base)
    conn = _hc.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("POST", "/oauth/authorize", _up.urlencode(dict(q, passphrase=token)),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    loc = resp.getheader("Location") or ""
    resp.read(); conn.close()
    check("con la contraseña correcta redirige con el code", resp.status == 302 and "code=" in loc, f"{resp.status} {loc}")
    check("y devuelve el state y el iss (RFC 9207)", "state=xyz" in loc and "iss=" in loc, loc)
    code = _up.parse_qs(_up.urlparse(loc).query)["code"][0]

    st, tok, _ = http("POST", "/oauth/token",
                      {"grant_type": "authorization_code", "code": code,
                       "client_id": cli["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
                       "code_verifier": "el-verifier-equivocado"}, form=True)
    check("un code_verifier que no corresponde NO canjea (PKCE)", st == 400, f"{st} {tok}")

    conn = _hc.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("POST", "/oauth/authorize", _up.urlencode(dict(q, passphrase=token)),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse(); loc = resp.getheader("Location") or ""; resp.read(); conn.close()
    code = _up.parse_qs(_up.urlparse(loc).query)["code"][0]
    st, tok, _ = http("POST", "/oauth/token",
                      {"grant_type": "authorization_code", "code": code,
                       "client_id": cli["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
                       "code_verifier": verifier}, form=True)
    check("con el verifier correcto sí emite un access token", st == 200 and tok.get("access_token"), f"{st} {tok}")
    at = tok["access_token"]

    st, tok2, _ = http("POST", "/oauth/token",
                       {"grant_type": "authorization_code", "code": code,
                        "client_id": cli["client_id"], "redirect_uri": "http://127.0.0.1:9/cb",
                        "code_verifier": verifier}, form=True)
    check("el code es de UN SOLO uso", st == 400, f"{st} {tok2}")

    # 5) el MCP por HTTP, con el token recién emitido
    st, r, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05"}},
                    {"Authorization": "Bearer " + at})
    check("initialize por HTTP responde", st == 200 and
          (r.get("result") or {}).get("serverInfo", {}).get("name") == "diagraminder", f"{st} {r}")
    st, r, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"Authorization": "Bearer " + at})
    nombres = sorted(t["name"] for t in (r.get("result") or {}).get("tools", []))
    check("tools/list por HTTP da las mismas tools que por stdio",
          nombres == sorted(["diagram_schema", "list_diagrams", "read_diagram", "write_diagram"]), str(nombres))
    st, r, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "list_diagrams", "arguments": {}}},
                    {"Authorization": "Bearer " + at})
    texto = ((r.get("result") or {}).get("content") or [{}])[0].get("text", "")
    check("y una tool de verdad corre por el túnel", st == 200 and "Mapa" in texto, texto[:140])

    # 6) la MISMA política manda en el remoto: no hay una puerta de atrás
    urllib.request.urlopen(urllib.request.Request(
        f"{base}/mcp/policy?token={token}", data=json.dumps({"enabled": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()
    st, r, _ = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                    {"Authorization": "Bearer " + at})
    check("apagar el MCP también vacía la lista del REMOTO",
          (r.get("result") or {}).get("tools") == [], json.dumps(r)[:140])

    # 7) un GET a /mcp no es 404: se explica
    st, r, _ = http("GET", "/mcp")
    check("GET /mcp contesta 405 con sentido, no 404", st == 405, str(st))

finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
