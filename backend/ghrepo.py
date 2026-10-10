"""El nodo GitHub del orquestador (doc 28 §GitHub): un REPO como recurso de un agente.

Un `agGithub` es un repositorio de GitHub (`owner/name`) con su token (PAT) guardado
en el `keys.json` del orquestador (sección `gh:<nodo>`, decisión T): nunca en el
tree.json, nunca en `.git/config`, nunca en lo que ve el modelo. El backend lo clona en
`<orch>/<pid>/repos/<nodo>/` y ese clon se registra como target de `editorfs` con la
misma clave que un agFolder (`<pid>#<nodo>`): el agente trabaja los ARCHIVOS con las
tools de carpeta de siempre (fs_*, versiones, shell), y lo REMOTO con las tools de acá.

Qué puede hacer lo decide el NODO y se chequea EN EL SERVIDOR (no en el cliente, regla
dura), en dos lugares: al armar la lista de tools (lo que no está habilitado NO se
lista: una lista corta el modelo la entiende sola) y otra vez al ejecutar cada una.

    leer      → ver el repo: status, log, diff, ramas, traer lo último, PRs e issues
    editar    → + ramas propias, commit, push (a ramas que NO son la principal),
                abrir PRs, comentar, abrir issues
    ejecutar  → + shell en el clon (eso lo da la parte de carpeta, no este módulo)
    merge     → (tilde aparte) mergear PRs y pushear a la rama principal

Las operaciones de git usan `svgit._git` (sin prompts, con el token redactado de toda
salida); las de GitHub, la API REST. `DMN_GH_API` / `DMN_GH_GIT` cambian las bases
(los tests apuntan a un GitHub falso y a un remoto en disco).

Lógica pura (sin HTTP propio). Errores → GhError(code, msg).
"""
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request

import svgit

API_TIMEOUT = 30
DIFF_CAP = 60_000
TEXT_CAP = 20_000
PERM_LEVEL = {"leer": 0, "editar": 1, "ejecutar": 2}
AI_NOTE = "[made by an AI agent via DiagraMinder]"


class GhError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def api_base():
    return (os.environ.get("DMN_GH_API") or "https://api.github.com").rstrip("/")


def git_base():
    return (os.environ.get("DMN_GH_GIT") or "https://github.com").rstrip("/")


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_repo(s):
    """`owner/name` desde lo que se pegue: `owner/name`, la URL https (con o sin .git,
    con /tree/… atrás) o el `git@github.com:owner/name.git` de ssh. None si no cierra."""
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("git@"):
        s = s.split(":", 1)[-1]
    elif "://" in s:
        parts = [p for p in urllib.parse.urlparse(s).path.split("/") if p]
        s = "/".join(parts[:2])
    s = s.strip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return s if _REPO_RE.match(s) and ".." not in s else None


def remote_url(repo):
    return f"{git_base()}/{repo}.git"


# ---------------------------------------------------------------- API de GitHub

def _api_error(status, payload):
    msg = (payload or {}).get("message") if isinstance(payload, dict) else None
    if status == 401:
        return GhError(401, "GitHub rejected the token (expired or revoked?). Replace it in the node.")
    if status == 404:
        return GhError(404, "Not found — or the repo is private and the token has no access to it.")
    if status == 403:
        return GhError(403, f"GitHub refused: {msg or 'the token lacks permission for this'}.")
    if status in (405, 409):
        return GhError(409, f"GitHub can't do it right now: {msg or 'conflict'}.")
    if status == 422:
        errs = (payload or {}).get("errors") if isinstance(payload, dict) else None
        det = "; ".join(str(e.get("message") or e) for e in errs or [] if e) if errs else ""
        return GhError(422, f"GitHub rejected the request: {msg or 'validation failed'}"
                            + (f" ({det})" if det else ""))
    return GhError(502, f"GitHub answered {status}: {msg or 'unexpected error'}")


def api(token, method, path, body=None):
    """Una llamada a la API REST. Devuelve el JSON; GhError legible si falla."""
    if not token:
        raise GhError(400, "This GitHub node has no token yet: set one in the node.")
    req = urllib.request.Request(api_base() + path, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "DiagraMinder")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=API_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        raise _api_error(e.code, payload)
    except (urllib.error.URLError, OSError) as e:
        raise GhError(502, f"Couldn't reach GitHub: {getattr(e, 'reason', e)}")


