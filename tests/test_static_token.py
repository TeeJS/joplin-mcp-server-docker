"""Static bearer token mode: the posture that replaces OAuth for local clients."""
import json, os, sys, threading, time
from pathlib import Path

MCP_PORT = 9410
SERVER_URL = f"http://127.0.0.1:{MCP_PORT}"
TOKEN = "b7f3" + "a" * 60  # 64 chars, over the 32 minimum

os.environ.update(
    JOPLIN_TOKEN="0123456789abcdef0123456789abcdef0123456789",
    JOPLIN_HOST="127.0.0.1", JOPLIN_PORT="41999",
    MCP_STATIC_TOKEN=f"  {TOKEN}  ",              # deliberate whitespace
    MCP_OAUTH_ENABLED="true",                     # must be overridden by static
    MCP_OAUTH_ISSUER="https://auth.invalid",
    MCP_SERVER_URL=SERVER_URL,
    MCP_READ_GROUPS="joplin-readers",             # must not apply in static mode
    MCP_WRITE_GROUPS="joplin-admins",
    MCP_HOST="127.0.0.1", MCP_PORT=str(MCP_PORT),
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx, uvicorn
import src.mcp.joplin_mcp as m

threading.Thread(
    target=lambda: uvicorn.Server(uvicorn.Config(
        m.build_app(), host="127.0.0.1", port=MCP_PORT, log_level="error")).run(),
    daemon=True).start()
time.sleep(2.5)

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
H = {"Content-Type": "application/json",
     "Accept": "application/json, text/event-stream"}
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS  {label}")
    else:
        fail += 1; print(f"  FAIL  {label}  {detail}")


def post(token=None):
    h = dict(H)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.post(f"{SERVER_URL}/mcp", headers=h, json=INIT, timeout=15)


print("\n[1] mode selection")
cfg = m.oauth.OAuthConfig.from_env()
check("static token wins over MCP_OAUTH_ENABLED", cfg.mode == "static", cfg.mode)
check("whitespace trimmed off the token", cfg.static_token == TOKEN)

print("\n[2] the gate")
check("no token -> 401", post().status_code == 401)
check("wrong token -> 401", post("b7f3" + "b" * 60).status_code == 401)
r = post(TOKEN)
check("correct token -> 200", r.status_code == 200, str(r.status_code))
check("session issued to valid caller", "mcp-session-id" in r.headers)

print("\n[3] the 401 does not advertise OAuth")
wa = post().headers.get("www-authenticate", "")
check("no resource_metadata pointer", "resource_metadata" not in wa, wa)
check("no scope list", "scope=" not in wa, wa)

print("\n[4] no OAuth discovery routes are published")
for path in ["/.well-known/oauth-protected-resource",
             "/.well-known/oauth-protected-resource/mcp",
             "/.well-known/oauth-authorization-server"]:
    code = httpx.get(f"{SERVER_URL}{path}", timeout=10).status_code
    check(f"{path} -> 404", code == 404, str(code))

print("\n[5] health stays outside the gate")
h = httpx.get(f"{SERVER_URL}/healthz", timeout=10)
check("/healthz open", h.status_code == 200)
check("reports auth mode", h.json().get("auth") == "static", h.text[:80])

print("\n[6] tools are reachable, group policy does not apply")
sid = r.headers["mcp-session-id"]
auth = {**H, "Authorization": f"Bearer {TOKEN}", "Mcp-Session-Id": sid}
httpx.post(f"{SERVER_URL}/mcp", headers=auth,
           json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
res = httpx.post(f"{SERVER_URL}/mcp", headers=auth,
                 json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, timeout=10)
tools = sorted(t["name"] for t in
               json.loads(res.text.split("data: ")[1])["result"]["tools"])
check("write tools present despite read group config",
      "delete_note" in tools and "create_note" in tools, str(tools))
check("import_markdown still absent (no root)", "import_markdown" not in tools)

print("\n[7] a short token is refused at startup")
try:
    m.oauth.OAuthConfig(static_token="tooshort").validate()
    check("short token rejected", False, "no error raised")
except m.oauth.OAuthConfigError as e:
    check("short token rejected", "at least 32" in str(e), str(e))

print(f"\n{'='*46}\n  {ok} passed, {fail} failed\n{'='*46}")
sys.exit(1 if fail else 0)
