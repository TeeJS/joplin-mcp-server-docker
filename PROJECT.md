# PROJECT.md — Secure the Joplin MCP server

Status: **done and verified in production.** All six phases of the skill's order of work are
complete. The endpoint that answered an anonymous `initialize` with `200` and a session id
now returns `401` from outside the network. Two interactive follow-ups remain, listed at the
end of §5.
Created: 2026-08-01
Reference pattern: `TeeJS/linkwarden-mcp-server`. **Not `plex-mcp-server-docker`.** Plex's
`modules/auth.py` was written against Authentik and its shape reflects that — the audience
check is disabled outright with the comment "Authentik sets aud to client_id", and
`authorization_servers` points at the MCP server itself rather than the issuer. Authelia is
what is actually deployed here, and linkwarden is the Authelia-targeted implementation. It
also carries four security fixes made after plex and never backported to it: whitespace
tolerance in config (`faf3700`), declared scopes instead of letting the client guess
(`3cedb71`), credential redaction in logs (`371408d`), and group-based authorization
(`e607f8b`) — which plex has no concept of.

## Current posture

`src/mcp/joplin_mcp.py` starts FastMCP with the `streamable-http` transport bound to
`0.0.0.0:8000` and **no authentication of any kind**. It is deployed on Unraid and reached
today at `http://192.168.1.25:8004/mcp`. It holds `JOPLIN_TOKEN`, a credential with full
read/write access to the entire Joplin note database.

Ten tools are exposed. Four read (`search_notes`, `get_note`, `list_tags`,
`get_note_tags` — `search_notes` with a wildcard returns everything), six write
(`create_note`, `update_note`, `delete_note`, `import_markdown`, `tag_note`,
`untag_note`). `delete_note(permanent=True)` is unrecoverable.

## Findings

Ranked by what an attacker gets, not by how hard they are to fix.

### F1 — Unauthenticated and exposed to the public internet (critical, CONFIRMED)

Anyone who can reach the port gets every tool — no credential, no consent, no audit trail.

Confirmed by test on 2026-08-01. `joplin-mcp.schmitzplex.com` carries a public A record
(`136.36.104.114`; the LAN answer is split-horizon). An anonymous `initialize` sent over
that public path returned:

    HTTP/1.1 200 OK
    mcp-session-id: c30ac18b9f0b48bfac322f4ee2e65ce2
    serverInfo: {"name":"joplin","version":"1.28.1"}

A session id issued to an anonymous caller is the whole finding: everything behind it —
`search_notes` over the entire database, `update_note`, `delete_note(permanent=True)`, and
the `import_markdown` token disclosure in F2 — is reachable by any host on the internet.
Tool enumeration against production was deliberately not run; the source is sufficient.

`serverInfo.version` also confirms F4 empirically: the running image resolved to MCP SDK
1.28.1 at build time, pinned by nothing.

### F2 — `import_markdown` is an arbitrary file-read primitive (critical)

`src/mcp/joplin_mcp.py:252` accepts an unconstrained `file_path`, reads it
(`joplin_utils.py:59`), and returns the contents in the tool response. The container runs
as **root** (no `USER` in the Dockerfile). A caller reads `/proc/self/environ` and
recovers `JOPLIN_TOKEN` itself, then talks to the Joplin API directly and never touches
this server again. Adding authentication does not close this — the tool has to be
constrained. Dropping to a non-root user does not close it either: a process can always
read its own environment.

### F3 — The Joplin token leaks into tool responses and container logs (high)

`joplin_api.py:221` puts the token in the query string as well as the header. `requests`
exceptions stringify the full URL, and every tool ends with
`return {"error": str(e)}` plus `logger.error(f"...: {e}")`. One failing Joplin call
hands the token back to the caller and writes it to disk.

### F4 — Dependencies are unpinned, and the next build is already broken (high)

`pyproject.toml:15` requires `mcp[cli]` with no bound. The Dockerfile runs
`pip install .`, which ignores the `uv.lock` it copies in, so the lockfile provides no
pinning at all. PyPI now resolves `mcp` to **2.0.0, which removed `mcp.server.fastmcp`**
(verified locally). The next CI build therefore produces an image that crashes on import.
The version in the running image is also unknown and unreproducible.

### F5 — No DNS-rebinding protection (medium)

`transport_security` is not passed to `FastMCP`, and the SDK disables the check by default
for backwards compatibility (`mcp/server/transport_security.py`). No `Origin` or `Host`
validation. Any web page the user visits can POST JSON-RPC at the LAN address and drive
write and delete tools blind — CORS hides the responses, it does not stop the requests.

