"""OAuth 2.1 para el MCP REMOTO (doc 37 §F19).

Cuando el MCP sale de esta máquina por un túnel, la contraseña del backend deja de
alcanzar: Claude web y ChatGPT no piden "pegá un token", piden **conectarse a un
connector**, y para eso hay que hablar el subconjunto de la spec de autorización de
MCP que ellos usan. Esto es ese subconjunto, portado del diseño de `localmcpcoder`
(que es de Juan y hace exactamente esto) a Python stdlib, porque el backend no tiene
dependencias y no las va a tener.

    RFC 9728  Protected Resource Metadata   /.well-known/oauth-protected-resource
    RFC 8414  Authorization Server Metadata /.well-known/oauth-authorization-server
    RFC 7591  Dynamic Client Registration   POST /oauth/register
    OAuth 2.1 + PKCE (S256, obligatorio)    GET/POST /oauth/authorize, POST /oauth/token
    RFC 8707  Resource Indicators           los access token van atados a su audiencia
    RFC 9207  Issuer identification         `iss` en la respuesta de autorización

**El secreto NUNCA viaja en una URL.** Va en el header `Authorization`, porque una URL
termina escrita en el log de cada proxy que atraviesa — y un túnel público son varios.

El cliente se registra solo: no hay client_id ni secret que copiar a mano. El único
paso humano es tipear la contraseña en la pantalla de consentimiento, una vez.

Todo el estado vive en MEMORIA a propósito: reiniciar el backend invalida cada token
emitido. Para una devtool local eso es una feature — apagar y prender es una forma
entendible de "revocar todo".
"""
import base64
import hashlib
import hmac
import html
import os
import secrets
import time

ACCESS_TTL = 3600        # 1 hora
CODE_TTL = 60            # 1 minuto: el código se canjea al instante o no sirve
SCOPE = "mcp"

# La contraseña tiene 192 bits de entropía, así que esto no es lo que la protege:
# es para que un bot no llene el log a fuerza de intentos.
MAX_FALLOS = 5
BLOQUEO = 60


def _rand(n=32):
    return secrets.token_hex(n)


def _s256(verifier):
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def _igual(a, b):
    """Comparación de tiempo constante: `==` sobre un secreto filtra por timing."""
    return hmac.compare_digest(str(a or ""), str(b or ""))


