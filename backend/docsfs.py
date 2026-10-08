"""Blobs del modo `documents` (doc 30, fase 3) — store content-addressed.

El nombre del archivo en disco ES el sha256 de su contenido:

    <projects>/<Carpeta>/<Proyecto>/documents/<hash>

Los METADATOS (nombre, mime, carpeta virtual) NO viven acá: van en el manifiesto
del tree.json, que ya viaja por los mirrors normales. Acá solo bytes, y el hash
es a la vez id, dedupe y VERIFICACIÓN de integridad: en cada `put` se recalcula
el sha256 y se compara con el que declara el cliente (si no coincide → 400, no
se escribe). Así los mirrors se mantienen sanos sin confiar en el emisor.

Lógica pura (sin HTTP) para poder espejarla en el conector externo (fase 4), igual
que editorfs.py / sourcever.py.
"""

import hashlib
import os
import shutil

DOCS_DIRNAME = "documents"
BYNAME_DIRNAME = "by-name"             # vista legible (hardlinks) para la IA y el usuario
MAX_BLOB = 200 * 1024 * 1024          # 200 MB por blob (la web corta antes, en 100)
HASH_LEN = 64                          # sha256 hex


def docs_dir(project_dir):
    return os.path.join(project_dir, DOCS_DIRNAME)


def valid_hash(h):
    """El hash viene del cliente y se usa como NOMBRE DE ARCHIVO: validarlo es lo
    que impide un path traversal (`../..`) por el nombre del blob."""
    if not isinstance(h, str) or len(h) != HASH_LEN:
        return False
    return all(c in "0123456789abcdef" for c in h.lower())


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def blob_path(project_dir, h):
    return os.path.join(docs_dir(project_dir), h.lower())


# ===================== operaciones =====================

def docs_list(project_dir):
    """Hashes que EXISTEN en el disco (lo que el mirror ya tiene) + tamaños."""
    d = docs_dir(project_dir)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        if not valid_hash(n):
            continue                    # ignorar cualquier cosa que no sea un blob
        try:
            out.append({"hash": n, "size": os.path.getsize(os.path.join(d, n))})
        except OSError:
            pass
    return 200, {"blobs": out}


def docs_get(project_dir, h):
    """Bytes de un blob. Devuelve (code, body_dict) con body['bytes'] en el caso OK
    para que el server lo mande crudo."""
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    p = blob_path(project_dir, h)
    if not os.path.isfile(p):
        return 404, {"error": "blob no encontrado"}
    try:
        with open(p, "rb") as f:
            return 200, {"bytes": f.read()}
    except OSError as e:
        return 500, {"error": str(e)}


def docs_put(project_dir, h, data):
    """Guarda un blob VERIFICANDO que su sha256 sea el declarado. Idempotente: si
    ya está (mismo hash = mismo contenido), no reescribe."""
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    if not isinstance(data, (bytes, bytearray)) or not data:
        return 400, {"error": "empty body"}
    if len(data) > MAX_BLOB:
        return 413, {"error": f"blob too large (max {MAX_BLOB // (1024 * 1024)} MB)"}
    real = sha256_bytes(data)
    if real != h.lower():
        # la verificación del doc 30 decisión D: el contenido no es lo que dice ser
        return 400, {"error": "el contenido no coincide con el hash", "expected": h.lower(), "got": real}
    d = docs_dir(project_dir)
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, real)
        if os.path.isfile(p) and os.path.getsize(p) == len(data):
            return 200, {"ok": True, "hash": real, "size": len(data), "deduped": True}
        tmp = p + ".part"                # escritura atómica: no dejar blobs a medias
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return 200, {"ok": True, "hash": real, "size": len(data)}
    except OSError as e:
        return 500, {"error": str(e)}


def docs_delete(project_dir, h):
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    p = blob_path(project_dir, h)
    try:
        if os.path.isfile(p):
            os.remove(p)
        return 200, {"ok": True}
    except OSError as e:
        return 500, {"error": str(e)}


def _safe_component(part):
    """Un tramo de ruta seguro: sin separadores, sin `..`, sin vacíos."""
    part = str(part or "").replace("\\", "/").split("/")[-1].strip()
    if part in ("", ".", ".."):
        return ""
    return "".join(c for c in part if c not in '\0:*?"<>|')


