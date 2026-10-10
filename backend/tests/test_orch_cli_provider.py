"""Qué CLI corre el ORQUESTADOR en un nodo agente, y cómo arma y lee su turno.

Desde 2026-09-15 el motor despacha por PERFIL (`orch_cli.py`) y no contra `claude` fijo:
un orquestador puede tener un nodo Claude Code y otro Antigravity corriendo a la vez.
Codex y Gemini quedan afuera porque no tienen `--add-dir`, y en el orquestador el agente
corre en un workspace vacío donde sus recursos se montan justamente con ese flag.

Lo que este test cuida:

- **Regresión de Claude Code**: su comando queda EXACTAMENTE como estaba antes del
  refactor. Es el único camino que ya corría en producción.
- **Antigravity**: que se le pase lo que su binario acepta (system dentro del prompt,
  `--conversation`) y NADA que no tenga (`--append-system-prompt`,
  `--allowedTools`, `--mcp-config`) — un flag inventado lo mata al arrancar.
- Que las guardas que no se pueden cumplir se rechacen ANTES de gastar un turno.
- **El shell** (2026-10-08): un agente NO confinado tiene Bash/PowerShell SIEMPRE y
  pre-aprobados; el confinado nunca (su shell es `fs_exec`). Antes iba atado a que algún
  recurso tuviera permiso «ejecutar» y en la práctica quedaba sin Bash.

No llama a ninguna API ni ejecuta ningún CLI: corre sin red.

    python3 backend/tests/test_orch_cli_provider.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orch_cli                                            # noqa: E402
import orchestrator                                        # noqa: E402
from clis import CLIS                                      # noqa: E402
from orch_cli import ORCH_CLIS, CliTurnError               # noqa: E402
from orchestrator import (CLI_ENGINE_OK, CLI_PROVIDERS, OrchError, _cli_for,  # noqa: E402
                          cli_session_get, cli_session_set)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def nodo(provider, **data):
    return {"id": "n1", "titulo": "Contador", "data": {"ia": {"provider": provider}, **data}}


def spec(**kw):
    """Un spec como el que arma `_cli_cmd`, con lo mínimo de un agente real."""
    s = {"node_id": "n1", "msg": "hacé X", "system": "YOU ARE THE ACCOUNTANT",
         "model": None, "effort": None, "add_dirs": [], "confinado": False,
         "mcp": {}, "mcp_env": {"url": "http://127.0.0.1:8765", "token": "tok"},
         "session": None}
    s.update(kw)
    return s


def flag(cmd, name):
    """El valor que sigue a `name`, o None si el flag no está."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


print("\n=== A. la web ofrece cuatro CLIs; el motor EJECUTA los que tienen perfil ===")
for p in ("local", "local-codex", "local-gemini", "local-antigravity"):
    check(f"«{p}» entra por la rama CLI (no por la de API)", p in CLI_PROVIDERS)
check("hay un provider CLI por cada adaptador del chat", len(CLI_PROVIDERS) == len(CLIS),
      f"{sorted(CLI_PROVIDERS)} vs {sorted(CLIS)}")
check("el motor ejecuta Claude Code y Antigravity",
      sorted(ORCH_CLIS) == ["local", "local-antigravity"], sorted(ORCH_CLIS))
check("CLI_ENGINE_OK sale de los perfiles (una sola fuente)", CLI_ENGINE_OK == set(ORCH_CLIS))
# cada perfil del orquestador tiene su adaptador en el chat: mismo binario en los dos lados
check("el perfil de Claude apunta al mismo binario que el chat",
      ORCH_CLIS["local"].bin_names == CLIS["claude"].bin_names)
check("y el de Antigravity también (`agy`)",
      ORCH_CLIS["local-antigravity"].bin_names == CLIS["antigravity"].bin_names == ["agy"])

print("\n=== B. qué perfil le toca a cada nodo ===")
check("«local» → Claude Code", _cli_for(nodo("local")).key == "local")
check("«local-antigravity» → Antigravity", _cli_for(nodo("local-antigravity")).key == "local-antigravity")
check("un nodo sin ia definida cae en Claude Code", _cli_for({"id": "n", "titulo": "x", "data": {}}).key == "local")
for p in ("local-codex", "local-gemini"):
    try:
        _cli_for(nodo(p))
        check(f"«{p}» se rechaza", False, "no levantó OrchError")
    except OrchError as e:
        msg = str(getattr(e, "detail", e))
        check(f"«{p}» se rechaza", True)
        check("  …explicando que es por --add-dir (los recursos del nodo)", "--add-dir" in msg, msg)
        check("  …y nombrando los que SÍ corren", "Claude Code" in msg and "Antigravity" in msg, msg)
        check("  …y que en el chat andan", "chat" in msg.lower(), msg)

