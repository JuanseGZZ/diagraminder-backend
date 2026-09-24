"""Qué puede hacer el MCP: el interruptor y los tres niveles (doc 37 §F19).

El MCP no es "una feature prendida para siempre": es **acceso de un programa de afuera
a tu máquina**, y eso se prende, se apaga y se gradúa. Acá vive esa decisión, y vive
en el SERVIDOR a propósito.

Por qué en el servidor: una vez que pegaste el `.mcp.json`, el cliente MCP ya tiene la
URL y el token. Si el interruptor viviera en la web, apagarlo no apagaría nada — el
cliente seguiría entrando por su lado. Es la regla dura de CLAUDE.md: *una guarda en el
cliente NO es una regla*. Así que el `diagram_mcp` pregunta acá en cada `tools/list` y
en cada `tools/call`, y el backend vuelve a chequear en `/fs` antes de tocar un archivo.

Los tres niveles son acumulativos:

    diagrams  los 4 tools de diagramas y las 8 de memoria (memory_*). Es el default.
    files     + leer/escribir/editar/grep/git, CONFINADO a `root`.
    shell     + ejecutar comandos. El confinamiento por carpeta NO alcanza contra un
              comando arbitrario, así que este nivel se elige a mano y se avisa.

`enabled` arranca en True y el modo en "diagrams": el que ya tenía el MCP andando no
se entera de nada, y lo nuevo (tocar archivos) es opt-in explícito.
"""
import os

NIVELES = ("diagrams", "files", "shell")

# Lo que cada nivel agrega sobre el anterior. `diagram_mcp` arma su lista de tools con
# esto, así que un nivel que no incluye una tool NO la ve el modelo: preferimos que no
# exista a que exista y sea rechazada (una lista corta se entiende; un rechazo, no).
TOOLS_POR_NIVEL = {
    "diagrams": ("list_diagrams", "read_diagram", "diagram_schema", "write_diagram",
                 # la memoria: los mismos diagramas, de a un nodo (diagram_memory.py)
                 "memory_overview", "memory_search", "memory_read", "memory_add",
                 "memory_update", "memory_link", "memory_unlink", "memory_delete"),
    "files": ("fs_tree", "fs_read", "fs_write", "fs_edit", "fs_mkdir", "fs_rename",
              "fs_delete", "fs_grep", "sv_save", "sv_list", "sv_restore",
              "gh_push", "gh_pull", "gh_log"),
    "shell": ("fs_exec",),
}

DEFAULT = {"enabled": True, "mode": "diagrams", "root": "", "remote": False}


def normalizar(cfg):
    """Un dict de config.json → una política válida. Nunca tira: un config.json roto
    o editado a mano cae al default, que es el nivel MÁS BAJO de permisos."""
    d = dict(DEFAULT)
    if isinstance(cfg, dict):
        m = cfg.get("mode")
        d["enabled"] = bool(cfg.get("enabled", True))
        d["mode"] = m if m in NIVELES else "diagrams"
        d["root"] = str(cfg.get("root") or "")
        d["remote"] = bool(cfg.get("remote", False))
    # Un modo que toca archivos sin carpeta elegida NO es "toda tu máquina": es
    # "todavía no configurado". Se degrada a diagramas en vez de abrir el disco.
    if d["mode"] in ("files", "shell") and not d["root"]:
        d["mode"] = "diagrams"
    return d


def herramientas(pol):
    """Los nombres de tools habilitados por esta política. Vacío si está apagado."""
    if not pol.get("enabled"):
        return ()
    fuera = []
    for n in NIVELES:
        fuera += list(TOOLS_POR_NIVEL[n])
        if n == pol.get("mode"):
            break
    return tuple(fuera)


def permite(pol, tool):
    return tool in herramientas(pol)


def motivo(pol, tool):
    """Por qué NO se permite, dicho para el MODELO (en inglés, regla dura).
    Un 'no' que no explica cómo destrabarlo hace que el modelo reintente al pedo."""
    if not pol.get("enabled"):
        return ("the MCP is turned off in DiagraMinder. Ask the user to enable it in "
                "Settings → MCP. Do not retry until they say they did.")
    if tool in TOOLS_POR_NIVEL["shell"]:
        return ("running commands is not enabled. The user has DiagraMinder's MCP at "
                f"level '{pol.get('mode')}'; running commands needs level 'shell', "
                "which they must turn on in Settings → MCP.")
    if tool in TOOLS_POR_NIVEL["files"]:
        return ("file access is not enabled. The user has DiagraMinder's MCP limited to "
                "diagrams. To read or write files they must pick a folder and raise the "
                "level in Settings → MCP.")
    return f"unknown tool: {tool}"


def dentro_de_root(pol, path):
    """¿`path` cae adentro de la raíz autorizada? Se resuelve con realpath para que un
    symlink o un `..` no saquen al agente de la carpeta (mismo criterio que editorfs)."""
    root = pol.get("root") or ""
    if not root:
        return False
    try:
        r = os.path.realpath(root)
        p = os.path.realpath(path if os.path.isabs(path) else os.path.join(r, path))
    except Exception:
        return False
    return p == r or p.startswith(r + os.sep)
