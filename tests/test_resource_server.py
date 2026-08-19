"""End-to-end test: mock OIDC issuer + the real Joplin MCP server, gated."""
import json, os, sys, threading, time

ISSUER_PORT = 9401
MCP_PORT = 9400
ISSUER = f"http://127.0.0.1:{ISSUER_PORT}"
SERVER_URL = f"http://127.0.0.1:{MCP_PORT}"

os.environ.update(
    JOPLIN_TOKEN="0123456789abcdef0123456789abcdef0123456789",
    JOPLIN_HOST="127.0.0.1", JOPLIN_PORT="41999",
    MCP_OAUTH_ENABLED="true",
    MCP_OAUTH_ISSUER=f"  {ISSUER}  ",           # deliberate whitespace
    MCP_SERVER_URL=SERVER_URL,
    MCP_READ_GROUPS="joplin-readers",
    MCP_WRITE_GROUPS=" joplin-admins ,  ",      # deliberate junk
    MCP_HOST="127.0.0.1", MCP_PORT=str(MCP_PORT),
    # 0 makes the discovery cache expire before every single request, so the
    # whole suite runs through the JWKS-refresh path. That path once threw away
    # the warm key client and rejected every token arriving during the swap,
    # which clients read as "token expired" and answered with a browser window
    # each. Exercising it everywhere is cheap against a local mock issuer.
    MCP_OAUTH_JWKS_CACHE_TTL="0",
)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import httpx, jwt, uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import to_base64url_uint
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub = key.public_key().public_numbers()
JWK = {"kty": "RSA", "kid": "test-1", "use": "sig", "alg": "RS256",
       "n": to_base64url_uint(pub.n).decode(), "e": to_base64url_uint(pub.e).decode()}


async def oidc_config(request):
    return JSONResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks",
                         "authorization_endpoint": f"{ISSUER}/authorize",
                         "token_endpoint": f"{ISSUER}/token",
                         "code_challenge_methods_supported": ["S256"],
                         "scopes_supported": ["openid", "profile", "groups", "offline_access"]})


async def jwks(request):
    return JSONResponse({"keys": [JWK]})


issuer_app = Starlette(routes=[
    Route("/.well-known/openid-configuration", oidc_config),
    Route("/jwks", jwks)])


def mint(groups, sub="tschmitz", **over):
    payload = {"iss": ISSUER, "sub": sub, "aud": f"{SERVER_URL}/mcp",
               "exp": int(time.time()) + 600, "iat": int(time.time()),
               "groups": groups}
    payload.update(over)
    return jwt.encode(payload, key, algorithm="RS256",
                      headers={"kid": "test-1", "typ": "at+jwt"})


def serve(app, port):
    uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                  log_level="error")).run()


threading.Thread(target=serve, args=(issuer_app, ISSUER_PORT), daemon=True).start()
time.sleep(1.5)

import src.mcp.joplin_mcp as m
threading.Thread(target=serve, args=(m.build_app(), MCP_PORT), daemon=True).start()
time.sleep(2.5)

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS  {label}")
    else:
        fail += 1; print(f"  FAIL  {label}  {detail}")


def sse_json(text):
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)


