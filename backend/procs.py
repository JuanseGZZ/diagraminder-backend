"""Lanzar procesos sin que Windows abra una consola (bug 2026-09-21).

En Windows, un proceso hijo lanzado desde una app **sin consola** (el binario va con
`--noconsole` desde F17b) se crea una ventana propia: una `cmd` negra que aparece y
desaparece. Con un `--version` suelto es un parpadeo; con el backend probando cuatro
CLIs cada vez que alguien pide `/health`, es una consola prendiéndose y apagándose sin
parar — que es exactamente lo que se vio.

El flag que lo evita es `CREATE_NO_WINDOW`, y estaba puesto en **una sola** de las
dieciséis llamadas a subprocess del backend. Poner el flag en cada lugar es garantizar
que el próximo se olvide, así que vive acá: `run()` y `popen()` son las que se usan, y
no hay que acordarse de nada.

En macOS y Linux no hace nada: el flag no existe y `kw` queda vacío.
"""
import os
import subprocess

# `CREATE_NO_WINDOW` solo existe en Windows; en el resto el dict va vacío.
SIN_CONSOLA = ({"creationflags": subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else {})


def run(*args, **kw):
    """`subprocess.run` que no abre consola. Mismo contrato."""
    return subprocess.run(*args, **{**SIN_CONSOLA, **kw})


def popen(*args, **kw):
    """`subprocess.Popen` que no abre consola. Mismo contrato.

    Importa más que en `run`: un proceso LARGO (el túnel, un CLI de agente) no deja
    una ventana parpadeando sino una ventana ABIERTA todo el tiempo que dure.
    """
    return subprocess.Popen(*args, **{**SIN_CONSOLA, **kw})
