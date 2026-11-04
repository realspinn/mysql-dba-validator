# Hosted online demo

`MDV_PUBLIC_DEMO=1` runs the server as the public online demo: the homepage at `/` and
the validator at `/demo`, for static analysis of SQL text only. The downloaded
application never sets it and is unchanged.

## What the server enforces

With `MDV_PUBLIC_DEMO=1` (read once at startup; requests cannot change it):

- Only these routes are served: `GET /`, `GET /demo`, `GET /api/health`,
  `POST /api/validate` and `GET /static/assets/*`. Everything else, including the
  connection test, database discovery, evidence, heartbeat and update-check routes and
  the API docs (`/docs`, `/redoc`, `/openapi.json`), returns 404 before route code runs.
- `POST /api/validate` accepts the SQL text alone. Any connection field (`host`,
  `port`, `username`, `password`, `database`, `mode`), even empty or null, is refused
  with 403 before the target is classified, so no host name is looked up and no socket
  or MySQL connection is opened. Unknown or nested fields are refused with 422; values
  are not echoed.
- At most two validations run at once in the server process (the start command runs
  one process); another request gets 503 with `Retry-After`.
- Request bodies are limited to 400 KiB (64 KiB of SQL at JSON's worst-case escaping,
  plus room). A larger declared `Content-Length` is refused at once; a chunked body or
  one that exceeds its declared length is cut off once the limit is reached.
- Only the host names in `MDV_ALLOWED_HOSTS` are answered (not `127.0.0.1` or
  `localhost` unless listed). Forwarded headers are not used.
- `/api/health` returns `{"status": "ok"}` and never contacts MySQL, even if `MYSQL_*`
  variables are set.
- The Content-Security-Policy allows the page to call only its own server (no local
  connector address). The page shows the SQL privacy notice and hides the connection,
  connector and database controls.

The server refuses to start if `MDV_PUBLIC_DEMO` is anything other than unset, `0` or
`1`, if `MDV_ALLOWED_HOSTS` has no valid host name, or if `CORS_ALLOW_ORIGINS` is set.
On Render (which sets `RENDER=true` in every service) it also refuses to start unless
`MDV_PUBLIC_DEMO=1`, so a missing setting cannot publish the full local application.

`MDV_HOMEPAGE_PREVIEW=1` is different: a local routing preview with none of the above.

## Download links and releases

The homepage's download menu opens GitHub's
`https://github.com/realspinn/mysql-dba-validator/releases/latest`, which GitHub
redirects to the release marked **Latest** (never a draft or, unless marked Latest by
hand, a prerelease). Publishing a desktop release therefore updates where visitors go
without redeploying the demo. The menu opens the release page rather than a file
because the asset names include the version.

Publishing a release does not redeploy the demo itself; it keeps running the commit
last deployed. A change to the demo's code still needs CI and a manual deploy of the
verified commit. The desktop app's update check reads GitHub's release API on the
user's machine; the Windows app then downloads the release file itself, and on macOS the
browser does. Neither uses these links.

## SQL privacy

Submitted SQL is sent to the hosted server and analysed in memory. The application does
not store it, log it or send it anywhere else. The server's access log records the
method, path and status of each request, not bodies. The hosting provider's own
request logging and retention are outside the application's control and are not
covered by this statement.

## Render (Free)

`render.yaml` describes one Python web service on the Free plan with auto-deploy off.
Creating it requires connecting the repository in the Render Dashboard; nothing in this
repository does that. If it is used as a Blueprint, also turn off **Auto Sync** in the
Blueprint's settings: auto-sync is on by default and applies `render.yaml` changes on
every push, independently of `autoDeployTrigger`.

| Setting | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health check path | `/api/health` |
| `MDV_PUBLIC_DEMO` | `1` |
| `MDV_ALLOWED_HOSTS` | the service's host name, such as `<name>.onrender.com`, plus any custom domain (comma-separated) |
| `PYTHON_VERSION` | `3.14.8` |

Set no database, connector or cloud credentials. The demo does not use them.

Render's documentation (checked November 2026) states for Free web services: 750
instance hours per workspace per month; spin-down after 15 minutes without inbound
traffic and about a minute to start again; a single instance; no persistent disk
(files are lost on restart or redeploy); outbound bandwidth and build minutes come from
the workspace allowance, and a Free service is suspended for the rest of the month if
bandwidth is exceeded without a payment method. Render terminates TLS and redirects HTTP
to HTTPS. Its health checks send the service's `onrender.com` name, or a verified custom
domain once there is one, as `Host`: add a custom domain to `MDV_ALLOWED_HOSTS` before
verifying it, or the health checks get 400 and Render restarts the service. The Free
instance's CPU and memory were not stated on the pages checked.

## Rollback and emergency stop

- **Stop public access at once:** suspend the service in the Render Dashboard (service
  settings). Deleting a service that a Blueprint manages is not enough: the next
  Blueprint sync recreates it. Remove it from `render.yaml` and sync first, then delete it.
- **Go back to a known-good commit:** Deploys, then Manual Deploy, then "Deploy a
  specific commit"; or roll back to a previous successful deploy whose build is still
  retained (Render keeps a plan-dependent number of builds). Choose only a commit that
  includes the public demo mode (this change or later). An earlier commit runs the full
  local application, with its connection routes, on the public address.
- **Fix a configuration mistake:** correct the environment variable and redeploy. A
  wrong `MDV_PUBLIC_DEMO` value or a missing `MDV_ALLOWED_HOSTS` stops the server from
  starting, so the previous deploy keeps serving.

If a security problem is found in the live demo, suspend the service first.

## Not covered

- A few expensive requests can still keep the demo busy: some 40 KiB inputs take 10 to
  20 seconds of CPU on a desktop machine, and longer on a small instance. The
  concurrency limit stops them piling up, so other visitors get an immediate "busy"
  answer rather than a long wait; it does not prevent the slowdown. There is no per-client rate limit: behind Render's proxy every request arrives
  from the proxy's address.
- The checks above are tested with the FastAPI test client, direct ASGI calls and a
  local Uvicorn process. They have not been verified on Render itself.
