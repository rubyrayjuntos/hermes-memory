# Graph Explorer (Hermes Desktop plugin)

Pins **Graph** in the Desktop sidebar and opens the live explorer served by
`hermes_memory.graph_api` at `http://127.0.0.1:7890/api/librarian/explorer`.

## Install

```bash
mkdir -p ~/.hermes/desktop-plugins
cp -R desktop-plugins/graph-explorer ~/.hermes/desktop-plugins/
```

Enable **Graph Explorer** under **Capabilities → Plugins**. Override the iframe
URL with `localStorage.setItem('hermes.graphExplorerUrl', '…')` if the viz API
is not on the default port.

## Requirements

- Viz API running (`python -m hermes_memory.graph_api serve`)
- Built explorer assets in `docs/graph/explorer/` (`npm run build` in `ui/graph-explorer`)
