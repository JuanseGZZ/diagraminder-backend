"""El nodo GitHub del orquestador (doc 28 §GitHub): un repo como recurso de un agente.

Sin red: el "GitHub" es un remoto git EN DISCO (DMN_GH_GIT=file://…) y una API falsa
(DMN_GH_API) que registra lo que le piden. Contra el backend REAL (HOME temporal,
puerto libre):

  A. ghrepo puro: parsear el repo, qué tools habilita cada permiso
  B. el token: se guarda, nunca vuelve, nunca va a .git/config ni a una respuesta
  C. clonar, ramas, commit, push — y el push a la principal solo con «merge»
  D. la API: PRs, comentarios, merge (con y sin permiso), issues
  E. lo que ve el agente: tools API (Inspect), el MCP de las cabezas CLI, los flags
  F. el permiso se chequea en el SERVIDOR aunque se llame directo
  G. validate_graph conoce agFolder y agGithub (agFolder faltaba desde F3)

    python3 backend/tests/test_github_node.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
SERVER = os.path.join(BACKEND, "server.py")
sys.path.insert(0, BACKEND)

ok = fail = 0
TOKEN = "ghp_TESTtoken_1234567890abcd"


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


def git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, (r.stdout + r.stderr)


# ------------------------------------------------------------ el GitHub falso
LLAMADAS = []


class FakeGH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else None
        LLAMADAS.append({"method": method, "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"message": "Bad credentials"})
        p = self.path.split("?")[0]
        if p == "/repos/acme/app" and method == "GET":
            return self._send(200, {"full_name": "acme/app", "private": True, "default_branch": "main",
                                    "permissions": {"push": True, "admin": False}})
        if p == "/repos/acme/app/pulls" and method == "GET":
            return self._send(200, [{"number": 7, "title": "Feature X", "state": "open", "user": {"login": "bot"},
                                     "head": {"ref": "feature/x"}, "base": {"ref": "main"}, "html_url": "u7"}])
        if p == "/repos/acme/app/pulls" and method == "POST":
            return self._send(201, {"number": 7, "title": body["title"], "state": "open",
                                    "head": {"ref": body["head"]}, "base": {"ref": body["base"]}, "html_url": "u7"})
        if p == "/repos/acme/app/pulls/7" and method == "GET":
            return self._send(200, {"number": 7, "title": "Feature X", "state": "open", "body": "desc",
                                    "mergeable": True, "head": {"ref": "feature/x"}, "base": {"ref": "main"}})
        if p == "/repos/acme/app/pulls/7/files":
            return self._send(200, [{"filename": "hola.txt", "status": "added", "additions": 1, "deletions": 0}])
        if p == "/repos/acme/app/pulls/7/merge" and method == "PUT":
            return self._send(200, {"merged": True, "sha": "abc123", "message": "Pull Request successfully merged"})
        if p == "/repos/acme/app/issues/7/comments" and method == "POST":
            return self._send(201, {"html_url": "c1"})
        if p == "/repos/acme/app/issues" and method == "GET":
            return self._send(200, [{"number": 3, "title": "Bug", "state": "open", "user": {"login": "ana"}, "labels": []},
                                    {"number": 7, "title": "Feature X", "pull_request": {}, "state": "open"}])
        return self._send(404, {"message": "Not Found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


import ghrepo  # noqa: E402
import orchestrator  # noqa: E402

print("\n### A. ghrepo puro")
for txt, want in (("acme/app", "acme/app"), ("https://github.com/acme/app", "acme/app"),
                  ("https://github.com/acme/app.git", "acme/app"), ("git@github.com:acme/app.git", "acme/app"),
                  ("https://github.com/acme/app/tree/main/src", "acme/app"), ("acme", None), ("../x/y", None)):
    check(f"parse_repo({txt!r}) → {want}", ghrepo.parse_repo(txt) == want, str(ghrepo.parse_repo(txt)))
leer = ghrepo.allowed_tools("leer", False)
editar = ghrepo.allowed_tools("editar", False)
merge = ghrepo.allowed_tools("editar", True)
check("leer: ver, traer y leer PRs/issues — sin commit ni push",
      "git_pull" in leer and "gh_pr_view" in leer and "git_commit" not in leer and "git_push" not in leer)
check("editar: commit, push, PRs — sin merge", "git_push" in editar and "gh_pr_create" in editar and "gh_pr_merge" not in editar)
check("con merge: también gh_pr_merge", "gh_pr_merge" in merge)
check("merge sin editar (leer + merge) NO da merge: el merge pide escribir", "gh_pr_merge" not in ghrepo.allowed_tools("leer", True))

print("\n### G. validate_graph")
ctx0 = {"pid": "orch1", "project_meta": lambda p: None}
g_ok = {"type": "orchestrator", "nodos": [{"id": 1, "type": "agAgent"}, {"id": 2, "type": "agFolder"},
                                          {"id": 3, "type": "agGithub"}],
        "flechas": [{"fromId": 1, "toId": 2, "kind": "usa"}, {"fromId": 1, "toId": 3, "kind": "usa"}]}
check("un organigrama con agFolder y agGithub es válido (agFolder faltaba desde F3)",
      orchestrator.validate_graph(ctx0, g_ok) is None, str(orchestrator.validate_graph(ctx0, g_ok)))

# ------------------------------------------------------------ el remoto en disco
tmp = tempfile.mkdtemp(prefix="dmgh-")
remotes = os.path.join(tmp, "remotes")
bare = os.path.join(remotes, "acme", "app.git")
os.makedirs(bare)
git("init", "--bare", "-b", "main", bare)
seed = os.path.join(tmp, "seed")
git("clone", bare, seed)
with open(os.path.join(seed, "README.md"), "w") as f:
    f.write("# app\n")
git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=seed)
git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init", cwd=seed)
git("push", "origin", "HEAD:main", cwd=seed)

fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeGH)
threading.Thread(target=fake.serve_forever, daemon=True).start()

home = os.path.join(tmp, "home")
os.makedirs(home)
port = puerto_libre()
env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home,
           DMN_GH_API=f"http://127.0.0.1:{fake.server_address[1]}", DMN_GH_GIT=f"file://{remotes}")
srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
base = f"http://127.0.0.1:{port}"


def pedir(path, body=None, method=None):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{base}{path}{sep}token={tok}", method=method or ("POST" if body is not None else "GET"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    tok = ""
    for raiz, _, archivos in os.walk(home):
        if "token.txt" in archivos:
            tok = open(os.path.join(raiz, "token.txt")).read().strip()
    root = pedir("/config")[1]["root"]
    PID = "orch1"
    local = os.path.join(root, "Local")
    proj = os.path.join(local, "Empresa")
    os.makedirs(proj)
    with open(os.path.join(local, "index.json"), "w") as f:
        json.dump({"projects": [{"id": PID, "name": "Empresa", "type": "orchestrator"}]}, f)

    def grafo(perm="editar", merge=False, ia=None):
        tree = {"type": "orchestrator", "lastIdCharged": 2, "lastArrowId": 1, "formas": [], "nodos": [
            {"id": 1, "x": 0, "y": 0, "titulo": "Dev", "type": "agAgent",
             "data": {"rol": "dev", "ia": ia or {"kind": "api", "provider": "anthropic", "model": "claude-sonnet-5-5"}}},
            {"id": 2, "x": 300, "y": 0, "titulo": "App repo", "type": "agGithub",
             "data": {"repo": "https://github.com/acme/app", "permiso": perm, "merge": merge}}],
            "flechas": [{"id": 1, "fromId": 1, "toId": 2, "kind": "usa", "fromSide": "right", "toSide": "left"}]}
        with open(os.path.join(proj, "tree.json"), "w") as f:
            json.dump(tree, f)
    grafo()

    print("\n### B. el token")
    st, j = pedir("/gh/status?projectId=orch1&nodeId=2")
    check("sin token: el estado lo dice (y no hay clon)", st == 200 and j["tokenSet"] is False and j["cloned"] is False, str(j))
    st, j = pedir("/gh/sync", {"projectId": PID, "nodeId": 2})
    check("sincronizar sin token → error claro", st == 400 and "token" in j["error"], str(j))
    st, j = pedir("/orch/keys", {"projectId": PID, "keys": {"gh:2": {"key": TOKEN}}})
    check("guardar el token del nodo (keys.json gh:<nodo>)", st == 200, str(j))
    st, j = pedir(f"/orch/keys?projectId={PID}")
    check("el estado lo muestra como …últimos 4, NUNCA el secreto",
          j["keys"]["gh"].get("2", {}).get("hint") == "…abcd" and TOKEN not in json.dumps(j), json.dumps(j)[:200])
    st, j = pedir("/gh/verify", {"projectId": PID, "nodeId": 2})
    check("verificar contra GitHub: el repo, su rama principal y si se puede pushear",
          st == 200 and j == {"fullName": "acme/app", "private": True, "defaultBranch": "main", "canPush": True, "canAdmin": False}, str(j))
    check("la API recibió el token como Bearer", LLAMADAS[-1]["auth"] == f"Bearer {TOKEN}")

    print("\n### C. clonar, ramas, commit, push")
    st, j = pedir("/gh/sync", {"projectId": PID, "nodeId": 2})
    clon = os.path.join(home, "Library", "Application Support", "DiagraMind", "orchestrator", PID, "repos", "2") \
        if sys.platform == "darwin" else j.get("path")
    st2, s2 = pedir("/gh/status?projectId=orch1&nodeId=2")
    clon = s2.get("path") or clon
    check("sincronizar clona el repo", st == 200 and j["cloned"] and j["branch"] == "main", str(j))
    check("en la carpeta del orquestador", os.path.isfile(os.path.join(clon, "README.md")), clon)
    cfg = open(os.path.join(clon, ".git", "config")).read()
    check("el token NO quedó en .git/config (va en cada fetch/push)", TOKEN not in cfg and "acme/app.git" in cfg, cfg)
    st, j = pedir("/gh/tools?projectId=orch1&nodeId=2")
    nombres = [t["name"] for t in j["tools"]]
    check("/gh/tools: lo que habilita el nodo (editar, sin merge)", "git_push" in nombres and "gh_pr_merge" not in nombres, str(nombres))

    def call(tool, args=None):
        return pedir("/gh/call", {"projectId": PID, "nodeId": 2, "tool": tool, "args": args or {}, "author": "Dev"})

    st, j = call("git_checkout", {"branch": "feature/x", "create": True})
    check("crear una rama propia", st == 200 and j["result"]["branch"] == "feature/x", str(j))
    with open(os.path.join(clon, "hola.txt"), "w") as f:
        f.write("hola\n")
    st, j = call("git_status")
    check("status ve el archivo nuevo", st == 200 and j["result"]["changedCount"] == 1, str(j))
    st, j = call("git_commit", {"message": "Agrega hola"})
    check("commit", st == 200 and j["result"]["committed"] is True, str(j))
    code, out = git("log", "-1", "--format=%an|%B", cwd=clon)
    check("con el autor del agente y la marca de que lo hizo una IA", out.startswith("Dev|Agrega hola") and ghrepo.AI_NOTE in out, out)
    st, j = call("git_push")
    code, out = git("--git-dir", bare, "branch", "--list", "feature/x")
    check("push de la rama a GitHub (el remoto la tiene)", st == 200 and "feature/x" in out, str(j) + out)
    st, j = call("git_checkout", {"branch": "main"})
    with open(os.path.join(clon, "directo.txt"), "w") as f:
        f.write("x\n")
    call("git_commit", {"message": "directo a main"})
    st, j = call("git_push")
    check("push a la rama PRINCIPAL sin «merge» → 403 que dice el camino (rama + PR)",
          st == 403 and "PR" in j["error"], str(j))
    code, out = git("--git-dir", bare, "log", "-1", "--format=%s", "main")
    check("y main en GitHub sigue intacta", out.strip() == "init", out)
    st, j = call("git_diff", {"ref": "origin/main"})
    check("diff contra origin/main muestra el commit local", st == 200 and "directo.txt" in j["result"], str(j)[:200])

    print("\n### D. la API: PRs, comentarios, merge, issues")
    call("git_checkout", {"branch": "feature/x"})
    LLAMADAS.clear()
    st, j = call("gh_pr_create", {"title": "Feature X", "body": "Agrega hola"})
    pr = LLAMADAS[-1] if LLAMADAS else {}
    check("abrir un PR: head = la rama actual, base = la principal",
          st == 200 and pr.get("body", {}).get("head") == "feature/x" and pr["body"]["base"] == "main", str(pr))
    check("con la marca de IA en la descripción", ghrepo.AI_NOTE in pr.get("body", {}).get("body", ""))
    st, j = call("gh_pr_view", {"number": 7})
    check("ver un PR con sus archivos", st == 200 and j["result"]["files"][0]["file"] == "hola.txt", str(j))
    st, j = call("gh_comment", {"number": 7, "body": "Listo para revisar"})
    check("comentar", st == 200 and j["result"]["ok"], str(j))
    st, j = call("gh_issue_list")
    check("issues SIN los PRs (la API los mezcla)", st == 200 and [i["number"] for i in j["result"]] == [3], str(j))
    st, j = call("gh_pr_merge", {"number": 7})
    check("mergear sin el permiso → 403", st == 403, str(j))
    check("y GitHub no recibió nada", not any(c["method"] == "PUT" for c in LLAMADAS))
    grafo(merge=True)
    st, j = call("gh_pr_merge", {"number": 7, "method": "squash"})
    check("con «merge»: mergea (squash)", st == 200 and j["result"]["merged"] is True, str(j))
    check("GitHub recibió el PUT con el método", any(c["method"] == "PUT" and c["body"] == {"merge_method": "squash"} for c in LLAMADAS))
    call("git_checkout", {"branch": "main"})
    st, j = call("git_push")
    code, out = git("--git-dir", bare, "log", "-1", "--format=%s", "main")
    check("con «merge» sí puede pushear a la principal", st == 200 and out.strip() == "directo a main", str(j) + out)

    print("\n### F. el permiso se chequea en el servidor")
    grafo(perm="leer")
    st, j = call("git_commit", {"message": "x"})
    check("leer: commit por /gh/call directo → 403 (aunque el cliente lo pida)", st == 403, str(j))
    st, j = pedir("/gh/tools?projectId=orch1&nodeId=2")
    check("leer: y no se lista", "git_commit" not in [t["name"] for t in j["tools"]])
    st, j = pedir("/gh/call", {"projectId": PID, "nodeId": 1, "tool": "git_status"})
    check("un nodo que no es GitHub → 404", st == 404, str(j))
    grafo()

    print("\n### E. lo que ve el agente")
    st, j = pedir(f"/orch/inspect?projectId={PID}&nodeId=1")
    tools = [t["name"] for g in j.get("toolGroups", []) for t in g.get("tools", [])]
    check("agente API: tools de carpeta sobre el clon + las de git/GitHub, con el prefijo del recurso",
          "r2_fs_write" in tools and "r2_git_push" in tools and "r2_gh_pr_create" in tools, str(tools)[:300])
    check("sin merge en el nodo, sin gh_pr_merge", "r2_gh_pr_merge" not in tools)
    sistema = j.get("system") or ""
    check("el system le dice que use las tools (credenciales resueltas)", "acme/app" in sistema and "r2_git_" in sistema, sistema[-400:])
    check("y el token no aparece en nada de lo que ve el modelo", TOKEN not in json.dumps(j))

    grafo(ia={"kind": "cli", "provider": "local"})
    st, j = pedir(f"/orch/inspect?projectId={PID}&nodeId=1")
    refs = [r for r in j.get("toolRefs", j.get("refs", [])) if r.get("prefix") == "dmgh2"] if isinstance(j, dict) else []
    sistema = j.get("system") or ""
    check("cabeza CLI: el clon montado y el MCP dmgh2 para lo remoto",
          clon in sistema and "mcp__dmgh2__" in sistema and "NO credentials" in sistema, sistema[-500:])
    # Antigravity (agy ≥ 1.3) recibe el MCP por plugin: su nota nombra SU server y ya no le
    # dice que lo remoto lo haga otro (2026-10-10). El Inspect muestra lo mismo que el turno.
    grafo(ia={"kind": "cli", "provider": "local-antigravity"})
    st, j = pedir(f"/orch/inspect?projectId={PID}&nodeId=1")
    sis_agy = j.get("system") or ""
    check("cabeza agy: el system le nombra el server dmgh2_dmgh2 para lo remoto",
          "dmgh2_dmgh2" in sis_agy and "NOT available" not in sis_agy and "mcp__dmgh2__" not in sis_agy,
          sis_agy[-500:])
    check("…y el token tampoco aparece", TOKEN not in json.dumps(j))
    grafo(ia={"kind": "cli", "provider": "local"})
    import orch_cli
    spec = {"node_id": 1, "msg": "hola", "system": "s", "model": None, "effort": None, "add_dirs": [clon],
            "confinado": False, "mcp": {"dmgh2": {"kind": "gh", "nodeId": 2, "tools": editar}},
            "mcp_env": {"url": base, "token": tok}, "session": None, "project_id": PID}
    cmd, cfgp = orch_cli.ORCH_CLIS["local"].build("claude", spec)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    mcfg = json.load(open(cfgp))
    srv_cfg = mcfg["mcpServers"]["dmgh2"]
    check("Claude Code (no confinado): --mcp-config con el server dmgh2 (--mcp-gh)",
          "--mcp-config" in cmd and "--mcp-gh" in srv_cfg["args"], str(srv_cfg)[:200])
    check("sus tools PRE-APROBADAS junto al shell", "Bash" in allowed and "mcp__dmgh2__git_push" in allowed, allowed)
    check("el server lleva el token LOCAL, nunca el de GitHub",
          srv_cfg["env"]["DMGH_TOKEN"] == tok and TOKEN not in json.dumps(mcfg))
    os.remove(cfgp)
    spec["confinado"] = True
    cmd, cfgp = orch_cli.ORCH_CLIS["local"].build("claude", spec)
    check("confinado: dmgh2 también entra en la whitelist", "mcp__dmgh2__git_commit" in cmd[cmd.index("--allowedTools") + 1])
    os.remove(cfgp)

    # el MCP stdio de verdad: lo que lanzaría Claude Code
    p = subprocess.Popen([sys.executable, SERVER, "--mcp-gh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True,
                         env={**env, "DMGH_URL": base, "DMGH_TOKEN": tok, "DMGH_PROJECT": PID, "DMGH_NODE": "2"})
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "git_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "gh_pr_merge", "arguments": {"number": 7}}}]
    out, _ = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
    res = {r["id"]: r for r in (json.loads(x) for x in out.splitlines() if x.strip())}
    check("MCP stdio: tools/list trae lo que el backend habilita",
          "git_push" in [t["name"] for t in res[2]["result"]["tools"]] and
          "gh_pr_merge" not in [t["name"] for t in res[2]["result"]["tools"]], str(res.get(2))[:200])
    check("MCP stdio: tools/call git_status contra el clon", '"cloned": true' in res[3]["result"]["content"][0]["text"])
    check("MCP stdio: una tool no habilitada vuelve como error (el backend la rechaza)",
          res[4]["result"]["isError"] is True and "not allowed" in res[4]["result"]["content"][0]["text"], str(res.get(4)))

    print("\n### B2. un token malo")
    pedir("/orch/keys", {"projectId": PID, "keys": {"gh:2": {"key": "ghp_malo_000000000000"}}})
    st, j = pedir("/gh/verify", {"projectId": PID, "nodeId": 2})
    check("GitHub lo rechaza → 401 con un mensaje que dice qué hacer", st == 401 and "Replace it" in j["error"], str(j))
finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    fake.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