def session(token):
    """initialize + notifications/initialized, returns (response, session id)."""
    r = httpx.post(f"{SERVER_URL}/mcp", headers={**H, "Authorization": f"Bearer {token}"},
                   json=INIT, timeout=10)
    sid = r.headers.get("mcp-session-id")
    if sid:
        httpx.post(f"{SERVER_URL}/mcp",
                   headers={**H, "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid},
                   json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
    return r, sid


def rpc(token, sid, method, params=None):
    body = {"jsonrpc": "2.0", "id": 2, "method": method}
    if params:
        body["params"] = params
    r = httpx.post(f"{SERVER_URL}/mcp",
                   headers={**H, "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid},
                   json=body, timeout=10)
    return sse_json(r.text)


print("\n[1] unauthenticated access")
r = httpx.post(f"{SERVER_URL}/mcp", headers=H, json=INIT, timeout=10)
wa = r.headers.get("www-authenticate", "")
check("anonymous initialize -> 401", r.status_code == 401, f"got {r.status_code}")
check("no session id issued", "mcp-session-id" not in r.headers)
check("WWW-Authenticate has resource_metadata", "resource_metadata=" in wa, wa)
check("WWW-Authenticate has scope", 'scope="openid profile groups offline_access"' in wa, wa)

print("\n[2] unauthenticated discovery + health")
h = httpx.get(f"{SERVER_URL}/healthz", timeout=10)
check("/healthz open, app JSON", h.status_code == 200 and h.json()["service"] == "joplin-mcp", h.text[:80])
for path in ["/.well-known/oauth-protected-resource",
             "/.well-known/oauth-protected-resource/mcp"]:
    d = httpx.get(f"{SERVER_URL}{path}", timeout=10)
    body = d.json() if d.status_code == 200 else {}
    check(f"{path} -> 200", d.status_code == 200, str(d.status_code))
    check(f"{path} resource exact", body.get("resource") == f"{SERVER_URL}/mcp", repr(body.get("resource")))
    check(f"{path} points at issuer (trimmed)", body.get("authorization_servers") == [ISSUER],
          repr(body.get("authorization_servers")))
a = httpx.get(f"{SERVER_URL}/.well-known/oauth-authorization-server", timeout=10)
check("AS metadata mirrors issuer", a.status_code == 200 and a.json().get("issuer") == ISSUER, a.text[:80])

print("\n[3] bad tokens")
r = httpx.post(f"{SERVER_URL}/mcp", headers={**H, "Authorization": "Bearer garbage"}, json=INIT, timeout=10)
check("garbage token -> 401", r.status_code == 401, str(r.status_code))
expired = mint(["joplin-admins"], exp=int(time.time()) - 60)
r = httpx.post(f"{SERVER_URL}/mcp", headers={**H, "Authorization": f"Bearer {expired}"}, json=INIT, timeout=10)
check("expired token -> 401", r.status_code == 401, str(r.status_code))
other = jwt.encode({"iss": "http://evil", "sub": "x", "exp": int(time.time()) + 600,
                    "groups": ["joplin-admins"]}, key, algorithm="RS256", headers={"kid": "test-1"})
r = httpx.post(f"{SERVER_URL}/mcp", headers={**H, "Authorization": f"Bearer {other}"}, json=INIT, timeout=10)
check("wrong issuer -> 401", r.status_code == 401, str(r.status_code))
r = httpx.post(f"{SERVER_URL}/mcp?access_token={mint(['joplin-admins'])}", headers=H, json=INIT, timeout=10)
check("token in query string rejected", r.status_code == 401, str(r.status_code))

print("\n[4] valid token, unmapped group -> 403")
r = httpx.post(f"{SERVER_URL}/mcp", headers={**H, "Authorization": f"Bearer {mint(['some-other-group'])}"},
               json=INIT, timeout=10)
check("unmapped group -> 403 not 401", r.status_code == 403, str(r.status_code))
check("403 body says insufficient_scope", r.json().get("error") == "insufficient_scope", r.text[:80])

print("\n[5] read group")
rt = mint(["joplin-readers"])
r, sid = session(rt)
check("read caller initialize -> 200", r.status_code == 200, str(r.status_code))
names = sorted(t["name"] for t in rpc(rt, sid, "tools/list")["result"]["tools"])
check("read caller sees only read tools", names == ["find_linked_notes", "find_similar_notes", "get_note", "get_note_tags", "list_notebooks", "list_tags", "search_notes"], str(names))
res = rpc(rt, sid, "tools/call", {"name": "delete_note", "arguments": {"note_id": "x"}})
text = json.dumps(res)
check("read caller refused unlisted write tool", "forbidden" in text and "write access" in text, text[:160])

print("\n[6] write group")
wt = mint(["joplin-admins"])
r, sid = session(wt)
check("write caller initialize -> 200", r.status_code == 200, str(r.status_code))
wnames = sorted(t["name"] for t in rpc(wt, sid, "tools/list")["result"]["tools"])
check("write caller sees write tools", "delete_note" in wnames and "create_note" in wnames, str(wnames))
check("import_markdown still absent (no root)", "import_markdown" not in wnames, str(wnames))
res = rpc(wt, sid, "tools/call", {"name": "delete_note", "arguments": {"note_id": "x", "permanent": True}})
check("permanent delete refused by server policy", "Permanent deletion is disabled" in json.dumps(res), json.dumps(res)[:160])
res = rpc(wt, sid, "tools/call", {"name": "search_notes", "arguments": {"args": {"query": "x"}}})
blob = json.dumps(res)
check("joplin failure leaks no token", "0123456789abcdef" not in blob, blob[:200])

print("\n[7] space-delimited groups claim")
st = mint("joplin-readers extra")
r, sid = session(st)
check("string groups claim accepted", r.status_code == 200, str(r.status_code))

print("\n[8] concurrent burst across a JWKS cache refresh")
import concurrent.futures
bt = mint(["joplin-admins"])


def one_call(_):
    return httpx.post(f"{SERVER_URL}/mcp",
                      headers={**H, "Authorization": f"Bearer {bt}"},
                      json=INIT, timeout=20).status_code


with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    codes = list(ex.map(one_call, range(12)))
check("12 simultaneous valid tokens all accepted", set(codes) == {200},
      f"got {sorted(set(codes))} -> {codes.count(401)} spurious 401s")

print(f"\n{'='*46}\n  {ok} passed, {fail} failed\n{'='*46}")
sys.exit(1 if fail else 0)