def docs_link_names(project_dir, entries):
    """Reconstruye `<documents>/by-name/` : una vista LEGIBLE de la biblioteca —
    los nombres reales (con extensión) y las carpetas virtuales del manifiesto,
    apuntando a los blobs con **hardlinks** (mismo filesystem → no ocupan espacio).

    Es lo que hace usable la biblioteca para un agente que mira el disco (Claude
    Code con `--add-dir`): `documents/<hash>` no dice nada, `by-name/papers/
    informe.pdf` sí. Se REGENERA entera en cada sync, así nunca queda desfasada.
    """
    root = os.path.join(docs_dir(project_dir), BYNAME_DIRNAME)
    shutil.rmtree(root, ignore_errors=True)
    made = 0
    for e in entries or []:
        h = str((e or {}).get("hash") or "").lower()
        if not valid_hash(h):
            continue
        src = blob_path(project_dir, h)
        if not os.path.isfile(src):
            continue                      # todavía no se subió: no hay a qué linkear
        name = _safe_component((e or {}).get("name") or h[:12])
        if not name:
            continue
        parts = [p for p in (_safe_component(x) for x in str((e or {}).get("dir") or "").split("/")) if p]
        dest_dir = os.path.join(root, *parts)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest):      # dos docs con el mismo nombre en la misma carpeta
                stem, ext = os.path.splitext(name)
                dest = os.path.join(dest_dir, f"{stem}-{h[:6]}{ext}")
            try:
                os.link(src, dest)        # hardlink: sin duplicar bytes
            except OSError:
                shutil.copy2(src, dest)   # otro filesystem / FS sin links: copia
            made += 1
        except OSError:
            pass
    return made


GC_GRACE_S = 60      # un blob más nuevo que esto no se poda (ver docs_gc)


def docs_gc(project_dir, keep_hashes, now=None):
    """Borra del disco los blobs que el manifiesto ya no referencia. `keep_hashes`
    es la lista de hashes del tree.json (la web la manda al sincronizar).

    Un blob de menos de GC_GRACE_S NO se borra: la web poda con SU manifiesto, y uno
    que un agente acaba de dejar por la bandeja (ingest_inbox) todavía no le llegó.
    Sin la gracia, una sync de la web en ese medio segundo se comía el archivo nuevo
    y el manifiesto quedaba apuntando a nada. Lo que de verdad sobra se va en la
    próxima sync."""
    import time
    now = time.time() if now is None else now
    keep = {h.lower() for h in (keep_hashes or []) if valid_hash(h)}
    d = docs_dir(project_dir)
    removed = []
    try:
        names = os.listdir(d)
    except OSError:
        return 200, {"ok": True, "removed": []}
    for n in names:
        if valid_hash(n) and n.lower() not in keep:
            try:
                if now - os.path.getmtime(os.path.join(d, n)) < GC_GRACE_S:
                    continue
            except OSError:
                continue
            try:
                os.remove(os.path.join(d, n))
                removed.append(n)
            except OSError:
                pass
    return 200, {"ok": True, "removed": removed}


# ===================== la BANDEJA DE ENTRADA (2026-10-08) =====================
# Antes el agente no podía AGREGAR nada a la biblioteca: la skill le decía «que lo suba
# el usuario». Pedirle «leé estos archivos y hacé otro con X» terminaba en un archivo
# suelto que la app no mostraba. Ahora el agente deja el archivo en
# `documents/inbox/[carpeta virtual/]nombre.ext` y el backend lo INGIERE: lo guarda por
# hash, lo agrega al manifiesto y el watcher se lo emite a la web, que baja los bytes.

INBOX_DIRNAME = "inbox"
MAX_INGEST = 100 * 1024 * 1024        # el mismo tope que la subida de la web
SETTLE_S = 1.0                        # un archivo más nuevo que esto puede estar a medio escribir
_MIME_EXTRA = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
               ".csv": "text/csv", ".json": "application/json", ".pdf": "application/pdf",
               ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp3": "audio/mpeg"}


def inbox_dir(project_dir):
    return os.path.join(docs_dir(project_dir), INBOX_DIRNAME)


