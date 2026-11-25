# SMI Acquire workstation monitor

The QueueServer worktree adds a React/Finch screen alongside the Panel app. On
the workstation, from the root of this worktree:

```bash
bash workstation/build.sh
pixi run python -m smi_acquire.workstation
```

The launcher opens the local browser. The default is completely simulated:
use **Jog sim_x** or enter seconds and click **Set exposure** to run `mvr` or `mv` on
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

## Commission one exposure, then one jog

On the live Camera page, a click shows a displacement preview using the
selected microscope YAML's saved beam center, piezo/Huber matrix and display
pixel stride (`ui.display_max_dim`), plus current chi readbacks. Compare with
the legacy Move panel on the same image feature and stack. Only click once in
the legacy app: its second click can commit a move. Finch image clicks are
preview-only. A legacy matrix loaded only into memory from the config store
must be compared separately from the YAML. `ui.default_step` is a jog choice,
not a calibration coefficient; the workstation's commissioning jog settings
are independent of it.

1. Launch with the live camera config alone. Open **Queue & Console →
   Capabilities for this workstation login**. Look for `mv`, `mvr`, the
   on-axis camera's exposure setpoint and the intended sample axis. The lists
   are the HTTP server's response for this key, without a group override.
   Verify the worker plan accepts a device name and number. If a plan or
   component is missing, review its worker registration, discovery depth and
   group permissions before changing the profile. No local EPICS write fallback.
2. Check the camera prefix against the worker device's write PV; its
   `AcquireTime` must correspond to the displayed camera's `AcquireTime_RBV`.
   A name appearing in the allowed list alone cannot establish that identity.
   For the trial, give QueueServer exclusive control; an idle worker cannot
   establish that an independent BSUI session is idle.
3. Set `CAMERA_CONFIG` to the verified YAML path and `EXPOSURE_DEVICE` to the
   verified **worker device name**, then restart with only exposure enabled:

   ```bash
   pixi run --locked python -m smi_acquire.workstation \
     --live-camera-config "${CAMERA_CONFIG:?Set the microscope YAML path}" \
     --exposure-device "${EXPOSURE_DEVICE:?Set the verified worker signal}"
   ```

   Note the original readback. Pick a different suitable value in the existing
   0.0001–2 s UI range. Check `finished / completed` for the item UID **and**
   `Exposure RBV: matches request` from a subsequent direct read. A mismatch
   may indicate IOC rounding or a binding problem. Restore the original value
   and verify both again (if outside the UI range, defer the trial).
4. After exposure commissioning, verify one sample axis's installed mapping,
   limits, native units and motion direction. To enable that axis, also pass
   `--jog-axis "$JOG_DEVICE" --jog-step "$JOG_STEP" --jog-units "$JOG_UNITS"`
   (for piezo Y, `--jog-axis sample_piezo_y` alone defaults to 100 um)
   with reviewed values. Buttons request exactly ± that step. Observe the
   physical axis readback separately. Positive means positive in the installed
   motor coordinates; no camera or beamline-frame sign conversion is applied.

Commands use **immediate plan execution**, never queue insertion. Closed, busy,
locked or unknown worker state refuses the request. New clicks are blocked while
an item is unresolved. `accepted` is not completion. After a lost acknowledgement
the app shows `unknown` and searches history for the unique command tag to recover
the worker-assigned UID; it never retries a move. If history cannot resolve it,
check the separate queue-monitor and actual readback before restarting. Restarting
the app loses its local tracking state; it does not cancel a worker plan.

To return to a monitor-only session, omit the device bindings. The existing
Panel `app` and `app-real` tasks are separate.

### All sample piezos in one panel

Use `--piezo-jogs` instead of `--jog-axis`. Each row sends **one** immediate
`mvr` to its named worker device. X/Y/Z default to 100 um (`--jog-step`
overrides the translational step); theta and chi both default to 0.1 degree.
An explicit positive `--angular-step-deg` overrides the step for both
angular rows. The profile does not define piezo phi. `sample_piezo_th` maps to `piezo.th`, `sample_piezo_chi` to
`piezo.ch`. The labels use installed motor coordinates, with no sign or unit
conversion. Missing worker aliases remain disabled. Use the same flags without
`--live-camera-config` to exercise the five in-memory motors first.

