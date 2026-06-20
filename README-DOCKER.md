# Joplin MCP — Docker / Unraid

Containerized fork of [`dweigend/joplin-mcp`](https://github.com/dweigend/joplin-mcp) that exposes the server over **Streamable HTTP** instead of stdio, so it can run as a long-lived service on Unraid (or any Docker host) and be reached from MCP clients over the network.

## Environment variables

| Variable        | Required | Default                  | Notes                                                     |
| --------------- | -------- | ------------------------ | --------------------------------------------------------- |
| `JOPLIN_TOKEN`  | yes      | —                        | Joplin Web Clipper API token.                             |
| `JOPLIN_HOST`   | yes      | `host.docker.internal`   | IP/hostname of the box running Joplin Desktop.            |
| `JOPLIN_PORT`   | no       | `41184`                  | Joplin Web Clipper port.                                  |
| `MCP_PORT`      | no       | `8000`                   | Port the MCP server listens on inside the container.      |
| `MCP_HOST`      | no       | `0.0.0.0`                | Bind address inside the container.                        |
| `MCP_TRANSPORT` | no       | `streamable-http`        | Override only if you know what you're doing.              |

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