def verify(repo, token):
    """¿El repo existe y el token llega? → lo que la UI muestra al conectar."""
    j = api(token, "GET", f"/repos/{repo}")
    perms = j.get("permissions") or {}
    return {"fullName": j.get("full_name") or repo, "private": bool(j.get("private")),
            "defaultBranch": j.get("default_branch") or "main",
            "canPush": bool(perms.get("push")), "canAdmin": bool(perms.get("admin"))}


# ---------------------------------------------------------------- el clon local

# UN lock por clon. Un agente commiteando mientras el humano aprieta «Clonar / traer», o
# dos turnos que tocan el mismo nodo, se pisaban: dos `git clone` a la misma carpeta, y el
# que fallaba BORRABA la del otro a mitad de camino (pasó en el e2e, 2026-10-10). RLock:
# `run_tool` lo toma y adentro `clone` lo vuelve a tomar.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(path):
    key = os.path.realpath(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())

def _git(args, cwd, token=None, timeout=None):
    try:
        return svgit._git(args, cwd, token, timeout)
    except svgit.GitError as e:
        raise GhError(e.code, e.msg)


def is_cloned(path):
    return os.path.isdir(os.path.join(path, ".git"))


def clone(path, repo, token):
    """Clona si no está. El origin queda con la URL LIMPIA: el token se inyecta en
    cada fetch/push y no se escribe nunca en `.git/config`."""
    with lock_for(path):
        return _clone(path, repo, token)


def _clone(path, repo, token):
    if is_cloned(path):
        return False
    if os.path.isdir(path) and os.listdir(path):
        raise GhError(409, f"the clone folder is not empty and not a repo: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isdir(path):
        os.rmdir(path)
    code, out = _git(["clone", svgit._auth_url(remote_url(repo), token), path],
                     os.path.dirname(path), token, timeout=600)
    if code != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise GhError(400, f"clone failed: {svgit._explain(out)} ({out.strip()[-300:]})")
    _git(["remote", "set-url", "origin", remote_url(repo)], path)
    return True


def current_branch(path):
    code, out = _git(["branch", "--show-current"], path, timeout=15)
    return out.strip() if code == 0 else ""


def default_branch(path):
    """La rama principal según el clon (origin/HEAD), sin pedirle nada a la API."""
    code, out = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path, timeout=15)
    if code == 0 and out.strip().startswith("origin/"):
        return out.strip()[len("origin/"):]
    return "main"