### F6 — Authentication alone would not be enough (medium)

With OAuth on and nothing else, every authenticated user still gets `delete_note`. There is
no read/write split and no server-level kill switch.

### F7 — Container hardening (medium)

Runs as root, no `HEALTHCHECK`, and no health endpoint to point one at — which also means
there is nothing to keep outside the auth gate once auth is added.

## 1. What is the one thing this must do?

Make it impossible for an unauthenticated caller to read, modify, or delete Joplin notes
through this server, or to recover the Joplin token from it — while keeping the existing
Claude Code client on the LAN working.

## 2. What would be wrong if we shipped "working" software without it?

- An endpoint that answers `200` to a stranger. Reachability and authorization are separate
  problems; a reverse proxy solves only the first.
- Auth added at the front door while `import_markdown` keeps handing out the token that
  makes the front door irrelevant.
- A gate that authenticates but does not authorize, so any valid account can permanently
  delete notes.
- Claiming it is secure on the basis of a green build and a healthy container. The claim
  requires an unauthenticated request being *refused from outside the network*.

## 3. What is explicitly off-limits as a workaround?

- Relying on the hostname or port being unguessable.
- A token in the connector URL query string. The MCP authorization spec prohibits it.
- Breaking the existing LAN client, or forcing OAuth on a deployment that does not need it.
- Silently dropping tools to reduce the attack surface. `import_markdown` gets constrained,
  not deleted, unless you say otherwise.
- Declaring success from a `200`, a status code alone, or a test run from inside the LAN.

## 4. Deployment target and backup location

- Image: `ghcr.io/teejs/joplin-mcp-server-docker`, built by Actions, pulled by Unraid.
- Runtime: container on Unraid (`192.168.1.25`), host port 8004 → container 8000.
- Backup: this git repo, clean at `0d0572f`. Every edit is revertible; no separate file
  backups needed. `my-joplin-mcp.xml` on the Unraid host is the one file outside git — it
  gets a timestamped copy before it is replaced.

## 5. How will we verify it is done?

Verified locally against a mock OIDC issuer, on **both** `mcp` 1.28.1 (what the deployed
image reports) and 1.29.0 (what the lockfile resolves) — 27 assertions, all passing:

- [x] With auth on, an unauthenticated `initialize` returns `401` and no `Mcp-Session-Id`.
- [x] That `401` carries `WWW-Authenticate: Bearer resource_metadata="…", scope="…"`.
- [x] Both `/.well-known/oauth-protected-resource` and the `/mcp`-suffixed variant return
      JSON whose `resource` equals the connector URL exactly. Tested with deliberate
      whitespace around the issuer env var; the served value is trimmed.
- [x] `/.well-known/oauth-authorization-server` mirrors the issuer's live document.
- [x] `/healthz` answers unauthenticated and returns the app's own JSON.
- [x] A token with no mapped group gets `403 insufficient_scope`, not `401`.
- [x] A read-group caller sees 4 tools instead of 9 **and** is refused when it calls
      `delete_note` by name anyway.
- [x] Garbage, expired, and wrong-issuer tokens are refused; a token in the query string
      is refused rather than accepted as a fallback.
- [x] `import_markdown` refuses `..`, absolute paths, and `/proc/self/environ`, and is not
      registered at all when no root is set.
- [x] A forced Joplin API failure returns an error containing no token.
- [x] `delete_note(permanent=true)` is refused under the default policy.
- [x] A space-delimited `groups` claim is accepted, as well as an array.

Verified against the **published image** on the Unraid host, OAuth on, pointed at the live
Authelia (throwaway container on port 18099, since removed):

- [x] CI green; `uv sync --frozen` and the `mcp<2` bound both hold in the image.
- [x] Container runs healthy as non-root (`joplin`), so `/healthz` works through the
      HEALTHCHECK with auth enabled — i.e. health really is outside the gate.
- [x] Anonymous `initialize` → `401`, no session id, `WWW-Authenticate` carrying both
      `resource_metadata` and `scope="openid profile groups offline_access"`.
- [x] `resource` is exactly `https://joplin-mcp.schmitzplex.com/mcp`.
- [x] Both well-known paths answer; the AS mirror resolves against the live issuer.
- [x] Docker media types are `application/vnd.docker.*`, so Unraid's update check can read
      the manifest.