print("\n=== C. confinado: solo el CLI que puede cumplirlo ===")
check("Claude Code puede confinar", ORCH_CLIS["local"].can_confine is True)
check("Antigravity NO puede (no tiene MCP ni --allowedTools)",
      ORCH_CLIS["local-antigravity"].can_confine is False)
check("un nodo confinado con Claude Code se acepta",
      _cli_for(nodo("local", confinado=True)).key == "local")
try:
    _cli_for(nodo("local-antigravity", confinado=True))
    check("un nodo confinado con agy se rechaza", False, "no levantó OrchError")
except OrchError as e:
    msg = str(getattr(e, "detail", e))
    check("un nodo confinado con agy se rechaza", True)
    check("  …diciendo en qué se apoya el confinamiento",
          "--mcp-config" in msg and "--allowedTools" in msg, msg)
    check("  …y qué hacer (Claude Code, o apagarlo)", "Claude Code" in msg, msg)

print("\n=== D. Claude Code: el comando no se movió (regresión) ===")
claude = ORCH_CLIS["local"]
cmd, cfg = claude.build("/bin/claude", spec(model="claude-opus-5", add_dirs=["/tmp/proj"],
                                            session="sess-1"))
check("lanza `-p` con el mensaje", cmd[1] == "-p" and cmd[2] == "hacé X", " ".join(cmd[:3]))
check("el system va en --append-system-prompt",
      flag(cmd, "--append-system-prompt") == "YOU ARE THE ACCOUNTANT")
check("el modelo se mapea al alias del CLI", flag(cmd, "--model") == "opus", flag(cmd, "--model"))
check("stream-json + verbose", "--output-format" in cmd and "--verbose" in cmd)
check("--permission-mode acceptEdits", flag(cmd, "--permission-mode") == "acceptEdits")
check("monta el recurso con --add-dir", flag(cmd, "--add-dir") == "/tmp/proj")
check("sin confinar pre-aprueba el shell (Bash y PowerShell)",
      flag(cmd, "--allowedTools") == "Bash,PowerShell", " ".join(cmd))
check("retoma con --resume", flag(cmd, "--resume") == "sess-1")
check("sin confinar no arma config MCP", cfg is None)
cmd_min, _ = claude.build("/bin/claude", spec())
deny = lambda c: c[c.index("--disallowedTools") + 1:c.index("--allowedTools") if "--allowedTools" in c else None]
check("sin confinar y SIN recursos igual tiene shell (era el bug: quedaba sin Bash)",
      "Bash" not in deny(cmd_min) and flag(cmd_min, "--allowedTools") == "Bash,PowerShell",
      " ".join(cmd_min))
# La regresión de verdad: el comando ENTERO, no flag por flag. Si alguien lo mueve, se
# entera acá. (Cambió a propósito el 2026-10-08: el shell ya no se niega sin confinar.)
check("el comando completo es exactamente el esperado", cmd_min == [
    "/bin/claude", "-p", "hacé X", "--output-format", "stream-json", "--verbose",
    "--model", "sonnet", "--permission-mode", "acceptEdits",
    "--append-system-prompt", "YOU ARE THE ACCOUNTANT",
    "--disallowedTools", "WebFetch", "WebSearch",
    "--allowedTools", "Bash,PowerShell"], " ".join(cmd_min))
cmd_conf, cfg_conf = claude.build("/bin/claude", spec(confinado=True, add_dirs=["/tmp/d"],
                                                      mcp={"dmfs7": {"projectId": "p7", "perm": 2}}))
check("confinado arma el --mcp-config", cfg_conf is not None and "--mcp-config" in cmd_conf)
check("y la whitelist nombra las tools del MCP",
      "mcp__dmfs7__fs_exec" in flag(cmd_conf, "--allowedTools"), flag(cmd_conf, "--allowedTools"))
