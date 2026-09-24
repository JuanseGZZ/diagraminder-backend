"""Los diagramas como MEMORIA ASOCIATIVA para un agente (tools `memory_*` del MCP).

Por qué existe: con `read_diagram`/`write_diagram` un agente puede EDITAR un canvas, pero
no USARLO como memoria. Para leer un nodo se tragaba el `tree.json` entero —ids, x/y,
colores y el HTML del editor—, que pesa más tokens que la misma información en prosa.
Y para agregarle un hermano a un nodo tenía que reescribir el JSON completo e inventar
una posición que no pisara nada. Así el canvas terminaba siendo un esqueleto de
etiquetas que nadie mantenía (2026-09-24, lo dijo un agente mirando uno de 76 nodos).

Lo que da este módulo es RECORRER en vez de tragar: un mapa compacto, buscar, leer un
nodo con sus vecinos, y escribir de a un nodo (agregar hijo/hermano/padre, editar,
conectar, desconectar, borrar) sin tocar el JSON.

Sirve para los dos modos que son memoria:
  - `cart` (Organigrama): un ÁRBOL. Vecinos = padre, hijos y hermanos.
  - `freestyle` (Canvas): un GRAFO. Vecinos = flechas de entrada y de salida; y los
    sectores (`grupo`) como carpetas.

Es PURO a propósito: recibe el árbol como dict y devuelve texto (y el árbol nuevo si
escribió). La red y el disco los pone `diagram_mcp.py`, que escribe por `/state/write`
igual que `write_diagram` —así el usuario ve el cambio en vivo—. Los esquemas son los
de `skills.py` y los `toJson()` de `app/trees/`: no se inventa ningún campo.

Todo lo que devuelve lo lee el MODELO → en inglés (regla dura de CLAUDE.md).
"""
import html
import json
import re
import unicodedata
from html.parser import HTMLParser

TIPOS = ("cart", "freestyle")
GRUPOS = ("grupo", "objgroup", "agDept")      # sectores: se dibujan detrás, no son destino de flechas

# Geometría del canvas (la misma que usa la app: doc 10 y autoLayout.js)
MD_ANCHO = 300
LAYER_GAP = 110
NODE_GAP = 36
PAD_GRUPO = 40
MAX_LEER = 25            # nodos con contenido completo en un memory_read (tope de tokens)


class MemoryToolError(Exception):
    """Un error que el modelo tiene que leer (y corregir): va como texto de la tool."""


# ---------------------------------------------------------------------------------
# texto ⇄ HTML del editor rico