def _mime_of(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in _MIME_EXTRA:
        return _MIME_EXTRA[ext]
    import mimetypes
    return mimetypes.guess_type(name)[0] or ""


def _ignorable(name):
    """Temporales de editores y del sistema: no son documentos."""
    return (name.startswith(".") or name.startswith("~$") or name.endswith((".part", ".tmp", ".swp", "~"))
            or name in ("Thumbs.db", "desktop.ini"))


def _nombre_libre(docs, name, d):
    """Si ya hay un doc con ese nombre en esa carpeta, «nombre (2).ext»: pisar uno
    del usuario con lo que escribió el agente sería perder trabajo."""
    usados = {x.get("name") for x in docs if (x.get("dir") or "") == d}
    if name not in usados:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem} ({n}){ext}" in usados:
        n += 1
    return f"{stem} ({n}){ext}"


def ingest_inbox(project_dir, manifest, now=None):
    """Mueve lo que haya en la bandeja al store y lo agrega al manifiesto (un dict del
    tree.json de tipo documents, que se MODIFICA en el lugar).

    Devuelve (agregados, rechazados): listas de dicts. Si no hay nada listo, ([], []).
    Un archivo que todavía se está escribiendo (mtime de hace menos de SETTLE_S) se
    deja para la próxima pasada. Uno demasiado grande se deja donde está, con un
    `<nombre>.TOO-BIG.txt` al lado que lo explica (el agente mira la carpeta)."""
    import time
    now = time.time() if now is None else now
    root = inbox_dir(project_dir)
    if not os.path.isdir(root):
        return [], []
    docs = manifest.setdefault("docs", [])
    dirs = manifest.setdefault("dirs", [])
    last = max([manifest.get("lastId") or 0] + [x.get("id") or 0 for x in docs])
    agregados, rechazados = [], []
    for base, subdirs, files in os.walk(root):
        subdirs[:] = [s for s in subdirs if not s.startswith(".")]
        rel = os.path.relpath(base, root)
        parts = [] if rel == "." else [p for p in (_safe_component(x) for x in rel.split(os.sep)) if p]
        vdir = "/".join(parts)
        for fn in sorted(files):
            if _ignorable(fn) or fn.endswith(".TOO-BIG.txt"):
                continue
            src = os.path.join(base, fn)
            try:
                st = os.stat(src)
            except OSError:
                continue
            if now - st.st_mtime < SETTLE_S:
                continue                              # a medio escribir: la próxima pasada
            if st.st_size == 0:
                continue                              # vacío: probablemente recién creado
            if st.st_size > MAX_INGEST:
                aviso = src + ".TOO-BIG.txt"
                if not os.path.exists(aviso):
                    try:
                        with open(aviso, "w", encoding="utf-8") as f:
                            f.write(f"Not added to the library: {fn} is {st.st_size // (1024 * 1024)} MB "
                                    f"and the limit is {MAX_INGEST // (1024 * 1024)} MB.\n")
                    except OSError:
                        pass
                rechazados.append({"name": fn, "reason": "too big"})
                continue
            try:
                with open(src, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            h = sha256_bytes(data)
            code, _ = docs_put(project_dir, h, data)
            if code != 200:
                rechazados.append({"name": fn, "reason": f"store failed ({code})"})
                continue
            # la carpeta virtual y TODOS sus prefijos (la web los lista así: "a", "a/b")
            for i in range(1, len(parts) + 1):
                p = "/".join(parts[:i])
                if p not in dirs:
                    dirs.append(p)
            name = _nombre_libre(docs, _safe_component(fn) or h[:12], vdir)
            last += 1
            doc = {"id": last, "name": name, "mime": _mime_of(name), "size": len(data),
                   "hash": h, "ts": int(now * 1000), "dir": vdir}
            docs.append(doc)
            agregados.append(doc)
            try:
                os.remove(src)
            except OSError:
                pass
    if agregados:
        manifest["lastId"] = last
        manifest["type"] = "documents"
    # carpetas de la bandeja que quedaron vacías: fuera (la raíz `inbox/` se deja)
    for base, _subdirs, _files in sorted(os.walk(root), key=lambda t: -len(t[0])):
        if base != root:
            try:
                os.rmdir(base)
            except OSError:
                pass
    return agregados, rechazados