check("confinado NO tiene el shell nativo (su shell es fs_exec)",
      "Bash" in cmd_conf[cmd_conf.index("--disallowedTools"):] and
      "Bash" not in flag(cmd_conf, "--allowedTools"), " ".join(cmd_conf))
if cfg_conf:
    os.remove(cfg_conf)
cmd_eff, _ = claude.build("/bin/claude", spec(effort="high"))
check("el esfuerzo se le suma al mensaje (keyword)", cmd_eff[2] != "hacé X", cmd_eff[2][:60])

print("\n=== E. Antigravity: solo flags que su binario tiene ===")
agy = ORCH_CLIS["local-antigravity"]
cmd, cfg = agy.build("/bin/agy", spec(add_dirs=["/tmp/a", "/tmp/b"], session="conv-9"))
check("lanza `-p`", cmd[1] == "-p")
check("el system va DENTRO del prompt (no tiene flag propio)",
      "YOU ARE THE ACCOUNTANT" in cmd[2] and "hacé X" in cmd[2], cmd[2][:80])
check("y el system va ANTES de la tarea", cmd[2].index("ACCOUNTANT") < cmd[2].index("hacé X"))
check("monta los DOS recursos con --add-dir",
      cmd.count("--add-dir") == 2 and "/tmp/a" in cmd and "/tmp/b" in cmd, " ".join(cmd))
check("retoma con --conversation (no --resume)",
      flag(cmd, "--conversation") == "conv-9" and "--resume" not in cmd)
check("modo accept-edits + saltear permisos (headless no puede aprobar)",
      flag(cmd, "--mode") == "accept-edits" and "--dangerously-skip-permissions" in cmd)
check("sin --sandbox: no confinado = terminal de verdad, como Claude Code", "--sandbox" not in cmd)
check("no arma --mcp-config (no tiene: el MCP le llega por plugin, ver K)", cfg is None)
for f in ("--append-system-prompt", "--allowedTools", "--disallowedTools", "--mcp-config",
          "--permission-mode", "--verbose"):
    check(f"NO le pasa «{f}» (su binario no lo tiene)", f not in cmd)
check("el modelo por defecto es el real del adaptador",
      flag(agy.build("/bin/agy", spec())[0], "--model") == "gemini-3.7-flash-medium")
check("y un id elegido se manda tal cual (el CLI no acepta alias)",
      flag(agy.build("/bin/agy", spec(model="claude-opus-4-6-thinking"))[0], "--model")
      == "claude-opus-4-6-thinking")
check("el esfuerzo NO se le suma al mensaje (ya va pegado al id del modelo)",
      agy.build("/bin/agy", spec(effort="high"))[0][2] == agy.build("/bin/agy", spec())[0][2],
      "el effort cambió el prompt de agy")

print("\n=== F. leer el stream: Claude Code ===")
logs = []
st = orch_cli.new_state()
claude.feed({"type": "system", "subtype": "init", "session_id": "s7"}, st, lambda t, full=None: logs.append(t))
claude.feed({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "hola"}, {"type": "tool_use", "name": "Read"}]}},
    st, lambda t, full=None: logs.append(t))
claude.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "chau"}]}},
            st, lambda t, full=None: logs.append(t))
claude.feed({"type": "result", "session_id": "s7", "result": None, "total_cost_usd": 0.42},
            st, lambda t, full=None: logs.append(t))
text, sid, cost = orch_cli.result_of(st, claude)
check("saca la sesión", sid == "s7")
check("junta los bloques de texto con renglón en blanco", text == "hola\n\nchau", repr(text))
check("informa el costo", cost == 0.42)
check("loguea la tool usada", any("Read" in x for x in logs), str(logs))
den = []
claude.feed({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "This command requires approval"}]}},
    orch_cli.new_state(), lambda t, full=None: den.append(t))
check("avisa cuando una tool se DENIEGA headless", any("denied" in x.lower() for x in den), str(den))
try:
    claude.feed({"type": "result", "is_error": True, "result": "boom"}, orch_cli.new_state(),
                lambda t, full=None: None)
    check("un error del CLI corta el turno", False, "no levantó")
except CliTurnError as e:
    check("un error del CLI corta el turno", "boom" in str(e), str(e))