- [x] Authelia `joplin-mcp` client authenticates: an invalid-code exchange returns
      `invalid_grant`, not `invalid_client`, confirming the secret and
      `client_secret_post` are both right.

Verified on the live deployment, 2026-08-01, after the container was updated and the five
OAuth variables set:

- [x] Container healthy on the new image, logging `OAuth enabled` with
      `resource=https://joplin-mcp.schmitzplex.com/mcp` and both group lists mapped.
      Env inspected with `cat -A`: no stray tabs or trailing whitespace.
- [x] Anonymous `initialize` over the public path → `401`, **no session id**, carrying
      `resource_metadata` and `scope`. Before the change the same request returned `200`
      and a session id.
- [x] Anonymous `tools/list` → `401`. Nine tools reachable before, zero now.
- [x] **Refused from genuinely outside the network** — fetched from Anthropic's egress
      rather than over hairpin NAT from the LAN: `GET /mcp` → `401 Unauthorized`.
- [x] `/healthz` and both protected-resource documents answer externally and return the
      app's own JSON, not reverse-proxy HTML.

- [x] Connector added at claude.ai and connected. The container logged
      `AUTHZ_GRANTED permission=write` 18 times against subject
      `3bfb08ae-…`, which proves the whole chain: the token validated, the `groups` claim
      reached the **access** token (a missing claim would have been `403 AUTHZ_DENIED`),
      and it mapped to `joplin-admins`.

Authelia advertises no `registration_endpoint`, so Claude's dynamic client registration
cannot work and the connector's "optional" OAuth Client ID and Secret fields are in fact
required. That surfaces as "Automatic client registration isn't supported."

Left:

- [ ] Regenerate the client secret. The plaintext was echoed into a chat transcript.
- [ ] Decide what to do about the LAN client — see below.

## Consequence: the direct LAN endpoint is gated too

The gate is server-wide, so `http://192.168.1.25:8004/mcp` now returns `401` as well. Any
Claude Code config pointing at the LAN address stops working. That is correct behaviour, not
a regression — but it needs a decision.

To point Claude Code at the gated endpoint instead, Authelia needs the loopback redirects a
native client uses (RFC 8252, ephemeral port, so the port is ignored):

```yaml
        redirect_uris:
          - 'https://claude.ai/api/mcp/auth_callback'
          - 'http://localhost/callback'
          - 'http://127.0.0.1/callback'
```

The alternative is to leave the LAN config removed and use the claude.ai connector from both
places.

## Planned changes

### New — `src/mcp/oauth.py`, OAuth 2.1 resource server

Optional, **off by default** (`MCP_OAUTH_ENABLED=false`), so one image serves both the
trusted-LAN posture and the internet-facing one.

- Config read from env, whitespace-trimmed on every value, validated at startup; logs the
  normalized values, not the raw ones.
- Lazy OIDC discovery so the container can start before the identity provider. JWKS cached
  with a TTL; a stale verifier is served if a refresh fails rather than refusing everything
  while the IdP blips.
- Validates signature, `iss`, `exp`, and optionally `aud`. Accepts RFC 9068 `typ: at+jwt`.
- `401` carrying `resource_metadata` **and** `scope`, so the client is told which scopes to
  request instead of guessing every scope the provider advertises.
- Serves the protected resource metadata at both probed paths, and mirrors the issuer's
  discovery document at `/.well-known/oauth-authorization-server`.
- `/healthz` registered outside the gate.

Built on the SDK's own `token_verifier` / `AuthSettings` plumbing rather than a hand-rolled
middleware, because that plumbing also binds each MCP session to the credential that
created it. Hand-rolling would silently lose that.

### New — `src/mcp/authz.py`, group-based tool access

`MCP_READ_GROUPS` / `MCP_WRITE_GROUPS` / `MCP_GROUPS_CLAIM`. Handles the three claim shapes
providers emit (JSON array, string list, space-delimited string). Both lists empty disables
group checking, so the zero-config LAN case still works; setting either turns it on and
anyone in neither list is refused.

Enforced at **`tools/list` and `tools/call`**. Filtering the list alone is not a control — a
client can invoke a tool that was never listed. Valid token, no mapped group → `403
insufficient_scope`, deliberately not `401`.

Policy only narrows: a new `JOPLIN_READ_ONLY=true` registers no write tools at all, and no
group grant can override it.

### `import_markdown` — constrained, not removed

