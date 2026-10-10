"""`/folders/reveal`: QUÉ carpeta abre (bug 2026-10-10).

El «Open root folder» de la web abría la carpeta ACTIVA; después, `projects/`. Lo que
espera el usuario es «la raíz de donde guarda todo DiagraMinder»: la carpeta de datos
(`app_dir()`, `…/DiagraMind/`). La web lo pide con `app: true`; sin eso el endpoint
sigue como antes, porque el panel local muestra y abre `projects_dir()`.

No levanta el server: llama al handler con `reveal_in_explorer` reemplazado, así el test
no abre ventanas del Finder/Explorer de verdad.

    python3 backend/tests/test_folders_reveal.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

home = tempfile.mkdtemp(prefix="dmreveal-")
os.environ["HOME"] = home
os.environ["LOCALAPPDATA"] = home
os.environ["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")

import server  # noqa: E402

ok = fail = 0


def check(nombre, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {nombre}")
    else:
        fail += 1
        print(f"  ❌ {nombre}" + (f" — {extra}" if extra else ""))


abiertas = []
server.reveal_in_explorer = lambda p: abiertas.append(p) or True


class Fake:
    def _json(self, code, obj):
        self.out = (code, obj)


def reveal(body):
    f = Fake()
    server.Handler._folders_reveal(f, body)
    return f.out[1]


print("\n▶ /folders/reveal abre la carpeta que corresponde\n")
app = server.app_dir()
check("la carpeta de datos cae en el HOME del test", app.startswith(home), app)

r = reveal({"app": True})
check("app: true → la carpeta de datos (…/DiagraMind/)", r["path"] == app, r["path"])
check("y es la que se mandó a abrir", abiertas[-1] == app, abiertas[-1])
check("aunque venga también una carpeta, gana app",
      reveal({"app": True, "folder": "Local"})["path"] == app)
check("sin nada → projects_dir (lo que abre el panel local)",
      reveal({})["path"] == server.projects_dir())
check("con carpeta → esa carpeta",
      reveal({"folder": "Local"})["path"] == server.folder_dir("Local"))

print(f"\n{'✅' if fail == 0 else '❌'} {ok} ok, {fail} fallidos\n")
sys.exit(1 if fail else 0)