print("\n=== G. leer el stream: Antigravity (eventos REALES del binario) ===")
logs = []
st = orch_cli.new_state()
log = lambda t, full=None: logs.append(t)          # noqa: E731
agy.feed({"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.7-flash-medium"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "agent_response",
                                                  "state": "ACTIVE", "text_delta": "Ya "}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
                                                  "tool_name": "view_file"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                                                  "tool_name": "view_file"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "agent_response",
                                                  "state": "DONE", "text_delta": "terminé."}}, st, log)
agy.feed({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS",
                                        "response": "Ya terminé."}}, st, log)
text, sid, cost = orch_cli.result_of(st, agy)
check("saca el conversation_id (es lo que retoma el próximo turno)", sid == "c1")
check("pega los PEDAZOS sin separador", text == "Ya terminé.", repr(text))
check("loguea la tool UNA vez (el DONE no repite)",
      len([x for x in logs if "view_file" in x]) == 1, str(logs))
check("no inventa un costo (agy no lo informa)", cost == 0.0)
st2 = orch_cli.new_state()
agy.feed({"event": "result", "result": {"conversation_id": "c2", "status": "SUCCESS",
                                        "response": "respuesta directa"}}, st2, log)
check("si no hubo deltas usa el `response` final",
      orch_cli.result_of(st2, agy)[0] == "respuesta directa")
try:
    agy.feed({"event": "result", "result": {"status": "ERROR", "response": "se cayó"}},
             orch_cli.new_state(), log)
    check("un status != SUCCESS corta el turno", False, "no levantó")
except CliTurnError as e:
    check("un status != SUCCESS corta el turno", "se cayó" in str(e), str(e))