New `JOPLIN_IMPORT_ROOT`. Unset (the default) disables the tool entirely. Set, it confines
reads to that directory after resolving symlinks and `..`. In the current container nothing
is mounted, so the tool has no legitimate use and the default costs nothing — but the
capability stays available to anyone who mounts a folder for it.

### Token-leak fixes

Scrub `token=` from any exception text before it is returned or logged; redact
`Authorization`, `Cookie`, and `X-Api-Key` anywhere a request is logged. Authorization
decisions logged at info, with the subject and the decision — never the token — because an
audit record that only exists at debug level is not an audit record.

### Dependency pinning

Pin `mcp[cli]>=1.29,<2` and the rest, regenerate `uv.lock`, and install from the lock in the
Dockerfile so the build is reproducible. Adds `pyjwt[crypto]` for JWT/JWKS validation. This
is a prerequisite, not a nice-to-have: without it the next build does not start.

### Transport and container hardening

Explicit `TransportSecuritySettings` with configurable allowed hosts and origins.
Non-root `USER`, and a `HEALTHCHECK` against `/healthz`.

### Docs and Unraid template

Env table, the Authelia prerequisites that are not defaults
(`access_token_signed_response_alg: 'RS256'`, a `claims_policies` entry putting `groups` in
the **access** token, `token_endpoint_auth_method: 'client_secret_post'`, and the exact
resource value whitelisted under `audience`), connector setup, and the new variables added
to `my-joplin-mcp.xml` with `Mask="true"` on every secret.

## Decisions taken without asking (all reversible)

- **OAuth optional, off by default.** Matches the sibling repo. Avoids a blocking decision
  and keeps the LAN client working during the change.
- **`import_markdown` constrained rather than removed.** Removing a tool is a
  behaviour change you did not ask for; disabled-by-default achieves the same safety.
- **SDK auth plumbing over hand-rolled middleware**, for the session-to-credential binding.
- **`mcp` pinned to the 1.x line** rather than migrating to 2.0. The 2.0 migration is real
  work (`mcp.server.fastmcp` is gone) and does not belong in a security change.

## Settled

- **Public hostname:** `https://joplin-mcp.schmitzplex.com`, so
  `MCP_SERVER_URL=https://joplin-mcp.schmitzplex.com` and the connector URL — and therefore
  the `resource` value, byte for byte — is `https://joplin-mcp.schmitzplex.com/mcp`.
- **Issuer:** `https://auth.schmitzplex.com`, confirmed from the live discovery document.
  A bare origin with no trailing slash, which is what Authelia reports and what
  `MCP_OAUTH_ISSUER` must be set to exactly.
- **Identity provider:** Authelia. Not Authentik — this is why plex is not the pattern.

- **Groups:** `MCP_READ_GROUPS=joplin-readers`, `MCP_WRITE_GROUPS=joplin-admins`, baked
  into `my-joplin-mcp.xml` as template defaults.
- **`joplin-admins` granted to `tschmitz`** in `/mnt/user/appdata/Authelia/users_database.yml`
  on 2026-08-01, appended to the existing `linkwarden-admins` rather than replacing it.
  Backed up alongside as `users_database.yml.bak-20260801-205025`; `authelia
  validate-config` passed and the container restarted healthy. The file backend does not
  watch that file, so the restart was required for the group to take effect.

## Open questions

None. The remaining work is deployment, below.

## What is left, and it is all deployment

The code is done and committed on `security/harden-exposed-server` (three commits). Nothing
protects the running server until it is deployed, so the endpoint is open right now.

1. **Merge and push to `main`.** Not done — pushing publishes an image, which is the user's
   call. CI only builds on `main` and `v*` tags, so a branch push alone ships nothing.
2. **Set `JOPLIN_READ_ONLY=true` on the container now.** This is the single largest
   reduction available before OAuth is configured, and it needs no rebuild — just the env
   var and a restart. Leave it on until step 4 is verified.
3. **Redeploy the Unraid template** so the new variables appear in the Docker tab. The copy
   on the host is outside git; back it up before replacing it.
4. **Create the Authelia `joplin-mcp` client** using the block in `README-DOCKER.md`, which
   is filled in for this deployment and modelled on the working `linkwarden-mcp` client
   already in `configuration.yml`. Back up `configuration.yml`, run
   `authelia validate-config` before restarting. Then set `MCP_OAUTH_ENABLED=true` on the
   container; the group variables are already template defaults.
5. **Run the exposure test from outside the network** (`references/verification.md` §2). Not
   from the LAN, and read the body rather than the status code.

Until step 5 passes, this is not done, regardless of how green the local suite is.
