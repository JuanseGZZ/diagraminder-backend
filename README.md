# DiagraMinder — backend

The program that runs behind [DiagraMinder](https://diagraminder.com) on your own
machine. It is what makes the AI part work:

- **Keeps your projects on disk** and mirrors them with the app, live.
- **Runs the AI you already have installed** — Claude Code, Antigravity — so the
  agents cost you nothing per token.
- **Exposes your diagrams over MCP**, so Claude Code can read them before touching
  your code and write back what it did.

Everything stays local. There is no account and nothing is sent anywhere.

> **Just want the whole thing in one file?** The app and this backend are also
> published together as a single executable:
> **[diagraminder-app](https://github.com/JuanseGZZ/diagraminder-app)**. That is the
> easy path. This repo is for running the backend **next to the website** — or for
> reading the code before you run it, which is why it is public.

---

## Run it

### From source, with Python

**No dependencies to install** — it is plain standard library.

```bash
git clone https://github.com/JuanseGZZ/diagraminder-backend.git
cd diagraminder-backend
python3 backend/server.py
```

Then open **https://diagraminder.com** and, in **Settings**, click **Connect backend**.
The website will ask for a password the first time: that is the access token, printed
on startup and stored in `token.txt` inside the data folder (the path is printed too).

**Requirements:** Python 3.10 or newer. On Linux you may also need `python3-tk`
(`sudo apt install python3-tk`) — it is what draws the "choose a folder" dialog.

### Or download a binary

From **[Releases](../../releases/latest)**: `DiagraMinder-Backend-win.exe`,
`DiagraMinder-Backend-mac`, `DiagraMinder-Backend-linux`. No Python needed.

There are also installers (`Instalar-DiagraMinder-Backend-*`) that put it in place and
start it with your session, and `diagraminder-backend.zip` with the plain scripts.

---

## Connect Claude Code to your diagrams

**Open the control panel** (the window this program opens, or `http://127.0.0.1:8765/panel`).
Under **MCP** it shows the exact `.mcp.json` for your machine — address and password
already filled in — with a button to copy it.

Or, on the terminal:

```bash
python3 backend/server.py --mcp-config
```

Either way you get this. `DMD_URL` is where Claude Code will talk to the program, and
`DMD_TOKEN` is this program's password:

```json
{
  "mcpServers": {
    "diagraminder": {
      "command": "/usr/bin/python3",
      "args": ["/path/to/backend/server.py", "--mcp-diagrams"],
      "env": {
        "DMD_URL": "http://127.0.0.1:8765",
        "DMD_TOKEN": "…this program's password…"
      }
    }
  }
}
```

Paste it into `.mcp.json` in the root of your project. Claude Code can then read every
diagram before touching your code, and write back what it did and what is left —
you see the canvas change live. In Claude Code, `/mcp` shows it connected.

Four tools: `list_diagrams`, `read_diagram`, `diagram_schema`, `write_diagram`.

> The config contains your access token. Treat it like a password: whoever has it can
> read and change your projects.

### What the MCP is allowed to do

It is a switch with three levels, in the control panel under **MCP**. It starts **on**
and at the lowest one:

| Level | What the agent gets |
|---|---|
| **Diagrams only** *(default)* | the four tools above |
| **Diagrams + files** | also read, write, edit, search, version and git — **only inside a folder you pick** |
| **Diagrams + files + commands** | also run commands |

Asking for a file level without picking a folder does **not** open your whole disk: it
falls back to diagrams. The check lives in the backend, not in the page — once the
`.mcp.json` is pasted, the client already has the URL and the token, so a switch in the
UI would not switch anything off.

### Reaching it from Claude web

Claude web runs on Anthropic's servers, so it cannot talk to `127.0.0.1`. The panel can
open a **Cloudflare tunnel** that gives you a public address (`https://….trycloudflare.com/mcp`)
to add as a custom connector; it authenticates with OAuth 2.1 and asks for this
program's password once.

The panel tells you whether you have `cloudflared` and the exact command to install
it (`brew install cloudflared` on macOS, `winget install --id Cloudflare.cloudflared`
on Windows). **This program will not download it for you**, and it never opens the
tunnel on its own. While the tunnel is open, anyone with the address
*and* the password reaches this machine at the level above. It dies when you turn it
off or close the program.

---

## Options

| Flag | What it does |
|---|---|
| `--port N` | Listen on another port (default `8765`). |
| `--no-ui` | Do not open the control panel window. |
| `--mcp-config` | Print the MCP config, ready to paste. |
| `--mcp-diagrams` | Run as an MCP server over stdio (what Claude Code launches). |

With a window open, **closing it stops the program**. With `--no-ui` it keeps running
until you stop it.

## Where your data lives

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/DiagraMind` |
| Windows | `%LOCALAPPDATA%\DiagraMind` |
| Linux | `~/.local/share/DiagraMind` |

Inside: `projects/` (your diagrams, as plain `tree.json` files you can read and back
up), `orchestrator/` (agent state), `token.txt` and `log.txt`.

It is outside the program on purpose — updating or reinstalling never touches it.

## Security, in one paragraph

The server listens **only on `127.0.0.1`**: nothing outside your machine can reach it.
Every request needs the token, because any website you have open in the browser could
otherwise talk to a local port. The agents' file access is confined to the folder you
mount for them, checked on the server side by resolving the real path — not by
trusting the client.

---

## Build a binary

```bash
python3 -m pip install pyinstaller certifi
bash backend/build_binary.sh
```

`certifi` is not optional: without its certificate bundle inside, **every HTTPS call
the binary makes fails** — including the update check.

Releases are cut by pushing a `v*` tag; `.github/workflows/release.yml` builds the
three systems and publishes them. The version lives in `backend/server.py` → `VERSION`.
