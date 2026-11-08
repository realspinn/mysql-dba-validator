# web/: the frontend source

The two pages the server sends are built here by [Vite](https://vite.dev) and committed
in `frontend/app/`:

| Page | Entry | Built | Served at |
| --- | --- | --- | --- |
| The validator (dashboard, SQL review, observed performance, lock diagnostics) | `index.html` → `src/app/main.tsx` (React) | `frontend/app/index.html` | `/`, and `/demo` |
| The homepage (online demo only, or `MDV_HOMEPAGE_PREVIEW=1`) | `home.html` → `src/home/home.ts` (no framework) | `frontend/app/home.html` | `/` in the demo |

Their scripts and stylesheets are hashed files in `frontend/app/assets/`
(`/static/app/assets/`). Images that are not part of the build (favicon, homepage
screenshots, the fish) stay in `frontend/assets/`.

Because the build is committed, nothing else needs Node: the server, the Python tests,
the packaged apps and the hosted demo use the files as they are. CI (`ci.yml`, job
*Frontend build is current*) rebuilds from this folder and the lockfile and fails if
the result differs from what is committed.

## Working on it

Node 24 and npm, then:

```powershell
cd web
npm ci            # exactly the versions in package-lock.json
npx tsc -b        # type check
npx vite build    # writes ../frontend/app/; commit it with the source change
```

For live editing, run the backend from this checkout on 8422 and Vite's dev server:

```powershell
uvicorn backend.main:app --host 127.0.0.1 --port 8422
cd web; npx vite   # http://127.0.0.1:5173/static/app/ , /api proxied to 8422
```

Then build and commit `frontend/app/` before opening a pull request.

## Source layout

```text
src/
  app/           shell: App, Sidebar, AmbientLight, UpdateNotice, heartbeat, update rules
  api/           requests: http.ts (JSON + errors), local.ts (/api/*, Local Mode only),
                 connector.ts (127.0.0.1:8765, company targets only), messages.ts
  types/         response shapes from the server (review, diagnostics)
  features/
    connection/  Local login, connector pairing, approved targets, database discovery
    review/      SQL editor, result, statement detail; reviewText.ts holds every sentence
    dashboard/   the current review at a glance
    diagnostics/ observed performance and lock waits (one shared snapshot mechanism)
  shared/        environment (demo flag, local server), storage, theme, icons
  styles/        tokens, base, ambient light, controls, shell, one file per view
  home/          the homepage's script and stylesheet (shares tokens and the light)
```

Rules the tests hold this code to (`tests/test_frontend_*.py`, `test_homepage.py`,
`test_update_check.py`, `test_public_demo.py`):

- Company (remote) work goes only to the connector, by approved `target_id`; the public
  `/api/*` routes are called only from `api/local.ts`, for MySQL on this computer.
- The connector session comes only from explicit pairing and lives in memory.
- Browser storage holds only per-viewer conveniences (theme, ambient light, editor
  split, update versions), through `shared/storage.ts`. Never a login, token, SQL or
  database name.
- A database is used only if it came from discovery for this target, login and session.
- Messages for stable server codes are fixed text (`api/messages.ts`, `reviewText.ts`).
- In the online demo the page offers no connection, diagnostics, heartbeat or update
  check; the server refuses them whatever the page does.

## Why this stack

- **React + TypeScript**: the workspace has real shared state (one connection used by
  review, diagnostics and the dashboard; async flows that must discard stale results).
  Typed components and modules replaced a 2,600-line script and its DOM wiring.
- **Vite**: one static build with hashed file names, no server at runtime. FastAPI keeps
  serving the files under its existing CSP (`default-src 'self'`); nothing was relaxed.
- **Cloudscape, selectively**: only its Select (target and database lists: keyboard,
  filtering, screen-reader behaviour), themed through its supported tokens
  (`shared/cloudscapeTheme.ts`), never its internal classes. Its base stylesheet without
  the embedded font is used, because the CSP refuses `data:` fonts.
- **No CSS framework or animation library**: tokens and plain CSS cover the design;
  motion is CSS transitions and keyframes, all off under `prefers-reduced-motion`.
- **The homepage has no framework**: it is static content; it shares the tokens, base
  styles and ambient light, which is all it needs to look like the product.

Costs, stated plainly: the validator's script is about 1.2 MB (270 KB gzipped), most of
it React and Cloudscape (including its runtime theming); the build output is committed,
so a frontend change also changes `frontend/app/`.
