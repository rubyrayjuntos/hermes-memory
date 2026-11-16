# Isolated Hermes beta-validation environment

This Compose setup provides a fresh Hermes home and a **dedicated Docker-in-Docker daemon** for validating `hermes-memory-install`. It does not mount the host Docker socket or the host `~/.hermes` directory. The nested daemon is privileged, but its containers, networks, and images are isolated from the host Docker daemon; its Docker API uses client certificates and is not published to the host. The current Claims / Entities Explorer assets are mounted read-only from this checkout; the retired Fountain page is available only at `/fountain.html`.

The Explorer is available at `http://127.0.0.1:17890/` by default. The root redirects to the Explorer route, and the proxy forwards `/api/` requests to the gateway API bound to `127.0.0.1:7890` inside the isolated network namespace. This avoids colliding with an inspector already listening on host port `7890`. If host port `17890` is occupied, set `PANE_HOST_PORT` to another free local port and use that port in the browser URL.

## Prerequisites

- Docker with Compose.
- A local Hermes Agent source checkout with its `Dockerfile` (this setup builds the image from source).
- `uv` installed on the host. The Hermes image does not include the `uv` command; Compose mounts the host executable into the isolated Hermes container.
- The host Docker Compose plugin executable, mounted into Hermes so `hermes-memory-install` can run `docker compose` against the nested daemon.

On this machine, the Hermes checkout and Compose plugin are:

```bash
export HERMES_AGENT_SOURCE="$HOME/.hermes/hermes-agent"
export UV_BIN="$(command -v uv)"
export DOCKER_COMPOSE_PLUGIN="/usr/libexec/docker/cli-plugins/docker-compose"
```

Change either path if your installation differs. Check both exist before proceeding:

```bash
test -f "$HERMES_AGENT_SOURCE/Dockerfile"
test -x "$UV_BIN"
test -x "$DOCKER_COMPOSE_PLUGIN"
```

## Build and start

From the repository root, validate the configuration first:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml config --quiet
```

Then build the Hermes image and start the isolated environment:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml build hermes
docker compose -f sandbox/hermes-beta/compose.yaml up -d
```

The Hermes image build may download dependencies. Starting the stack creates only its two named volumes (`docker-data` and `hermes-home`) on the host Docker daemon; the memory database is created later inside the nested daemon when the installer runs.

If the stack is already running and you changed Compose configuration, recreate only Hermes so both named volumes and the nested Docker daemon are preserved:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml up -d --force-recreate hermes
```

## Install and configure

The container sets `HOME=/opt/data` and `HERMES_HOME=/opt/data/.hermes`, so both Hermes Agent and the memory installer use the same isolated home. Run the installer from a pinned Git revision with the host `uv` executable mounted read-only into the isolated Hermes container:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml exec --user hermes hermes \
  uv tool run --from \
  "git+https://github.com/rubyrayjuntos/hermes-memory@c4f200587136521a8cdd666f9056af4c50cc3bc5" \
  hermes-memory-install --yes
```

This pinned revision is the current Alpha snapshot documented in the main README, not a Beta release. The install configures the isolated Hermes home and starts the database and inspector API inside the nested daemon.

To configure a model interactively in the isolated home, run:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml exec -it --user hermes hermes hermes
```

Do not copy host model credentials or mount the host Hermes directory into this environment. Credentials configured here stay in the `hermes-home` volume.

## Validate and stop

Open `http://127.0.0.1:17890/` in a browser to interact with the live Explorer. The API requests are served through the local proxy; the inspector API is not published directly to the host.

After installation, verify the Explorer route and graph API respond through the same browser-facing origin:

```bash
curl -fsS "http://127.0.0.1:${PANE_HOST_PORT:-17890}/api/librarian/explorer" -o /dev/null
curl -fsS "http://127.0.0.1:${PANE_HOST_PORT:-17890}/api/librarian/graph/stats?fresh=1"
```

For Beta evidence, use [`../../DEFINITION_OF_DONE.md`](../../DEFINITION_OF_DONE.md) as the human-demonstration gate. The existing [`../../docs/PANE_INTERACTION_SPEC.md`](../../docs/PANE_INTERACTION_SPEC.md) describes the retired Fountain UI; first map its required behaviors to the current Graph Explorer, then exercise the applicable interactions and save the screenshot or transcript. Starting the stack or receiving a successful `curl` response alone does not satisfy the gate.

Stop the containers while preserving the isolated test state:

```bash
docker compose -f sandbox/hermes-beta/compose.yaml down
```

Starting again with `up -d` reuses the volumes. Removing volumes destroys the test Hermes home, nested Docker images, and installed database, so do that only when you intentionally want to reset the environment.

The Hermes image has already been built and the isolated stack has been started in the current environment. The Compose changes still need to be applied with the service recreation command above before running the installer.
