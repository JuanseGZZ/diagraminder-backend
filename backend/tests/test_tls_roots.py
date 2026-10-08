"""El HTTPS del backend tiene raíces aunque el Python no traiga ninguna (2026-10-08).

El Python de python.org en macOS arranca SIN certificados hasta que se corre su
`Install Certificates.command`. Corriendo el backend desde el código, todo HTTPS
fallaba con `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` (lo
vio el usuario en un fetch del modo Object). No se reproducía en una terminal que ya
exportaba SSL_CERT_FILE: por eso este test la BORRA del entorno antes de probar.

    python3 backend/tests/test_tls_roots.py

El bloque B usa la red (un GET a un HTTPS público); sin red se saltea, no falla.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import shutil
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


def sin_certs_env(**extra):
    """El entorno de quien abre una terminal pelada: sin SSL_CERT_FILE/DIR."""
    env = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
    env.update(extra)
    return env


def py(code, env):
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env,
                       capture_output=True, text=True, timeout=30)
    return (r.stdout or "").strip(), (r.stderr or "").strip()


print("\n### A. ensure_ca_bundle deja raíces, y no pisa lo que ya estaba")
out, err = py("import ssl,util; util.ensure_ca_bundle(); "
              "print(ssl.create_default_context().cert_store_stats()['x509_ca'])", sin_certs_env())
check("después de ensure_ca_bundle el contexto por defecto TIENE raíces",
      out.isdigit() and int(out) > 0, out or err[-200:])

out, _ = py("import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])", sin_certs_env())
print(f"     (este Python, sin ayuda, arranca con {out} raíces)")

out, err = py("import os,util; print(util.ensure_ca_bundle()); print(os.environ.get('SSL_CERT_FILE'))",
              sin_certs_env(SSL_CERT_FILE="/ruta/elegida/por/el/usuario.pem"))
check("si el usuario ya puso SSL_CERT_FILE, no se toca",
      out.splitlines() == ["None", "/ruta/elegida/por/el/usuario.pem"], out or err[-200:])

out, err = py("import util,inspect; print('check_hostname' in inspect.getsource(util) or 'CERT_NONE' in inspect.getsource(util))",
              sin_certs_env())
check("y NUNCA se desactiva la verificación (ni CERT_NONE ni check_hostname)", out == "False", out or err)


print("\n### B. el /fetch del modo Object, contra el backend real, sin SSL_CERT_FILE")


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


try:
    socket.create_connection(("example.com", 443), timeout=5).close()
    hay_red = True
except OSError:
    hay_red = False

if not hay_red:
    print("  ⏭  sin red: se saltea")
else:
    home = tempfile.mkdtemp(prefix="dmtls-")
    port = puerto_libre()
    srv = subprocess.Popen([sys.executable, SERVER, "--port", str(port), "--no-ui"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=sin_certs_env(HOME=home, USERPROFILE=home, LOCALAPPDATA=home))
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        tok = ""
        for raiz, _, archivos in os.walk(home):
            if "token.txt" in archivos:
                tok = open(os.path.join(raiz, "token.txt")).read().strip()
                break

        def fetch(url):
            req = urllib.request.Request(f"{base}/fetch?token={tok}", method="POST",
                                         data=json.dumps({"url": url, "method": "GET"}).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=40).read())

        r = fetch("https://example.com/")
        check("un HTTPS público responde (antes: CERTIFICATE_VERIFY_FAILED)",
              r.get("ok") and r.get("status") == 200, json.dumps(r)[:200])
        r = fetch("https://expired.badssl.com/")
        check("…y un certificado VENCIDO sigue rechazándose (se verifica de verdad)",
              not r.get("ok") and "CERTIFICATE_VERIFY_FAILED" in (r.get("error") or ""), json.dumps(r)[:200])
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        shutil.rmtree(home, ignore_errors=True)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)
