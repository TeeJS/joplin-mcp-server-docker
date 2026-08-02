# Joplin MCP — Docker / Unraid

Containerized fork of [`dweigend/joplin-mcp`](https://github.com/dweigend/joplin-mcp) that exposes the server over **Streamable HTTP** instead of stdio, so it can run as a long-lived service on Unraid (or any Docker host) and be reached from MCP clients over the network.

## Environment variables

| Variable                        | Required | Default                  | Notes                                                     |
| ------------------------------- | -------- | ------------------------ | --------------------------------------------------------- |
| `JOPLIN_TOKEN`                  | yes      | —                        | Joplin Web Clipper API token.                             |
| `JOPLIN_HOST`                   | yes      | `host.docker.internal`   | IP/hostname of the box running Joplin Desktop.            |
| `JOPLIN_PORT`                   | no       | `41184`                  | Joplin Web Clipper port.                                  |
| `MCP_PORT`                      | no       | `8000`                   | Port the MCP server listens on inside the container.      |
| `MCP_HOST`                      | no       | `0.0.0.0`                | Bind address inside the container.                        |
| `MCP_TRANSPORT`                 | no       | `streamable-http`        | Override only if you know what you're doing.              |
| `JOPLIN_READ_ONLY`              | no       | `false`                  | Disable every write tool. See below.                      |
| `JOPLIN_IMPORT_ROOT`            | no       | — (tool disabled)        | Directory `import_markdown` may read from.                |
| `JOPLIN_ALLOW_PERMANENT_DELETE` | no       | `false`                  | Allow unrecoverable `delete_note(permanent=true)`.        |

## Tool exposure policy

This server holds a Joplin token with full access to your notes. These three
settings limit what any caller can do with it, independently of who the caller
is — they apply even with no authentication in front.

**`JOPLIN_READ_ONLY=true`** unregisters every tool outside the read allowlist
(`search_notes`, `get_note`, `list_tags`, `get_note_tags`). Removed tools are not
advertised and cannot be called. Set this whenever the endpoint is reachable by
anyone you would not hand your notes to.

**`JOPLIN_IMPORT_ROOT`** is unset by default, which removes `import_markdown`
entirely. The tool reads a caller-supplied path off the container filesystem and
returns the contents, so unconfined it reads any file the container can see —
including `/proc/self/environ`, which holds `JOPLIN_TOKEN` itself. Set it only if
you have mounted a folder to import from; paths are then resolved before the
check, so symlinks and `..` cannot escape the root.

**`JOPLIN_ALLOW_PERMANENT_DELETE`** is off by default. `delete_note` still works
and still moves notes to the Joplin trash, where they can be recovered;
`permanent=true` is refused rather than silently downgraded.

## Authentication (OAuth 2.1)

| Variable                   | Required | Default   | Notes                                                                        |
| -------------------------- | -------- | --------- | ---------------------------------------------------------------------------- |
| `MCP_OAUTH_ENABLED`        | no       | `false`   | Require OAuth 2.1 bearer tokens.                                             |
| `MCP_OAUTH_ISSUER`         | if OAuth | —         | OIDC issuer, byte for byte as the provider reports it.                       |
| `MCP_SERVER_URL`           | if OAuth | —         | Public base URL, no path. Connector URL is this + `MCP_PATH`.                |
| `MCP_PATH`                 | no       | `/mcp`    | Path the MCP endpoint is served on.                                          |
| `MCP_OAUTH_AUDIENCE`       | no       | —         | Expected `aud`. Empty skips the check; enable last.                          |
| `MCP_OAUTH_JWKS_CACHE_TTL` | no       | `3600`    | Seconds to cache provider discovery.                                         |
| `MCP_OAUTH_GROUPS_CLAIM`   | no       | `groups`  | Token claim holding the caller's groups.                                     |
| `MCP_READ_GROUPS`          | no       | —         | Comma-separated groups granted read tools only.                              |
| `MCP_WRITE_GROUPS`         | no       | —         | Comma-separated groups granted every exposed tool.                           |

**Anything internet-reachable needs `MCP_OAUTH_ENABLED=true`.** Without it the
endpoint hands anyone who finds it full use of your Joplin token. The server logs
a warning at startup whenever it comes up ungated, so check the container log if
you are unsure which posture it is in.

With OAuth on, the server publishes:

- `/.well-known/oauth-protected-resource` and the `/mcp`-suffixed variant Claude
  probes first
- `/.well-known/oauth-authorization-server`, mirrored from the issuer's own
  discovery document

and answers unauthenticated requests to `/mcp` with `401` plus
`WWW-Authenticate: Bearer resource_metadata="…", scope="…"`, which is how Claude
locates the authorization server and learns which scopes to ask for. `/healthz`
stays outside the gate so the container healthcheck keeps working.

### Group-based tool access

```bash
MCP_READ_GROUPS=joplin-readers
MCP_WRITE_GROUPS=joplin-admins
```

- A caller in a **write** group gets every exposed tool.
- A caller in a **read** group gets `search_notes`, `get_note`, `list_tags`,
  `get_note_tags`. Write tools are hidden from `tools/list` *and* refused at
  `tools/call` — both halves, because a client can invoke a tool that was never
  listed.
- A caller in neither gets `403 insufficient_scope`. The token is valid, so it is
  deliberately not a `401`; re-authenticating would change nothing.
- Both empty disables group checking entirely.

Group policy only narrows. With `JOPLIN_READ_ONLY=true` the write tools are not
registered at all, so a write group grants nothing extra.

### Authelia client configuration

Four things here are **not** defaults, and each fails with the same opaque
client-side message ("Authorization with the MCP server failed"). The Authelia log
names the real cause; read it first.

```yaml
identity_providers:
  oidc:
    claims_policies:
      joplin_mcp_policy:
        access_token:
          - 'groups'                                   # groups in the ACCESS token
    clients:
      - client_id: 'joplin-mcp'
        client_name: 'Joplin MCP'
        client_secret: '$pbkdf2-sha512$...'            # hashed, never plaintext
        public: false
        authorization_policy: 'one_factor'             # two_factor needs an enrolled device
        require_pkce: true
        pkce_challenge_method: 'S256'
        access_token_signed_response_alg: 'RS256'      # else tokens are opaque
        token_endpoint_auth_method: 'client_secret_post'  # Claude uses POST, not Basic
        claims_policy: 'joplin_mcp_policy'
        audience:
          - 'https://joplin-mcp.example.com/mcp'       # exactly the resource value
        redirect_uris:
          - 'https://claude.ai/api/mcp/auth_callback'
        scopes: ['openid', 'profile', 'groups', 'offline_access']
        grant_types: ['authorization_code', 'refresh_token']
        response_types: ['code']
```

- `access_token_signed_response_alg: 'RS256'` — Authelia issues **opaque** access
  tokens by default, which cannot be validated statelessly at all.
- The `claims_policies` entry is what puts `groups` in the access token. Authelia's
  docs steer toward `/userinfo` instead, on the grounds that *clients* must not
  inspect access tokens. That rule binds clients; this is a resource server, which
  is exactly what RFC 9068 JWT access tokens exist for.
- `token_endpoint_auth_method: 'client_secret_post'` — with `client_secret_basic`
  the login succeeds and then the token exchange fails with `invalid_client`.
- `audience` must whitelist the exact `resource` value or Authelia refuses it.

Also: the file auth backend does not give users a `groups` field automatically. Add
it, or every group check denies.

Back up `configuration.yml` and run `authelia validate-config` **before**
restarting — a bad config takes down SSO for everything behind it.

Verify the client credentials without an interactive login by sending a
deliberately invalid code:

```bash
curl -s -d "client_id=joplin-mcp&client_secret=SECRET&grant_type=authorization_code&code=bad&redirect_uri=https://claude.ai/api/mcp/auth_callback" https://auth.example.com/api/oidc/token
```

`invalid_grant` means client authentication succeeded and only the fake code was
rejected — the secret and auth method are right. `invalid_client` means the secret
or the auth method is wrong.

### Adding the connector

claude.ai → **Settings → Connectors → Add custom connector**, URL
`https://<public-host>/mcp`, with the OAuth Client ID and Secret under **Advanced
settings**. Claude connects from Anthropic's egress range (`160.79.104.0/21`), not
from your browser, so the endpoint and the identity provider both have to be
reachable from the public internet.

Never put a token in the connector URL — the MCP authorization spec prohibits
access tokens in the query string, and this server rejects them there.

## Run with docker-compose (local test)

```bash
cp .env.example .env       # fill in JOPLIN_TOKEN + JOPLIN_HOST
docker compose up --build
```

The endpoint is then `http://localhost:8000/mcp`.

## Image builds (GitHub Actions → GHCR)

The image is built and published by `.github/workflows/docker.yml` on every push to `main` and every `v*` tag. It lands at:

```
ghcr.io/teejs/joplin-mcp-server-docker:latest
ghcr.io/teejs/joplin-mcp-server-docker:sha-<short>
ghcr.io/teejs/joplin-mcp-server-docker:<semver>   # on tags
```

GHCR packages default to private. After the first successful CI run, go to **GitHub → your profile → Packages → joplin-mcp-server-docker → Package settings → Change visibility → Public** so Unraid can pull without credentials. (Or keep it private and add a `docker login ghcr.io` on the Unraid host.)

## Run on Unraid

1. Make sure the image has been built at least once (push to `main` or run the workflow manually).
2. `my-joplin-mcp.xml` in this repo is already deployed to `/boot/config/plugins/dockerMan/templates-user/` on `SchmitzMegaplex`. Re-run the unraid template skill to redeploy if you change it.
3. In the Unraid Docker tab, click **Add Container**, pick the `joplin-mcp` template, fill in `JOPLIN_TOKEN` and `JOPLIN_HOST` (your PC's LAN IP), then **Apply**.
4. Endpoint: `http://<unraid-ip>:8000/mcp`.

### Updates

Unraid's Docker tab will show "update ready" whenever the `:latest` digest changes upstream — click **Force Update** (or Apply on the container) to pull and recreate.

## Pointing a client at it

In a Claude Desktop / Claude Code MCP config block:

```json
{
  "mcpServers": {
    "joplin": {
      "type": "http",
      "url": "http://<unraid-ip>:8000/mcp"
    }
  }
}
```

## Joplin Web Clipper reachability

Joplin's Web Clipper service binds to `127.0.0.1` by default — it will **not** accept connections from the Unraid box. To allow it:

- Joplin Desktop → **Tools → Options → Web Clipper** → enable the service.
- Verify from the Unraid console: `curl http://<your-pc-ip>:41184/ping` should return `JoplinClipperServer`.
- If it doesn't, you'll need to allow inbound 41184/tcp through your PC's firewall. Some Joplin versions also need an `advanced setting` to bind on all interfaces — check the Joplin forum for your version.
