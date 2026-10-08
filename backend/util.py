"""Utilidades chicas compartidas por el backend local."""
import os
import ssl

# Bundles de CAs del sistema, en el orden en que se prueban. El primero es el de
# macOS (y de varios BSD); los otros, Debian/Ubuntu, Fedora/RHEL, openSUSE y Alpine.
_CA_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
               "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
               "/etc/ssl/certs/ca-bundle.crt")


def ensure_ca_bundle():
    """Que el HTTPS del backend tenga raíces contra las que verificar. Se llama UNA vez,
    al arrancar, y vale para todo `urlopen` del proceso (y de sus hijos).

    El bug (2026-10-08): el Python de python.org en macOS NO trae certificados hasta
    que alguien corre su `Install Certificates.command`. Corriendo el backend desde el
    código con ese Python, el contexto TLS por defecto arranca con CERO raíces y TODO
    HTTPS falla con `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
    certificate`: los fetch del modo Object, las APIs del orquestador, el updater. Los
    binarios no lo sufren porque traen `certifi`. No se ve en una terminal que ya
    exporta SSL_CERT_FILE, que es justo como se escapó.

    Por qué acá y no en cada llamada: había UNA que lo resolvía (updater) y siete que
    no. Repetirlo a mano es garantizar que la próxima se olvide (la regla de procs.run).
    Y NO se desactiva la verificación: se le dan raíces, que es lo que faltaba.
    Devuelve el bundle que puso, o None si no hizo falta tocar nada."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None                                   # alguien ya decidió: respetarlo
    try:
        if ssl.create_default_context().cert_store_stats().get("x509_ca"):
            return None                               # ya tiene raíces (Windows, Linux, brew)
    except Exception:
        pass
    candidatos = []
    try:
        import certifi                                # en el binario lo trae PyInstaller
        candidatos.append(certifi.where())
    except Exception:
        pass
    for ruta in candidatos + list(_CA_BUNDLES):
        if os.path.isfile(ruta):
            os.environ["SSL_CERT_FILE"] = ruta
            return ruta
    return None


def safe_name(name):
    # nombre de carpeta → dir seguro (legible). Permite espacios.
    s = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
    return s or "default"


def safe_file_name(name):
    # nombre de ARCHIVO seguro: a diferencia de safe_name, conserva el punto de la
    # extensión (foto.png). Se queda solo con el último segmento de la ruta (sin
    # separadores) para que un adjunto no pueda escribir fuera del temp.
    base = str(name).replace("\\", "/").split("/")[-1]
    s = "".join(c for c in base if c.isalnum() or c in "-_. ").strip()
    s = s.lstrip(".")          # nada de archivos ocultos / nombres vacíos
    return s or "archivo"