print("\n=== H. la sesión guardada es DE SU CLI ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx = {"app_dir": tmp, "pid": "p1"}
    cli_session_set(ctx, "n1", "abc", "local")
    check("Claude recupera la suya", cli_session_get(ctx, "n1", "local") == "abc")
    check("agy NO recibe la sesión de Claude (sería un id que no entiende)",
          cli_session_get(ctx, "n1", "local-antigravity") is None)
    cli_session_set(ctx, "n2", "conv-1", "local-antigravity")
    check("agy recupera la suya", cli_session_get(ctx, "n2", "local-antigravity") == "conv-1")
    check("y Claude no toma la de agy", cli_session_get(ctx, "n2", "local") is None)
    # formato viejo (string suelto): era de Claude Code, que era el único que había
    orchestrator._write_json(orchestrator._cli_sessions_path(ctx), {"n3": "vieja"})
    check("una sesión del formato viejo sigue siendo de Claude",
          cli_session_get(ctx, "n3", "local") == "vieja")
    check("…y no se le pasa a otro CLI", cli_session_get(ctx, "n3", "local-antigravity") is None)
    check("sin pedir CLI, devuelve lo que haya (compat)", cli_session_get(ctx, "n3") == "vieja")

print("\n=== I. los locks de un agente con una CARPETA cableada ===")
# Un agFolder no tiene `projectId` (tiene `path`): `_lock_keys` lo indexaba a pelo y el
# run moría con «error interno del motor: 'projectId'» antes del primer turno.
with tempfile.TemporaryDirectory() as tmp:
    graph = {"nodos": {
        1: {"id": 1, "type": "agAgent", "data": {}},
        2: {"id": 2, "type": "agFolder", "data": {"path": tmp, "permiso": "editar"}},
        3: {"id": 3, "type": "agFolder", "data": {"path": tmp + "/", "permiso": "editar"}},
        4: {"id": 4, "type": "agResource", "data": {"projectId": "p9", "permiso": "editar"}},
        5: {"id": 5, "type": "agFolder", "data": {"path": tmp, "permiso": "leer"}},
    }, "flechas": [{"kind": "usa", "fromId": 1, "toId": t} for t in (2, 3, 4, 5)]}
    try:
        keys = orchestrator._lock_keys(graph, {"nodeId": 1})
        err = None
    except Exception as e:
        keys, err = [], e
    check("una carpeta cableada NO tira KeyError", err is None, repr(err))
    dirs = [k for k in keys if k.startswith("dir:")]
    check("la carpeta se lockea por su path real", dirs == [f"dir:{os.path.realpath(tmp)}"] * 2, str(keys))
    check("el proyecto sigue lockeándose por su id", "res:p9" in keys, str(keys))
    check("con permiso «leer» no toma lock", len(keys) == 4, str(keys))
    run = {"locks": {}}
    orchestrator._try_locks(graph, run, {"id": "f1", "nodeId": 1})
    graph["flechas"].append({"kind": "usa", "fromId": 6, "toId": 3})
    check("otro agente sobre la MISMA carpeta espera",
          orchestrator._try_locks(graph, run, {"id": "f2", "nodeId": 6}) is False)

print("\n=== K. Antigravity recibe el MCP del nodo GitHub por PLUGIN (agy ≥ 1.3) ===")
# agy no tiene --mcp-config, pero carga `<workspace>/.agents/plugins/<p>/mcp_config.json`
# también en `-p` (verificado contra agy 1.3.3 el 2026-10-10: listó y llamó la tool del
# plugin). Sin esto un agente agy con un repo cableado no podía ni commitear (2026-10-10).
ws = tempfile.mkdtemp(prefix="dmagyws-")
viejo = os.path.join(ws, ".agents", "plugins", "dmgh99")
os.makedirs(viejo)                                   # un repo que ya NO está cableado
ajeno = os.path.join(ws, ".agents", "plugins", "otro")
os.makedirs(ajeno)                                   # algo que no es nuestro: no se toca
gh = {"dmgh7": {"kind": "gh", "nodeId": 7, "tools": ["git_status", "git_push"]}}
cmd, cfg = agy.build("/bin/agy", spec(mcp=gh, workspace=ws, project_id="orchP"))
plug = os.path.join(ws, ".agents", "plugins", "dmgh7")
check("escribe el plugin dmgh7 en el workspace del agente",
      os.path.isfile(os.path.join(plug, "plugin.json")) and os.path.isfile(os.path.join(plug, "mcp_config.json")))
mc = json.load(open(os.path.join(plug, "mcp_config.json")))
srv = mc["mcpServers"]["dmgh7"]
check("con el MCP --mcp-gh de este backend (el mismo que Claude Code)", "--mcp-gh" in srv["args"], str(srv))
check("apuntando al nodo y al orquestador",
      srv["env"]["DMGH_NODE"] == "7" and srv["env"]["DMGH_PROJECT"] == "orchP", str(srv["env"]))
check("con el token LOCAL", srv["env"]["DMGH_TOKEN"] == "tok")
if os.name != "nt":
    check("el config va 0600 (tiene el token local)",
          (os.stat(os.path.join(plug, "mcp_config.json")).st_mode & 0o777) == 0o600)
check("borra el plugin de un repo que ya no está cableado", not os.path.exists(viejo))
check("y no toca plugins que no son suyos", os.path.isdir(ajeno))
check("sigue sin --mcp-config (su binario no lo tiene)", "--mcp-config" not in cmd and cfg is None)
agy.build("/bin/agy", spec(mcp={}, workspace=ws))
check("sin repos cableados, no queda ningún plugin dmgh",
      not any(n.startswith("dmgh") for n in os.listdir(os.path.join(ws, ".agents", "plugins"))))
agy.build("/bin/agy", spec(mcp=gh))                  # sin workspace (tests viejos): no explota
check("sin workspace no escribe nada ni explota", True)
shutil.rmtree(ws, ignore_errors=True)

nota = ["- «app» (GitHub repo a/b, permission editar): a local clone at /x — work on its files "
        "DIRECTLY; for git and GitHub use the `mcp__dmgh7__*` tools — a `git push` from your "
        "shell has NO credentials and will fail."]
n_agy = orchestrator._cli_gh_notes(agy, nota, gh)[0]
check("la nota de agy nombra SU server (dmgh7_dmgh7) y cómo llamarlo",
      "dmgh7_dmgh7" in n_agy and "call_mcp_tool" in n_agy and "mcp__" not in n_agy, n_agy)
check("…y ya no le dice que no tiene las tools", "NOT available" not in n_agy, n_agy)
n_cl = orchestrator._cli_gh_notes(claude, nota, gh)[0]
check("la de Claude Code queda igual (mcp__dmgh7__*)", n_cl == nota[0], n_cl)
class _SinMcp: pass
n_no = orchestrator._cli_gh_notes(_SinMcp(), nota, gh)[0]
check("un CLI que no sabe cargarlo no recibe la promesa", "NOT available" in n_no and "mcp__" not in n_no, n_no)

print(f"\n{'✅' if fail == 0 else '❌'} {ok}/{ok + fail}")
sys.exit(0 if fail == 0 else 1)
