/**
 * Hermes Desktop — Graph Explorer sidebar entry.
 *
 * Install: copy this folder to ~/.hermes/desktop-plugins/graph-explorer/
 * (folder name must match plugin id). Enable under Capabilities → Plugins.
 *
 * Opens a full-page view that embeds the live explorer from the librarian
 * viz API (default http://127.0.0.1:7890/api/librarian/explorer).
 */
import { host, ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const EXPLORER_URL =
  (typeof localStorage !== 'undefined' &&
    localStorage.getItem('hermes.graphExplorerUrl')) ||
  'http://127.0.0.1:7890/api/librarian/explorer'

function ExplorerPage() {
  return jsxs('div', {
    className: 'flex h-full w-full flex-col',
    style: { background: 'var(--ui-bg, #12161C)' },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 border-b px-3 py-2 text-xs',
        style: {
          borderColor: 'var(--ui-border, #2E3846)',
          color: 'var(--ui-text-tertiary, #A7B0BC)',
        },
        children: [
          jsx('span', {
            className: 'font-medium',
            style: { color: 'var(--ui-text, #EDEFF2)' },
            children: 'Graph Explorer',
          }),
          jsx('span', { children: '·' }),
          jsx('span', { className: 'truncate', children: EXPLORER_URL }),
          jsx('button', {
            className: 'ml-auto underline',
            onClick: () => host.navigate('/'),
            children: 'Back to chat',
          }),
        ],
      }),
      jsx('iframe', {
        title: 'Hermes Graph Explorer',
        src: EXPLORER_URL,
        className: 'h-full w-full flex-1 border-0',
        style: { minHeight: 0 },
      }),
    ],
  })
}

export default {
  id: 'graph-explorer',
  name: 'Graph Explorer',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/graph-explorer' },
        render: () => jsx(ExplorerPage, {}),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: {
          path: '/graph-explorer',
          label: 'Graph',
          codicon: 'type-hierarchy-sub',
        },
      },
    ])
  },
}
