# SMI Acquire workstation monitor

The QueueServer worktree adds a React/Finch screen alongside the Panel app. On
the workstation, from the root of this worktree:

```bash
bash workstation/build.sh
pixi run python -m smi_acquire.workstation
```

The launcher opens the local browser. The default is completely simulated:
click **Jog X +0.1** or **Toggle exposure** to run a Bluesky `mvr` or `mv` on
an in-memory signal. The synthetic camera shows the result. Open **RunEngine →
Queue & Console** for the simulated history and messages. This exercises the
UI and plan types, not an external manager or its permissions. Ctrl-C exits.

The build uses the sibling `smi-finch-vibes/frontend` checkout's installed
dependencies and modular library build (`dist`). If missing, run `npm ci` and
`npm run build` there with its supported Node version. Set `FINCH_FRONTEND` to
its absolute path for another layout. No CDN or browser API key is needed.

## Commission the live monitor

Use the beamline operator login on the workstation, with its provisioned
`QSERVER_HTTP_SERVER_API_KEY` and the confirmed HTTPS `QSERVER_HTTP_SERVER_URI`
in the shell environment. Supply the workstation's actual microscope YAML:

```bash
pixi run python -m smi_acquire.workstation --live-camera-config /path/to/microscope.local.yaml
```

The Python process binds to loopback, uses direct CA/PVA reads for the camera,
and polls the HTTP server for manager status, pending items, history and the
console text buffer. It shares one camera feed and one polling loop across
views. The console is a bounded, periodically refreshed tail, not a durable log.
An unavailable console is reported independently of the manager connection.
The API key stays in Python; a separate launch-scoped token protects the local
API. Stop the launcher before changing operator sessions.

Compare the monitor with the workstation queue-monitor and the camera viewer.
Live mode exposes no mutation endpoints in this first slice. The legacy
`pixi run app` / `app-real` tasks retain their original behavior. Before adding
live controls, verify allowed plans/devices for the authenticated group and
the physical axis/camera bindings. Immediate-plan completion must be correlated
by item UID; a queue acknowledgement or console line is not completion.