class _ATexto(HTMLParser):
    """HTML del editor → texto plano con algo de markdown (lo más barato de leer)."""
    BLOQUES = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr",
               "blockquote", "pre", "hr", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.href = None
        self.pre = 0
        self.listas = []

    def _nl(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl()
            # ida y vuelta con a_html: <h3> ⇄ "# ", <h4> ⇄ "## ", <h5> ⇄ "### "
            self.out.append({"h4": "## ", "h5": "### ", "h6": "### "}.get(tag, "# "))
        elif tag in ("ul", "ol"):
            self._nl()
            self.listas.append([tag, 0])
        elif tag == "li":
            self._nl()
            if self.listas and self.listas[-1][0] == "ol":
                self.listas[-1][1] += 1
                marca = f"{self.listas[-1][1]}. "
            else:
                marca = "- "
            self.out.append("  " * max(0, len(self.listas) - 1) + marca)
        elif tag == "pre":
            self._nl()
            self.out.append("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag == "a":
            self.href = a.get("href") or ""
        elif tag == "img":
            self.out.append(f"[image: {a.get('alt') or 'attached'}]")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOQUES:
            self._nl()

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            if self.listas:
                self.listas.pop()
            self._nl()
        elif tag == "pre":
            self._nl()
            self.out.append("```\n")
            self.pre = max(0, self.pre - 1)
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("b", "strong"):
            self.out.append("**")
        elif tag == "a":
            h = self.href or ""
            m = re.match(r"#node:(\d+)", h)
            if m:
                self.out.append(f" [→ node {m.group(1)}]")
            elif h and not h.startswith("#"):
                self.out.append(f" ({h})")
            self.href = None
        elif tag in self.BLOQUES:
            self._nl()

    def handle_data(self, data):
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        self.out.append(data)


def a_texto(contenido):
    """El cuerpo de un nodo (HTML del editor, o texto pelado) → texto legible."""
    s = contenido or ""
    if "<" not in s:
        return s.strip()
    p = _ATexto()
    try:
        p.feed(s)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", s).strip()
    t = "".join(p.out)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\*\*\s*\*\*", "", t)
    return t.strip()


def _inline(s):
    """Markdown en línea → HTML (sobre texto YA escapado)."""
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def a_html(texto):
    """Texto / markdown simple → el HTML que dibuja el editor rico.

    El editor NO lee markdown: un `**x**` sale con asteriscos y un `\\n` no corta
    (skills.py, «What goes in the body of an md»). Por eso la conversión la hace el
    backend y el modelo escribe como escribe siempre."""
    texto = (texto or "").replace("\r\n", "\n").strip()
    if not texto:
        return ""
    out, lista, parrafo, pre = [], None, [], None

    def cerrar_parrafo():
        if parrafo:
            out.append("<p>" + "<br>".join(_inline(x) for x in parrafo) + "</p>")
            parrafo.clear()

    def cerrar_lista():
        nonlocal lista
        if lista:
            out.append(f"<{lista[0]}>" + "".join(f"<li>{_inline(x)}</li>" for x in lista[1]) + f"</{lista[0]}>")
            lista = None

    for crudo in texto.split("\n"):
        if pre is not None:
            if crudo.strip().startswith("```"):
                out.append("<pre>" + html.escape("\n".join(pre)) + "</pre>")
                pre = None
            else:
                pre.append(crudo)
            continue
        if crudo.strip().startswith("```"):
            cerrar_parrafo(); cerrar_lista()
            pre = []
            continue
        linea = html.escape(crudo.strip(), quote=False)
        m_h = re.match(r"(#{1,6})\s+(.*)", linea)
        m_ul = re.match(r"[-*•]\s+(.*)", linea)
        m_ol = re.match(r"\d+[.)]\s+(.*)", linea)
        if not linea:
            cerrar_parrafo(); cerrar_lista()
        elif m_h:
            cerrar_parrafo(); cerrar_lista()
            n = min(5, len(m_h.group(1)) + 2)          # # → h3, ## → h4, ### → h5
            out.append(f"<h{n}>{_inline(m_h.group(2))}</h{n}>")
        elif m_ul or m_ol:
            cerrar_parrafo()
            tipo = "ul" if m_ul else "ol"
            if lista and lista[0] != tipo:
                cerrar_lista()
            if not lista:
                lista = [tipo, []]
            lista[1].append((m_ul or m_ol).group(1))
        else:
            cerrar_lista()
            parrafo.append(linea)
    if pre is not None:
        out.append("<pre>" + html.escape("\n".join(pre)) + "</pre>")
    cerrar_parrafo(); cerrar_lista()
    return "".join(out)


def _plano(s):
    """Para buscar: minúsculas y sin tildes (una búsqueda de «sesion» encuentra «sesión»)."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _corto(s, n=70):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _color(c):
    if c in (None, "", "none", "null"):
        return None
    if not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        raise MemoryToolError("`color` must be a hex string like \"#e53935\" (or omit it).")
    return c


def _entero(v, campo):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise MemoryToolError(f"`{campo}` must be a node id (an integer); memory_overview lists them.")


# ---------------------------------------------------------------------------------
# un modo, una clase: el árbol y el grafo responden a las MISMAS preguntas

class _Cart:
    """El Organigrama: nodoRaiz → listaHijos (skills.py, diagramind-cart)."""

    def __init__(self, obj):
        self.obj = obj
        if not isinstance(obj.get("nodoRaiz"), dict):
            raise MemoryToolError("this organigram has no `nodoRaiz`: it is empty or broken. "
                              "Use read_diagram to see it.")
        self.raiz = obj["nodoRaiz"]
        self.por_id, self.padre = {}, {}
        pila = [(self.raiz, None)]
        while pila:
            n, p = pila.pop()
            n.setdefault("listaHijos", [])
            self.por_id[n.get("idCarta")] = n
            self.padre[n.get("idCarta")] = p
            for h in n["listaHijos"]:
                pila.append((h, n))

    # --- lectura ---
    def titulo(self, n):
        return (n.get("tituloCarta") or "").strip() or _corto(a_texto(n.get("descripcion")), 50) or "(untitled)"

    def texto(self, n):
        return a_texto(n.get("descripcion"))

    def nodo(self, i):
        n = self.por_id.get(_entero(i, "id"))
        if n is None:
            raise MemoryToolError(f"there is no card with id {i} in this organigram.")
        return n

    def ruta(self, n):
        out, p = [], self.padre.get(n.get("idCarta"))
        while p is not None:
            out.append(self.titulo(p))
            p = self.padre.get(p.get("idCarta"))
        return " › ".join(reversed(out))

    def vecinos(self, n):
        """[(id, relación)] — la relación dicha desde este nodo."""
        out = []
        p = self.padre.get(n.get("idCarta"))
        if p is not None:
            out.append((p.get("idCarta"), "parent"))
        out += [(h.get("idCarta"), "child") for h in n["listaHijos"]]
        return out

    def overview(self, tope):
        total = len(self.por_id)
        # La raíz NO se dibuja: es el lienzo (la app dice «Add to root»). Contarla como
        # carta haría que el modelo le hable al usuario de una carta que no ve.
        lineas = [f'Organigram — {total - 1} cards, a tree. [id] title (+N = children). '
                  f"[{self.raiz.get('idCarta')}] is the root: the canvas itself, NOT drawn as a card — "
                  "its children are the top-level cards. "
                  "Read cards with memory_read(ids); find them with memory_search."]
        # a lo ancho: si hay que cortar, se cortan las hojas profundas, no las ramas de arriba
        orden, cola = [], [(self.raiz, 0)]
        while cola and len(orden) < tope:
            n, d = cola.pop(0)
            orden.append((n, d))
            cola += [(h, d + 1) for h in n["listaHijos"]]
        mostrados = {id(n) for n, _ in orden}

        def dibujar(n, d):
            if id(n) not in mostrados:
                return
            hijos = n["listaHijos"]
            extra = f" (+{len(hijos)})" if hijos else ""
            lineas.append(f"{'  ' * d}- [{n.get('idCarta')}] {self.titulo(n)}{extra}")
            for h in hijos:
                dibujar(h, d + 1)
        dibujar(self.raiz, 0)
        if total > len(orden):
            lineas.append(f"… {total - len(orden)} deeper cards not shown: memory_read a branch "
                          "with depth, or raise max_nodes.")
        return "\n".join(lineas)

    def todos(self):
        return list(self.por_id.values())

    def id_de(self, n):
        return n.get("idCarta")

    def ficha(self, n, con_texto=True):
        cab = f"[{n.get('idCarta')}] {self.titulo(n)}"
        ruta = self.ruta(n)
        partes = [cab + (f"\npath: {ruta}" if ruta else "  (the root: the canvas itself, not drawn as a card)")]
        if con_texto:
            t = self.texto(n)
            partes.append(t if t else "(no text yet)")
        hijos = n["listaHijos"]
        if hijos:
            partes.append("children: " + " · ".join(f"[{h.get('idCarta')}] {self.titulo(h)}" for h in hijos))
        return "\n".join(partes)

    # --- escritura ---
    def _siguiente(self):
        self.obj["lastIdCharged"] = max(int(self.obj.get("lastIdCharged") or 0),
                                        max((int(i) for i in self.por_id if isinstance(i, int)), default=0)) + 1
        return self.obj["lastIdCharged"]

    def add(self, titulo, contenido, ancla, relacion, etiqueta, color):
        if etiqueta:
            raise MemoryToolError("`label` is for canvas arrows; an organigram only has parent/child.")
        nuevo_id = self._siguiente()
        carta = {"idCarta": nuevo_id, "idPadre": None, "tituloCarta": titulo,
                 "descripcion": a_html(contenido), "color": color, "shape": "default",
                 "collapsed": True, "hijosOcultos": False, "listaHijos": []}
        a = self.raiz if ancla is None else self.nodo(ancla)
        if ancla is None or relacion == "child":
            carta["idPadre"] = a.get("idCarta")
            a["listaHijos"].append(carta)
            dicho = f"as a child of [{a.get('idCarta')}] {self.titulo(a)}"
        elif relacion == "sibling":
            p = self.padre.get(a.get("idCarta"))
            if p is None:
                raise MemoryToolError("the root has no siblings. Add it as a child of the root instead.")
            carta["idPadre"] = p.get("idCarta")
            p["listaHijos"].insert(p["listaHijos"].index(a) + 1, carta)
            dicho = f"as a sibling of [{a.get('idCarta')}] {self.titulo(a)}, under [{p.get('idCarta')}] {self.titulo(p)}"
        else:   # parent: la carta nueva toma el lugar del ancla, y el ancla pasa a ser su hija
            p = self.padre.get(a.get("idCarta"))
            if p is None:
                # la raíz es el lienzo: ponerle un padre la volvería una carta visible
                raise MemoryToolError("the root is the canvas itself: it cannot get a parent. "
                                      "Add the card as a child of the root instead.")
            carta["idPadre"] = p.get("idCarta")
            p["listaHijos"][p["listaHijos"].index(a)] = carta
            a["idPadre"] = nuevo_id
            carta["listaHijos"].append(a)
            dicho = f"as the new parent of [{a.get('idCarta')}] {self.titulo(a)}"
        return nuevo_id, dicho

    def update(self, n, titulo, contenido, agregar, color, mover_a):
        cambios = []
        if titulo is not None:
            n["tituloCarta"] = titulo
            cambios.append("title")
        if contenido is not None:
            n["descripcion"] = a_html(contenido)
            cambios.append("text")
        if agregar:
            n["descripcion"] = (n.get("descripcion") or "") + a_html(agregar)
            cambios.append("appended text")
        if color is not None:
            n["color"] = None if color == "" else color
            cambios.append("color")
        if mover_a is not None:
            destino = self.nodo(mover_a)
            if n is self.raiz:
                raise MemoryToolError("the root cannot be moved.")
            x = destino
            while x is not None:
                if x is n:
                    raise MemoryToolError("that would put the card inside its own branch.")
                x = self.padre.get(x.get("idCarta"))
            viejo = self.padre[n.get("idCarta")]
            viejo["listaHijos"].remove(n)
            destino["listaHijos"].append(n)
            n["idPadre"] = destino.get("idCarta")
            cambios.append(f"moved under [{destino.get('idCarta')}] {self.titulo(destino)}")
        return cambios

    def link(self, *_):
        raise MemoryToolError("an organigram is a tree: cards have ONE parent and no free links. "
                          "To relate two cards, move one under the other with "
                          "memory_update(move_under=…), or add a card with memory_add.")

    unlink = link

    def delete(self, n, con_hijos):
        if n is self.raiz:
            raise MemoryToolError("the root cannot be deleted.")
        hijos = n["listaHijos"]
        if hijos and not con_hijos:
            raise MemoryToolError(f"[{n.get('idCarta')}] has {len(hijos)} children. Pass "
                                  "with_children=true to delete the whole branch, or move them first.")
        self.padre[n.get("idCarta")]["listaHijos"].remove(n)


class _Canvas:
    """El Canvas: nodos + flechas (skills.py, diagramind-freestyle)."""

    def __init__(self, obj):
        self.obj = obj
        obj.setdefault("nodos", [])
        obj.setdefault("flechas", [])
        self.por_id = {n.get("id"): n for n in obj["nodos"]}

    # --- lectura ---
    def es_grupo(self, n):
        return n.get("type") in GRUPOS

    def grupo_de(self, n):
        """La membresía es explícita (`data.grupo`); si no está, por geometría, como la
        app (freeCanvas.groupOf / grupoPorGeometria)."""
        if self.es_grupo(n):
            return None
        g = (n.get("data") or {}).get("grupo", "∅")
        if g != "∅":
            return g if g in self.por_id and self.es_grupo(self.por_id[g]) else None
        cx, cy = n.get("x", 0) + n.get("ancho", 0) / 2, n.get("y", 0) + n.get("alto", 0) / 2
        mejor, area = None, float("inf")
        for s in self.obj["nodos"]:
            if self.es_grupo(s) and s["x"] <= cx <= s["x"] + s["ancho"] and s["y"] <= cy <= s["y"] + s["alto"]:
                if s["ancho"] * s["alto"] < area:
                    mejor, area = s.get("id"), s["ancho"] * s["alto"]
        return mejor

    def texto(self, n):
        tipo = n.get("type") or "basic"
        if tipo == "web":
            return f"(web page: {n.get('contenido') or ''})"
        if tipo == "class":
            d = n.get("data") or {}
            return "\n".join(["fields: " + ", ".join(d.get("campos") or []),
                              "methods: " + ", ".join(d.get("metodos") or [])])
        if tipo == "media":
            return f"(media: {(n.get('data') or {}).get('src') or 'empty'})"
        t = a_texto(n.get("contenido"))
        tit = (n.get("titulo") or "").strip()
        # un md abre con su título como <h3>: no repetirlo
        if tit and t.startswith("# " + tit):
            t = t[len("# " + tit):].lstrip("\n ")
        return t

    def titulo(self, n):
        tit = (n.get("titulo") or "").strip()
        if tit:
            return tit
        t = a_texto(n.get("contenido")).lstrip("# ")
        return _corto(t.split("\n")[0], 60) if t else f"(untitled {n.get('type') or 'node'})"

    def nodo(self, i):
        n = self.por_id.get(_entero(i, "id"))
        if n is None:
            raise MemoryToolError(f"there is no node with id {i} in this canvas.")
        return n

    def flechas_de(self, i):
        return [f for f in self.obj["flechas"] if f.get("fromId") == i or f.get("toId") == i]

    def vecinos(self, n):
        out = []
        for f in self.flechas_de(n.get("id")):
            if f.get("fromId") == n.get("id"):
                out.append((f.get("toId"), "out", f.get("label") or ""))
            else:
                out.append((f.get("fromId"), "in", f.get("label") or ""))
        return out

    def todos(self):
        return [n for n in self.obj["nodos"] if not self.es_grupo(n)]

    def id_de(self, n):
        return n.get("id")

    def overview(self, tope):
        nodos = self.todos()
        grupos = [n for n in self.obj["nodos"] if self.es_grupo(n)]
        grado = {n.get("id"): len(self.flechas_de(n.get("id"))) for n in nodos}
        orden = sorted(nodos, key=lambda n: (-grado[n.get("id")], n.get("id")))
        lineas = [f"Canvas — {len(nodos)} nodes, {len(self.obj['flechas'])} arrows, {len(grupos)} groups. "
                  "Most connected first: those are the hubs. [id] title · links · (group). "
                  "Read nodes with memory_read(ids); find them with memory_search."]
        for n in orden[:tope]:
            g = self.grupo_de(n)
            en = f" · in [{g}] {self.titulo(self.por_id[g])}" if g is not None else ""
            lineas.append(f"[{n.get('id')}] {self.titulo(n)} · {grado[n.get('id')]} links{en}")
        if len(orden) > tope:
            lineas.append(f"… {len(orden) - tope} less-connected nodes not shown: memory_search "
                          "finds any node, or raise max_nodes.")
        if grupos:
            lineas.append("groups: " + " · ".join(
                f"[{g.get('id')}] {self.titulo(g)} ({sum(1 for n in nodos if self.grupo_de(n) == g.get('id'))} nodes)"
                for g in grupos))
        return "\n".join(lineas)

    def ficha(self, n, con_texto=True):
        g = self.grupo_de(n)
        tipo = n.get("type") or "basic"
        cab = f"[{n.get('id')}] {self.titulo(n)}  ({tipo}" + (f", in group [{g}] {self.titulo(self.por_id[g])}" if g is not None else "") + ")"
        partes = [cab]
        if con_texto:
            t = self.texto(n)
            partes.append(t if t else "(no text yet)")
        sal = [(v, l) for v, d, l in self.vecinos(n) if d == "out"]
        ent = [(v, l) for v, d, l in self.vecinos(n) if d == "in"]
        fmt = lambda xs: " · ".join(f"[{v}] {self.titulo(self.por_id[v])}" + (f' "{l}"' if l else "")
                                    for v, l in xs if v in self.por_id)
        if sal:
            partes.append("→ points to: " + fmt(sal))
        if ent:
            partes.append("← pointed from: " + fmt(ent))
        if self.es_grupo(n):
            miembros = [m for m in self.todos() if self.grupo_de(m) == n.get("id")]
            if miembros:
                partes.append("contains: " + " · ".join(f"[{m.get('id')}] {self.titulo(m)}" for m in miembros))
        return "\n".join(partes)

    # --- escritura ---
    def _nuevo_id(self, campo, lista):
        v = max(int(self.obj.get(campo) or 0), max((int(x.get("id") or 0) for x in lista), default=0)) + 1
        self.obj[campo] = v
        return v

    def _alto(self, html_):
        t = a_texto(html_)
        lineas = sum(max(1, -(-len(l) // 38)) for l in t.split("\n")) if t else 1
        return max(120, min(640, 70 + lineas * 26))

    def _choca(self, x, y, w, h, propio):
        for n in self.obj["nodos"]:
            if self.es_grupo(n):
                if n.get("id") == propio:
                    continue
                if x < n["x"] + n["ancho"] and n["x"] < x + w and y < n["y"] + n["alto"] and n["y"] < y + h:
                    return True
                continue
            if (x < n["x"] + n["ancho"] + NODE_GAP and n["x"] < x + w + NODE_GAP and
                    y < n["y"] + n["alto"] + NODE_GAP and n["y"] < y + h + NODE_GAP):
                return True
        return False

    def _lugar(self, x0, y0, w, h, propio):
        """El hueco libre más cercano a (x0, y0): baja y sube por la columna, y si la
        columna está llena prueba la de al lado. Nunca encima de otro nodo."""
        paso = 40
        for col in range(0, 12):
            x = x0 + col * (w + LAYER_GAP)
            for k in range(0, 60):
                for y in ((y0 + k * paso,) if k == 0 else (y0 + k * paso, y0 - k * paso)):
                    if not self._choca(x, y, w, h, propio):
                        return round(x), round(y)
        return round(x0), round(y0 + 60 * paso)

    def _lados(self, a, b):
        """La cara que mira al otro nodo (como autoLayout.sidesFacing)."""
        ca = (a["x"] + a["ancho"] / 2, a["y"] + a["alto"] / 2)
        cb = (b["x"] + b["ancho"] / 2, b["y"] + b["alto"] / 2)
        sep_x = b["x"] >= a["x"] + a["ancho"] or a["x"] >= b["x"] + b["ancho"]
        sep_y = b["y"] >= a["y"] + a["alto"] or a["y"] >= b["y"] + b["alto"]
        if sep_x and (not sep_y or abs(cb[0] - ca[0]) >= abs(cb[1] - ca[1])):
            return ("right", "left") if cb[0] >= ca[0] else ("left", "right")
        return ("bottom", "top") if cb[1] >= ca[1] else ("top", "bottom")

    def _flecha(self, desde, hasta, etiqueta):
        if any(f.get("fromId") == desde and f.get("toId") == hasta and f.get("attrId") is None
               for f in self.obj["flechas"]):
            return None
        a, b = self.por_id[desde], self.por_id[hasta]
        s1, s2 = self._lados(a, b)
        f = {"id": self._nuevo_id("lastArrowId", self.obj["flechas"]), "fromId": desde, "toId": hasta,
             "fromSide": s1, "toSide": s2, "label": etiqueta or "", "color": None,
             "kind": None, "attrId": None}
        self.obj["flechas"].append(f)
        return f

    def _envolver(self, gid, n):
        """Si el nodo nuevo cae fuera de su sector, el sector crece hasta contenerlo."""
        g = self.por_id.get(gid)
        if g is None:
            return
        x1 = min(g["x"], n["x"] - PAD_GRUPO)
        y1 = min(g["y"], n["y"] - PAD_GRUPO)
        x2 = max(g["x"] + g["ancho"], n["x"] + n["ancho"] + PAD_GRUPO)
        y2 = max(g["y"] + g["alto"], n["y"] + n["alto"] + PAD_GRUPO)
        g["x"], g["y"], g["ancho"], g["alto"] = x1, y1, x2 - x1, y2 - y1

    def add(self, titulo, contenido, ancla, relacion, etiqueta, color):
        cuerpo = (f"<h3>{html.escape(titulo, quote=False)}</h3>" if titulo else "") + a_html(contenido)
        w, h = MD_ANCHO, self._alto(cuerpo)
        a = None if ancla is None else self.nodo(ancla)
        if a is not None and self.es_grupo(a):
            # agregar "en" un sector: el nodo va adentro, sin flechas
            gid = a.get("id")
            x0, y0 = a["x"] + PAD_GRUPO, a["y"] + PAD_GRUPO + 40
            enlaces, dicho = [], f"inside the group [{gid}] {self.titulo(a)}"
        elif a is None:
            gid = None
            xs = [n["x"] + n["ancho"] for n in self.obj["nodos"]] or [0]
            ys = [n["y"] for n in self.obj["nodos"]] or [0]
            x0, y0 = max(xs) + LAYER_GAP, min(ys)
            enlaces, dicho = [], "on its own (no anchor, no arrows)"
        else:
            gid = self.grupo_de(a)
            if relacion == "child":
                x0, y0 = a["x"] + a["ancho"] + LAYER_GAP, a["y"]
                enlaces = [(a.get("id"), None)]
                dicho = f"pointed from [{a.get('id')}] {self.titulo(a)}"
            elif relacion == "parent":
                x0, y0 = a["x"] - LAYER_GAP - w, a["y"]
                enlaces = [(None, a.get("id"))]
                dicho = f"pointing to [{a.get('id')}] {self.titulo(a)}"
            else:   # sibling: lo mismo que apunta al ancla, apunta también al nuevo
                x0, y0 = a["x"], a["y"] + a["alto"] + NODE_GAP
                padres = [(f.get("fromId"), f.get("label") or "") for f in self.obj["flechas"]
                          if f.get("toId") == a.get("id")]
                enlaces = [(p, None, lab) for p, lab in padres]
                dicho = (f"as a sibling of [{a.get('id')}] {self.titulo(a)}, pointed from the same "
                         f"{len(padres)} node(s)" if padres else
                         f"next to [{a.get('id')}] {self.titulo(a)} — it has nothing pointing to it, so "
                         "the new node is NOT connected yet (memory_link it if it should be)")
        x, y = self._lugar(x0, y0, w, h, gid)
        nid = self._nuevo_id("lastIdCharged", self.obj["nodos"])
        n = {"id": nid, "x": x, "y": y, "ancho": w, "alto": h, "titulo": titulo, "contenido": cuerpo,
             "color": color, "type": "md", "data": {"grupo": gid}}
        self.obj["nodos"].append(n)
        self.por_id[nid] = n
        if gid is not None:
            self._envolver(gid, n)
        for e in enlaces:
            desde, hasta = e[0], e[1]
            lab = e[2] if len(e) > 2 and not etiqueta else etiqueta
            self._flecha(desde if desde is not None else nid, hasta if hasta is not None else nid, lab)
        return nid, dicho

    def update(self, n, titulo, contenido, agregar, color, mover_a):
        if mover_a is not None:
            raise MemoryToolError("`move_under` is for organigram cards. On the canvas, relate "
                              "nodes with memory_link / memory_unlink.")
        tipo = n.get("type") or "basic"
        cambios = []
        if (contenido is not None or agregar) and tipo not in ("md", "basic", "grupo"):
            raise MemoryToolError(f"[{n.get('id')}] is a `{tipo}` node: its content is not text. "
                              "Change it with read_diagram / write_diagram.")
        if (contenido is not None or agregar) and tipo == "basic":
            # un basic NO dibuja su contenido (skills.py): pasa a ser una tarjeta md, que sí
            n["type"] = "md"
            tipo = "md"
            n["ancho"] = max(n.get("ancho") or 0, MD_ANCHO)
            cambios.append("became a text card (md)")
        tit = (n.get("titulo") or "").strip()
        if titulo is not None:
            viejo = tit
            n["titulo"] = titulo
            if tipo == "md" and viejo and (n.get("contenido") or "").startswith(f"<h3>{html.escape(viejo, quote=False)}</h3>"):
                n["contenido"] = f"<h3>{html.escape(titulo, quote=False)}</h3>" + n["contenido"][len(f"<h3>{html.escape(viejo, quote=False)}</h3>"):]
            cambios.append("title")
            tit = titulo
        encabezado = f"<h3>{html.escape(tit, quote=False)}</h3>" if (tipo == "md" and tit) else ""
        if contenido is not None:
            n["contenido"] = encabezado + a_html(contenido)
            cambios.append("text")
        if agregar:
            base = n.get("contenido") or ""
            if tipo == "md" and encabezado and not base.startswith(encabezado):
                base = encabezado + base
            n["contenido"] = base + a_html(agregar)
            cambios.append("appended text")
        if tipo == "md" and ("text" in cambios or "appended text" in cambios):
            n["alto"] = max(n.get("alto") or 0, self._alto(n["contenido"]))
        if color is not None:
            n["color"] = None if color == "" else color
            cambios.append("color")
        return cambios

    def link(self, desde, hasta, etiqueta):
        a, b = self.nodo(desde), self.nodo(hasta)
        if a is b:
            raise MemoryToolError("a node cannot point to itself.")
        if self.es_grupo(a) or self.es_grupo(b):
            raise MemoryToolError("groups are not arrow targets: link the real nodes inside them.")
        f = self._flecha(a.get("id"), b.get("id"), etiqueta)
        if f is None:
            return f"[{a.get('id')}] already points to [{b.get('id')}]; nothing changed."
        return f"linked [{a.get('id')}] {self.titulo(a)} → [{b.get('id')}] {self.titulo(b)}" + (f' "{etiqueta}"' if etiqueta else "")

    def unlink(self, desde, hasta, _etiqueta=None):
        a, b = self.nodo(desde), self.nodo(hasta)
        antes = len(self.obj["flechas"])
        self.obj["flechas"] = [f for f in self.obj["flechas"]
                               if not (f.get("fromId") == a.get("id") and f.get("toId") == b.get("id"))]
        if len(self.obj["flechas"]) == antes:
            al_reves = any(f.get("fromId") == b.get("id") and f.get("toId") == a.get("id") for f in self.obj["flechas"])
            raise MemoryToolError(f"[{a.get('id')}] does not point to [{b.get('id')}]" +
                              (" — the arrow goes the other way: swap `from` and `to`." if al_reves else "."))
        return f"unlinked [{a.get('id')}] → [{b.get('id')}]"

    def delete(self, n, _con_hijos):
        i = n.get("id")
        self.obj["nodos"] = [m for m in self.obj["nodos"] if m.get("id") != i]
        self.obj["flechas"] = [f for f in self.obj["flechas"] if f.get("fromId") != i and f.get("toId") != i]
        if self.es_grupo(n):
            for m in self.obj["nodos"]:
                if (m.get("data") or {}).get("grupo") == i:
                    m["data"]["grupo"] = None
        return 1


def _modo(obj):
    tipo = obj.get("type")
    if tipo == "cart":
        return _Cart(obj)
    if tipo == "freestyle":
        return _Canvas(obj)
    raise MemoryToolError(f"the memory tools work on organigrams (cart) and canvases (freestyle); "
                      f"this diagram is `{tipo}`. Use read_diagram / write_diagram for it.")


# ---------------------------------------------------------------------------------
# las operaciones (lo que llama diagram_mcp): lectura → texto; escritura → (texto, obj)

def overview(obj, max_nodes=300):
    return _modo(obj).overview(max(10, min(int(max_nodes or 300), 2000)))


def search(obj, query, limit=15):
    m = _modo(obj)
    q = [t for t in _plano(query).split() if t]
    if not q:
        raise MemoryToolError("`query` is empty: pass a few words to look for.")
    filas = []
    for n in m.todos():
        tit, txt = _plano(m.titulo(n)), _plano(m.texto(n))
        pts = sum(3 * tit.count(t) + txt.count(t) for t in q)
        if all(t in tit or t in txt for t in q):
            pts += 10                          # los que tienen TODAS las palabras, primero
        if pts:
            filas.append((pts, n))
    filas.sort(key=lambda x: (-x[0], m.id_de(x[1])))
    if not filas:
        return f'nothing matches "{query}". memory_overview shows everything there is.'
    out = [f'{len(filas)} match(es) for "{query}", best first:']
    for _, n in filas[: max(1, min(int(limit or 15), 100))]:
        txt = m.texto(n)
        pos = min((i for i in (_plano(txt).find(t) for t in q) if i >= 0), default=0)
        frag = _corto(txt[max(0, pos - 30): pos + 90], 110) if txt else ""
        out.append(f"[{m.id_de(n)}] {m.titulo(n)}" + (f" — {frag}" if frag else ""))
    return "\n".join(out)


def read(obj, ids, depth=0):
    m = _modo(obj)
    if not isinstance(ids, list):
        ids = [ids]
    if not ids:
        raise MemoryToolError("`ids` is empty: pass the node ids to read (memory_overview lists them).")
    depth = max(0, min(int(depth or 0), 3))
    vistos, ya, frontera = [], set(), [m.nodo(i) for i in ids]
    for _ in range(depth + 1):
        siguiente = []
        for n in frontera:
            if m.id_de(n) in ya or len(vistos) >= MAX_LEER:
                continue
            vistos.append(n)
            ya.add(m.id_de(n))
            for v, *_ in m.vecinos(n):
                if v in m.por_id and v not in ya:
                    siguiente.append(m.por_id[v])
        frontera = siguiente
    texto = "\n\n".join(m.ficha(n) for n in vistos)
    if len(vistos) >= MAX_LEER:
        texto += f"\n\n(stopped at {MAX_LEER} nodes: read the rest by id.)"
    return texto


def _escribiendo(obj):
    obj = json.loads(json.dumps(obj))           # se trabaja sobre una copia: si falla, nada cambió
    return obj, _modo(obj)


def add(obj, title, content="", anchor=None, relation="child", label="", color=None):
    title = (title or "").strip()
    if not title:
        raise MemoryToolError("`title` is required: a short name for what this node remembers.")
    relation = (relation or "child").strip().lower()
    if relation not in ("child", "sibling", "parent"):
        raise MemoryToolError("`relation` is child, sibling or parent (relative to `anchor`).")
    obj, m = _escribiendo(obj)
    nid, dicho = m.add(title, content or "", None if anchor in (None, "") else _entero(anchor, "anchor"),
                       relation, (label or "").strip(), _color(color))
    return f"added [{nid}] {title}, {dicho}.", obj


def update(obj, id, title=None, content=None, append=None, color=None, move_under=None):
    obj, m = _escribiendo(obj)
    n = m.nodo(id)
    if color is not None and color != "":
        color = _color(color)
    cambios = m.update(n, title if title is None else str(title).strip(), content, append,
                       color, None if move_under in (None, "") else move_under)
    if not cambios:
        raise MemoryToolError("nothing to change: pass title, content, append, color or move_under.")
    return f"updated [{m.id_de(n)}] {m.titulo(n)}: {', '.join(cambios)}.", obj


def link(obj, from_id, to_id, label=""):
    obj, m = _escribiendo(obj)
    return m.link(_entero(from_id, "from"), _entero(to_id, "to"), (label or "").strip()), obj


def unlink(obj, from_id, to_id):
    obj, m = _escribiendo(obj)
    return m.unlink(_entero(from_id, "from"), _entero(to_id, "to")), obj


def delete(obj, id, with_children=False):
    obj, m = _escribiendo(obj)
    n = m.nodo(id)
    tit = m.titulo(n)
    m.delete(n, bool(with_children))
    return f"deleted [{m.id_de(n)}] {tit}" + (" and its branch" if with_children else "") + ".", obj
