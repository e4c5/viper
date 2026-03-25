# GitHub Codespaces

This repository includes a recommended GitHub Codespaces setup in
`.devcontainer/devcontainer.json`.

## What the Codespace config does

- Uses the Microsoft Python 3.12 dev container image.
- Installs the project in editable mode with development dependencies on create.
- Sets `USE_INLINE_AGENT=true` in the remote environment so Jenkins pipeline runs
  default to the inline agent path.
- Forwards:
  - `3000` for Gitea
  - `8080` for Jenkins

## Start a Codespace

1. In GitHub, open the repository page.
2. Click **Code**.
3. Open the **Codespaces** tab.
4. Create a new Codespace on your branch.

GitHub Codespaces reads `.devcontainer/devcontainer.json` automatically.

## Recommended: run inline mode in Codespaces

The simplest Codespaces workflow does not require Docker-in-Docker. After the
Codespace starts, use the CLI directly:

```bash
code-review --help
```

This keeps setup lightweight and matches `USE_INLINE_AGENT=true`.

## Optional: run the local Gitea + Jenkins stack in Codespaces

If your Codespace supports Docker, you can still run the Compose stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.codespaces.yml up -d --build
```

Then open forwarded ports:

- `3000` → Gitea
- `8080` → Jenkins

## Why inline mode is the default

The bundled Jenkins pipeline supports two execution modes:

- **Inline mode** (`USE_INLINE_AGENT=true`) runs `code-review` directly inside the
  Jenkins environment.
- **Container mode** launches the agent via Docker or Podman.

Codespaces is a good fit for **inline mode**, because the CLI and Python
dependencies are installed directly into the dev environment and Jenkins does
not need to start a separate agent container for review execution.

## Notes

- The Docker Compose stack is still useful in a Codespace for local Gitea and
  Jenkins testing.
- `docker-compose.codespaces.yml` intentionally keeps Jenkins in inline mode and
  removes the host container socket mount requirement for Codespaces.
- If you prefer not to use the Compose stack, you can still run the CLI directly:

```bash
pip install -e ".[dev]"
code-review --help
```