def _changes(path):
    code, out = _git(["status", "--porcelain"], path, timeout=30)
    return [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []


def status(path):
    if not is_cloned(path):
        return {"cloned": False}
    br = current_branch(path)
    ch = _changes(path)
    code, out = _git(["log", "-1", "--format=%h %s"], path, timeout=15)
    ahead = behind = None
    code2, out2 = _git(["rev-list", "--left-right", "--count", f"origin/{br}...HEAD"], path, timeout=15)
    if code2 == 0 and out2.strip():
        try:
            behind, ahead = (int(x) for x in out2.split())
        except ValueError:
            pass
    return {"cloned": True, "branch": br, "defaultBranch": default_branch(path),
            "changes": ch[:200], "changedCount": len(ch),
            "lastCommit": out.strip() if code == 0 else "", "ahead": ahead, "behind": behind}


def fetch(path, repo, token):
    code, out = _git(["fetch", "--prune", svgit._auth_url(remote_url(repo), token),
                      "+refs/heads/*:refs/remotes/origin/*"], path, token, timeout=300)
    if code != 0:
        raise GhError(400, f"fetch failed: {svgit._explain(out)}")


def pull(path, repo, token):
    """Trae lo último de la rama actual (fast-forward). Con cambios sin commitear no
    toca nada: un merge a ciegas sobre el trabajo de un agente es justo lo que no."""
    fetch(path, repo, token)
    br = current_branch(path)
    if _changes(path):
        raise GhError(409, "There are uncommitted changes: commit them (or discard them) before pulling.")
    code, out = _git(["rev-parse", "--verify", f"origin/{br}"], path, timeout=15)
    if code != 0:
        return {"ok": True, "branch": br, "note": "this branch is not on GitHub yet: nothing to pull"}
    code, out = _git(["merge", "--ff-only", f"origin/{br}"], path, token)
    if code != 0:
        raise GhError(409, f"Can't fast-forward {br}: it diverged from GitHub. ({out.strip()[-200:]})")
    return {"ok": True, "branch": br, "head": status(path).get("lastCommit")}


def branches(path, repo, token):
    fetch(path, repo, token)
    code, out = _git(["branch", "-a", "--format=%(refname:short)"], path, timeout=30)
    local, remote = [], []
    for b in out.splitlines():
        b = b.strip()
        if not b or b == "origin/HEAD" or b == "origin":
            continue
        (remote if b.startswith("origin/") else local).append(b.replace("origin/", "", 1))
    return {"current": current_branch(path), "default": default_branch(path),
            "local": local, "remote": remote}


_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")


def checkout(path, branch, create=False, base=None):
    if not _BRANCH_RE.match(branch or ""):
        raise GhError(400, f"invalid branch name: {branch!r}")
    if create:
        args = ["checkout", "-b", branch] + ([f"origin/{base}"] if base else [])
    else:
        code, _ = _git(["rev-parse", "--verify", branch], path, timeout=15)
        args = ["checkout", branch] if code == 0 else ["checkout", "-b", branch, "--track", f"origin/{branch}"]
    code, out = _git(args, path)
    if code != 0:
        raise GhError(400, f"checkout failed: {out.strip()[-300:]}")
    return {"ok": True, "branch": current_branch(path)}


def commit(path, message, author):
    msg = (message or "").strip()
    if not msg:
        raise GhError(400, "a commit needs a message")
    _git(["add", "-A"], path)
    code, _ = _git(["diff", "--cached", "--quiet"], path)
    if code == 0:
        return {"ok": True, "committed": False, "note": "nothing to commit"}
    email = f"{(author or 'diagraminder').replace(' ', '.').lower()}@diagraminder.local"
    code, out = _git([*svgit._ident(author), "commit", "-m", f"{msg}\n\n{AI_NOTE}",
                      f"--author={author or 'DiagraMinder'} <{email}>"], path)
    if code != 0:
        raise GhError(400, f"commit failed: {out.strip()[-300:]}")
    code, out = _git(["log", "-1", "--format=%h"], path)
    return {"ok": True, "committed": True, "sha": out.strip(), "branch": current_branch(path)}


def push(path, repo, token, allow_default):
    """Pushea la rama ACTUAL a GitHub. A la principal, solo con el permiso de merge:
    sin él, el camino es una rama + un PR (que alguien mergea)."""
    br = current_branch(path)
    if not br:
        raise GhError(400, "not on a branch (detached HEAD)")
    if br == default_branch(path) and not allow_default:
        raise GhError(403, f"Pushing to «{br}» (the main branch) is not allowed for this node. "
                           "Create a branch (git_checkout with create=true), push it and open a PR.")
    code, out = _git(["push", svgit._auth_url(remote_url(repo), token), f"HEAD:refs/heads/{br}"],
                     path, token, timeout=300)
    if code != 0:
        raise GhError(400, f"push failed: {out.strip()[-300:]}")
    _git(["fetch", svgit._auth_url(remote_url(repo), token), f"+refs/heads/{br}:refs/remotes/origin/{br}"],
         path, token)
    return {"ok": True, "branch": br}


def diff(path, ref=None):
    args = ["diff", "--stat", "--patch"] + ([ref] if ref else ["HEAD"])
    code, out = _git(args, path, timeout=60)
    if code != 0:
        raise GhError(400, f"diff failed: {out.strip()[-300:]}")
    return out if len(out) <= DIFF_CAP else out[:DIFF_CAP] + f"\n… (+{len(out) - DIFF_CAP} chars)"


def log(path, n=20):
    return svgit._log(path, "HEAD", max(1, min(int(n or 20), 100)))


# ---------------------------------------------------------------- PRs e issues

def _cut(s, n=TEXT_CAP):
    s = s or ""
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def _pr_short(p):
    return {"number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
            "draft": p.get("draft"), "author": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
            "url": p.get("html_url")}


def pr_list(repo, token, state="open"):
    st = state if state in ("open", "closed", "all") else "open"
    return [_pr_short(p) for p in api(token, "GET", f"/repos/{repo}/pulls?state={st}&per_page=50")]


def pr_view(repo, token, number):
    n = int(number)
    p = api(token, "GET", f"/repos/{repo}/pulls/{n}")
    files = api(token, "GET", f"/repos/{repo}/pulls/{n}/files?per_page=100")
    return {**_pr_short(p), "body": _cut(p.get("body")), "mergeable": p.get("mergeable"),
            "merged": p.get("merged"), "files": [{"file": f.get("filename"), "status": f.get("status"),
                                                  "+": f.get("additions"), "-": f.get("deletions")} for f in files]}


def pr_create(repo, token, title, body, head, base, draft=False):
    if not (title or "").strip():
        raise GhError(400, "a PR needs a title")
    p = api(token, "POST", f"/repos/{repo}/pulls",
            {"title": title.strip(), "body": ((body or "").strip() + f"\n\n{AI_NOTE}").strip(),
             "head": head, "base": base, "draft": bool(draft)})
    return _pr_short(p)


def pr_merge(repo, token, number, method="squash"):
    m = method if method in ("merge", "squash", "rebase") else "squash"
    j = api(token, "PUT", f"/repos/{repo}/pulls/{int(number)}/merge", {"merge_method": m})
    return {"merged": bool(j.get("merged")), "sha": j.get("sha"), "message": j.get("message")}


def comment(repo, token, number, body):
    """Un PR es un issue para los comentarios: el mismo endpoint sirve a los dos."""
    if not (body or "").strip():
        raise GhError(400, "empty comment")
    j = api(token, "POST", f"/repos/{repo}/issues/{int(number)}/comments",
            {"body": body.strip() + f"\n\n{AI_NOTE}"})
    return {"ok": True, "url": j.get("html_url")}


def issue_list(repo, token, state="open"):
    st = state if state in ("open", "closed", "all") else "open"
    out = []
    for i in api(token, "GET", f"/repos/{repo}/issues?state={st}&per_page=50"):
        if "pull_request" in i:
            continue                      # la API de issues también trae los PRs (la clave marca)
        out.append({"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
                    "author": (i.get("user") or {}).get("login"),
                    "labels": [lb.get("name") for lb in i.get("labels") or []], "url": i.get("html_url")})
    return out


def issue_view(repo, token, number):
    n = int(number)
    i = api(token, "GET", f"/repos/{repo}/issues/{n}")
    cs = api(token, "GET", f"/repos/{repo}/issues/{n}/comments?per_page=50")
    return {"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
            "body": _cut(i.get("body")), "author": (i.get("user") or {}).get("login"),
            "comments": [{"author": (c.get("user") or {}).get("login"), "body": _cut(c.get("body"), 4000)}
                         for c in cs]}


def issue_create(repo, token, title, body):
    if not (title or "").strip():
        raise GhError(400, "an issue needs a title")
    j = api(token, "POST", f"/repos/{repo}/issues",
            {"title": title.strip(), "body": ((body or "").strip() + f"\n\n{AI_NOTE}").strip()})
    return {"number": j.get("number"), "url": j.get("html_url")}


# ---------------------------------------------------------------- las tools

_S = {"type": "string"}
_N = {"type": "integer"}


def _schema(props=None, req=None):
    return {"type": "object", "properties": props or {}, "required": req or []}


# (nombre, nivel mínimo, ¿pide merge?, descripción, schema). El nivel es el `permiso`
# del nodo (0 leer, 1 editar). Va al MODELO → en inglés.
TOOLS = [
    ("git_status", 0, False, "Status of the local clone of the GitHub repo: current branch, uncommitted "
     "changes, last commit, ahead/behind GitHub.", _schema()),
    ("git_log", 0, False, "Latest commits of the current branch ({sha, author, ts, msg}).",
     _schema({"n": _N})),
    ("git_diff", 0, False, "Diff of the working tree against HEAD (uncommitted changes), or against `ref` "
     "(a branch/sha, e.g. origin/main to see everything this branch changes).", _schema({"ref": _S})),
    ("git_branches", 0, False, "Fetches from GitHub and lists the branches (local and on GitHub) plus the "
     "current and the main one.", _schema()),
    ("git_pull", 0, False, "Brings the latest of the CURRENT branch from GitHub (fast-forward only). Refuses "
     "if there are uncommitted changes.", _schema()),
    ("gh_pr_list", 0, False, "Lists the pull requests of the repo.",
     _schema({"state": {"type": "string", "enum": ["open", "closed", "all"]}})),
    ("gh_pr_view", 0, False, "One pull request: title, description, state, mergeability and changed files.",
     _schema({"number": _N}, ["number"])),
    ("gh_issue_list", 0, False, "Lists the issues of the repo (pull requests excluded).",
     _schema({"state": {"type": "string", "enum": ["open", "closed", "all"]}})),
    ("gh_issue_view", 0, False, "One issue with its comments.", _schema({"number": _N}, ["number"])),
    ("git_checkout", 1, False, "Switches to a branch. With create=true creates it (from `base`, default the "
     "current HEAD). Work on your OWN branch, not on the main one.",
     _schema({"branch": _S, "create": {"type": "boolean"}, "base": _S}, ["branch"])),
    ("git_commit", 1, False, "Stages EVERYTHING in the clone and commits it with your message (marked as made "
     "by an AI). Commit before pushing.", _schema({"message": _S}, ["message"])),
    ("git_push", 1, False, "Pushes the CURRENT branch to GitHub (credentials are handled for you — never use "
     "`git push` from a shell, it has no credentials).", _schema()),
    ("gh_pr_create", 1, False, "Opens a pull request from `head` (default: the current branch, push it first) "
     "into `base` (default: the main branch).",
     _schema({"title": _S, "body": _S, "head": _S, "base": _S, "draft": {"type": "boolean"}}, ["title"])),
    ("gh_comment", 1, False, "Comments on an issue or a pull request (same numbering).",
     _schema({"number": _N, "body": _S}, ["number", "body"])),
    ("gh_issue_create", 1, False, "Opens an issue.", _schema({"title": _S, "body": _S}, ["title"])),
    ("gh_pr_merge", 1, True, "MERGES a pull request on GitHub. method: squash (default), merge or rebase. "
     "Only when the work was checked (tests/review) or the human asked for it.",
     _schema({"number": _N, "method": {"type": "string", "enum": ["squash", "merge", "rebase"]}}, ["number"])),
]


def allowed_tools(perm, merge):
    """Los nombres que el nodo habilita. LA regla: la usan la lista y la ejecución."""
    lvl = PERM_LEVEL.get(perm, 1)
    return [n for n, need, needs_merge, _d, _s in TOOLS if lvl >= need and (merge or not needs_merge)]


def tool_specs(perm, merge):
    ok = set(allowed_tools(perm, merge))
    out = []
    for n, _need, _m, desc, schema in TOOLS:
        if n in ok:
            if n == "git_push" and merge:
                desc += " This node may push to the main branch too."
            elif n == "git_push":
                desc += " NOT to the main branch: push your own branch and open a PR."
            out.append({"name": n, "description": desc, "inputSchema": schema})
    return out


def run_tool(name, args, *, path, repo, token, perm, merge, author):
    """Ejecuta UNA tool sobre el clon. Devuelve el resultado (dict/list/str). Vuelve a
    chequear el permiso: la lista de tools no alcanza como regla."""
    if name not in allowed_tools(perm, merge):
        raise GhError(403, f"«{name}» is not allowed for this GitHub node")
    with lock_for(path):
        return _run_tool(name, args or {}, path, repo, token, merge, author)


def _run_tool(name, a, path, repo, token, merge, author):
    if not is_cloned(path):
        clone(path, repo, token)
    if name == "git_status":
        return status(path)
    if name == "git_log":
        return log(path, a.get("n") or 20)
    if name == "git_diff":
        return diff(path, a.get("ref"))
    if name == "git_branches":
        return branches(path, repo, token)
    if name == "git_pull":
        return pull(path, repo, token)
    if name == "git_checkout":
        return checkout(path, a.get("branch"), bool(a.get("create")), a.get("base"))
    if name == "git_commit":
        return commit(path, a.get("message"), author)
    if name == "git_push":
        return push(path, repo, token, allow_default=merge)
    if name == "gh_pr_list":
        return pr_list(repo, token, a.get("state") or "open")
    if name == "gh_pr_view":
        return pr_view(repo, token, a.get("number"))
    if name == "gh_issue_list":
        return issue_list(repo, token, a.get("state") or "open")
    if name == "gh_issue_view":
        return issue_view(repo, token, a.get("number"))
    if name == "gh_pr_create":
        return pr_create(repo, token, a.get("title"), a.get("body"),
                         a.get("head") or current_branch(path), a.get("base") or default_branch(path),
                         a.get("draft"))
    if name == "gh_comment":
        return comment(repo, token, a.get("number"), a.get("body"))
    if name == "gh_issue_create":
        return issue_create(repo, token, a.get("title"), a.get("body"))
    if name == "gh_pr_merge":
        return pr_merge(repo, token, a.get("number"), a.get("method") or "squash")
    raise GhError(400, f"unknown tool: {name}")