## Queue & Console controls

Simulation starts with two 15-second demo items. Start the queue, pause and resume
the running item, then select **Stop after current item**. The current item should
finish and appear in history; the next stays pending. **Cancel scheduled stop**
withdraws that stop before the item finishes. Restart the simulator for fresh items.
The demo models queue lifecycle; it does not commission a live plan's checkpoints.

In live mode add `--queue-controls` to the normal workstation launcher to enable
these buttons. This flag is independent of the microscope device bindings; omit
all three kinds of control options for a monitor-only session. **Start queue**
starts the shared pending queue, not just a selected plan. **Pause at checkpoint**
requests a deferred RE pause; **Pause immediately** requests an immediate RE
pause. Neither is a motor emergency stop. Resume requires a paused plan. The app
checks fresh manager state and the displayed queue revision or running item UID
before sending a request; an acknowledgement is not a completed state transition.

**Reload idle environment** requires an idle open worker, autostart off and no
outstanding workstation move. It closes, waits for closure, opens and waits for
idle, then refreshes allowed plans/devices. Pending items are not cleared and
the queue is not started. This does not restart systemd or reload permissions
from disk. If a request times out or startup fails, inspect manager status and
worker console before doing anything else; the app does not retry control writes.

## Bookmarks: store edits vs. load plans

The **Align & Samples → Bookmarks** panel uses the existing `smi_plans` sample,
holder and magazine schema. Live access is opt-in: add `--bookmarks` to the
working camera launcher. The workstation uses its existing Redis credentials for
`swaxssamples` in db 2. No QueueServer plan is needed to create a holder, save a
sample, change notes/project/priority, or set the active sample *intent*. None
of those edits move hardware or create queue history. Refresh records after
changes from another application. There is no live-to-simulation fallback.

Commission first without any motion binding:

1. Use the simulator to create a uniquely named holder, expand it, enter a new
   sample name, and **Capture new sample here**. Select the sample by ID, save
   a notes field, and refresh. Simulator records last for the process lifetime.
2. On ws1 launch with `--bookmarks`, select/create an agreed commissioning holder,
   wait for the stage to settle, and capture one distinctly named sample. Compare
   the saved native motor readings with the beamline display. Capture reads the
   configured motor-record `.RBV` fields directly, sequentially (not an atomic
   hardware snapshot). Missing configured readbacks abort capture. Recorded
   machine coordinates have `frame="lab"`; they are not holder-relative offsets.
   Piezo chi has no Position field in schema 1, so it is stored as capture metadata.
3. Inspect the same holder/sample ID in the legacy client or via `SampleStore`;
   edit a note and refresh the other client. No worker reload is needed for this
   test. Writes use Redis WATCH/MULTI for linked records. Concurrent/stale edits
   require refreshing and reselecting the record. If the connection is lost
   during a save, refresh before retrying. **Set active intent** is not a mount
   operation. Replace nominal only when all configured axes are appropriate;
   the app refuses to replace a sample's nominal position if a refined pose exists.

After deploying the separate worker profile update, check for `load_sample` in
allowed plans. The QueueServer item takes **holder ID, sample ID, reviewed
sample.updated, and maximum per-axis piezo travel in µm**, not browser-supplied
coordinates. The worker resolves the shared record at execution, checks the
recorded measurement holder and version, and delegates to `smi_plans._core.goto_sample`
with explicit device bindings. This first load uses only the absolute piezo XYZ
of the refined (else nominal) position. It does not rotate, move the Huber,
transfer a holder, or update active intent. Verify the physical holder and axes
independently. Enable the button only after Y/Z and available travel are checked,
with `--bookmarks --bookmark-goto-max-um VALUE` for a chosen displacement cap.
If the store has no measurement holder, leave recall disabled until staff
verify and register the physically mounted holder through the existing workflow.