class OAuth:
    """El servidor de autorización. Una instancia por proceso."""

    def __init__(self):
        self.clients = {}      # client_id -> metadata
        self.codes = {}        # code      -> grant
        self.access = {}       # token     -> grant
        self.refresh = {}      # token     -> grant
        self.fallos = 0
        self.bloqueado_hasta = 0

    # ---------------------------------------------------------------- limpieza
    def _barrer(self):
        ahora = time.time()
        for d in (self.codes, self.access):
            for k in [k for k, v in d.items() if v.get("expira", 0) < ahora]:
                del d[k]

    # --------------------------------------------------------------- discovery
    def protected_resource(self, origen):
        return {
            "resource": f"{origen}/mcp",
            "authorization_servers": [origen],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }

    def as_metadata(self, origen):
        return {
            "issuer": origen,
            "authorization_endpoint": f"{origen}/oauth/authorize",
            "token_endpoint": f"{origen}/oauth/token",
            "registration_endpoint": f"{origen}/oauth/register",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }

    # ------------------------------------------------------------- registro
    def registrar(self, body):
        """(status, json). El cliente se inventa su propio id: es público y lo que
        lo protege es PKCE, no un secreto compartido."""
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return 400, {"error": "invalid_redirect_uri",
                         "error_description": "redirect_uris is required"}
        cid = _rand(16)
        cli = {
            "client_id": cid,
            "client_name": body.get("client_name") or "Unnamed MCP client",
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
        }
        self.clients[cid] = cli
        return 201, dict(cli, client_id_issued_at=int(time.time()))

    # --------------------------------------------------------- authorize
    def leer_authz(self, q):
        """(fatal, ctx). `fatal` = no se puede ni redirigir el error (sería un
        open redirect): se muestra en pantalla."""
        cli = self.clients.get(q.get("client_id"))
        if not cli:
            return "unknown client_id — register first", None
        ru = q.get("redirect_uri")
        if not ru or ru not in cli["redirect_uris"]:
            return "redirect_uri does not match the one registered", None
        if q.get("response_type") != "code":
            return None, {"client": cli, "oauth_error": "unsupported_response_type"}
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return None, {"client": cli, "oauth_error": "invalid_request"}
        return None, {"client": cli}

    def aprobar(self, q, passphrase, origen):
        """La contraseña es correcta → (code, None). Si no → (None, mensaje)."""
        ahora = time.time()
        if ahora < self.bloqueado_hasta:
            return None, (f"Demasiados intentos fallidos. Probá de nuevo en "
                          f"{int(self.bloqueado_hasta - ahora) + 1}s.")
        if not _igual(q.get("passphrase"), passphrase):
            self.fallos += 1
            if self.fallos >= MAX_FALLOS:
                self.bloqueado_hasta = ahora + BLOQUEO
                self.fallos = 0
            return None, "Contraseña incorrecta."
        self.fallos = 0
        code = _rand(24)
        self.codes[code] = {
            "client_id": q.get("client_id"),
            "redirect_uri": q.get("redirect_uri"),
            "code_challenge": q.get("code_challenge"),
            "resource": q.get("resource") or f"{origen}/mcp",
            "scope": q.get("scope") or SCOPE,
            "expira": ahora + CODE_TTL,
        }
        return code, None

    # ------------------------------------------------------------- token
    def _emitir(self, grant):
        at, rt = _rand(), _rand()
        self.access[at] = dict(grant, expira=time.time() + ACCESS_TTL)
        self.refresh[rt] = grant
        return {"access_token": at, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": rt, "scope": grant.get("scope", SCOPE)}

    def token(self, b):
        self._barrer()
        tipo = b.get("grant_type")
        if tipo == "authorization_code":
            # De un solo uso: se borra aunque el canje falle. Un código que se puede
            # reintentar es un código que se puede robar y reusar.
            grant = self.codes.pop(b.get("code"), None)
            if not grant or grant["expira"] < time.time():
                return 400, {"error": "invalid_grant"}
            if grant["client_id"] != b.get("client_id") or grant["redirect_uri"] != b.get("redirect_uri"):
                return 400, {"error": "invalid_grant"}
            ver = b.get("code_verifier") or ""
            if not ver or _s256(ver) != grant["code_challenge"]:
                return 400, {"error": "invalid_grant", "error_description": "PKCE check failed"}
            return 200, self._emitir({"client_id": grant["client_id"],
                                      "aud": b.get("resource") or grant["resource"],
                                      "scope": grant["scope"]})
        if tipo == "refresh_token":
            grant = self.refresh.pop(b.get("refresh_token"), None)   # rotación
            if not grant:
                return 400, {"error": "invalid_grant"}
            return 200, self._emitir(dict(grant, aud=b.get("resource") or grant.get("aud")))
        return 400, {"error": "unsupported_grant_type"}

    # --------------------------------------------------- resource server
    def verificar(self, header, passphrase, origen):
        """(ok, error, descripcion). El bearer puede ser un access token emitido acá
        o —atajo para clientes de línea de comandos— la contraseña misma."""
        self._barrer()
        if not header or not header.startswith("Bearer "):
            return False, None, None
        presentado = header[7:].strip()
        if _igual(presentado, passphrase):
            return True, None, None
        grant = self.access.get(presentado)
        if not grant:
            return False, "invalid_token", None
        if grant["expira"] < time.time():
            self.access.pop(presentado, None)
            return False, "invalid_token", "Token expired"
        # RFC 8707: un token emitido para OTRO recurso no sirve acá, aunque venga del
        # mismo servidor de autorización.
        esperado = f"{origen}/mcp"
        if grant.get("aud") and grant["aud"] not in (esperado, origen):
            return False, "invalid_token", "Token audience mismatch"
        return True, None, None

    def challenge_header(self, origen, error=None, descripcion=None):
        partes = [f'Bearer resource_metadata="{origen}/.well-known/oauth-protected-resource"']
        if error:
            partes.append(f'error="{error}"')
        partes.append(f'scope="{SCOPE}"')
        if descripcion:
            partes.append(f'error_description="{descripcion}"')
        return ", ".join(partes)


# ===================== la pantalla de consentimiento =====================

def consent_page(origen, nombre_cliente, params, error=None, nivel="", carpeta=""):
    """Lo único que ve una persona en todo el flujo. Tiene que decir QUÉ va a poder
    hacer el que entra — no "autorizar aplicación", que no informa nada."""
    e = html.escape
    ocultos = "".join(
        f'<input type="hidden" name="{e(k)}" value="{e(str(v))}">'
        for k, v in params.items() if v is not None)
    aviso = f'<p class="err">{e(error)}</p>' if error else ""
    detalle = f"<li>Nivel de acceso: <b>{e(nivel)}</b></li>" if nivel else ""
    if carpeta:
        detalle += f"<li>Carpeta que va a poder tocar: <code>{e(carpeta)}</code></li>"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conectar con DiagraMinder</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#14161c; color:#e8eaf0;
         font:400 15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif; }}
  .card {{ width:min(440px,92vw); padding:26px 28px; background:#1b1e25;
           border:1px solid #2a2e38; border-radius:14px; }}
  h1 {{ margin:0 0 4px; font-size:19px; }}
  p.sub {{ margin:0 0 18px; color:#9aa3b2; font-size:13.5px; }}
  ul {{ margin:0 0 18px; padding-left:18px; color:#c7cde0; font-size:13.5px; }}
  li {{ margin:3px 0; }}
  code {{ background:#0f1116; padding:1px 5px; border-radius:4px; font-size:12.5px; }}
  label {{ display:block; margin-bottom:6px; font-size:13px; color:#9aa3b2; }}
  input[type=password] {{ width:100%; box-sizing:border-box; padding:10px 12px;
      background:#0f1116; border:1px solid #2a2e38; border-radius:8px; color:#e8eaf0;
      font:400 14px ui-monospace,SFMono-Regular,Menlo,monospace; }}
  button {{ width:100%; margin-top:14px; padding:11px; border:0; border-radius:8px;
            background:#4f7bf0; color:#fff; font:600 14px system-ui; cursor:pointer; }}
  button:hover {{ background:#3f68d8; }}
  .err {{ margin:0 0 14px; padding:9px 11px; background:#3a1c1f; border:1px solid #6b2b30;
          border-radius:8px; color:#ffb4b4; font-size:13px; }}
  .foot {{ margin:16px 0 0; color:#6f7787; font-size:12px; }}
</style></head><body>
<div class="card">
  <h1>Conectar con DiagraMinder</h1>
  <p class="sub"><b>{e(nombre_cliente)}</b> quiere acceder a esta máquina.</p>
  {aviso}
  <ul>
    <li>Va a poder leer y escribir tus <b>diagramas</b>.</li>
    {detalle}
  </ul>
  <form method="POST" action="/oauth/authorize">
    {ocultos}
    <label for="p">Contraseña del backend (está en el panel, y en token.txt)</label>
    <input id="p" name="passphrase" type="password" autocomplete="off" autofocus required>
    <button type="submit">Autorizar</button>
  </form>
  <p class="foot">Si no pediste esto, cerrá la pestaña y no escribas nada.</p>
</div></body></html>"""
