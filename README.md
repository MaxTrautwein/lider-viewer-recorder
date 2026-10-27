# LidarViewer

Record and view data from Ouster lidar sensors (developed with an OS-0-128). A
[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two apps:

- **`recorder`** (`packages/recorder`) runs headless on the host that receives the sensor's UDP
  stream (its `udp_dest`). It records 1…N frames or a duration into an
  [OSF](https://static.ouster.dev/sdk-docs/reference/osf.html) file and exits. No GUI libraries.
- **`viewer`** (`packages/viewer`) opens such a file anywhere: 3D point cloud plus 2D image,
  colored by range, reflectivity, signal or near-IR, with playback and frame-by-frame stepping.

```
sensor ──UDP──▶ udp_dest host: recorder record -d 10s ──▶ recordings/*.osf
                                                               │ scp / rsync
                                          laptop: viewer ◀─────┘
```

OSF is Ouster's standard format: one compressed file (~0.3–1.5 MB per frame) with the
sensor metadata embedded and a frame index. `ouster-cli` and Ouster Studio open it too.

The recorder never changes the sensor configuration. The only exception is the
operating mode: by default it wakes a sensor in STANDBY for a recording and puts it back
afterwards (`recording.start` / `recording.standby_after`, off with `--no-start` /
`--no-standby-after`), and `sensor start|stop` switches it by hand.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Each app can be run without installing it (`uvx`),
installed as a tool, or run from a clone. The recording host only needs `recorder`, which pulls
in no GUI libraries.

**Without installing:** `uvx` builds a cached, throwaway environment for each run:

```sh
uvx --from "git+https://github.com/MaxTrautwein/lider-viewer-recorder#subdirectory=packages/recorder" recorder sensor info
uvx --from "git+https://github.com/MaxTrautwein/lider-viewer-recorder#subdirectory=packages/viewer" viewer recordings/
```

To pin a version, put `@<tag, branch or commit>` before the `#`, e.g.
`git+https://github.com/MaxTrautwein/lider-viewer-recorder@v0.1.0#subdirectory=packages/recorder`.

**Installed as a tool:** puts `recorder` / `viewer` on the `PATH`:

```sh
uv tool install "git+https://github.com/MaxTrautwein/lider-viewer-recorder#subdirectory=packages/recorder"
uv tool install "git+https://github.com/MaxTrautwein/lider-viewer-recorder#subdirectory=packages/viewer"
uv tool upgrade recorder viewer      # later: update to the latest commit
```

**From a clone:** for development, or to run the newest version without installing:

```sh
git clone https://github.com/MaxTrautwein/lider-viewer-recorder.git
cd lider-viewer-recorder
uv sync                              # development: both apps + pytest/ruff
uv sync --package recorder           # recording host: recorder only (headless)
```

The commands below are written as `recorder …` / `viewer …`, as for an installed tool. With
the other two methods, use this prefix instead:

| | recorder | viewer |
|---|---|---|
| uvx | `uvx --from "git+…#subdirectory=packages/recorder" recorder …` | `uvx --from "git+…#subdirectory=packages/viewer" viewer …` |
| clone | `uv run --package recorder recorder …` (keeps the GUI libraries out) | `uv run viewer …` |

Relative paths (`recordings/`, `recorder-local.yaml`, …) are resolved against the current
directory, so run the apps from the folder you want to work in, e.g. the clone.

## Configuration

Each app reads a YAML file, `recorder-local.yaml` or `viewer-local.yaml`, from the first of:

1. `--config PATH`
2. the current directory
3. `$XDG_CONFIG_HOME/lidar/` (default `~/.config/lidar/`)

The file is required. Create it from the template that ships with the app, then edit it:

```sh
recorder config init          # ./recorder-local.yaml   (--user: ~/.config/lidar/, --force: overwrite)
viewer config init            # ./viewer-local.yaml
```

The templates, [`recorder.yaml`](packages/recorder/src/recorder/recorder.yaml) and
[`viewer.yaml`](packages/viewer/src/viewer/viewer.yaml), document every key. All keys are
required, so a typo or a key missing after an update is reported instead of silently ignored.
`*-local.yaml` is gitignored.

- **recorder:** `sensor.host` (required, the template leaves it empty), HTTP timeout, UDP
  port / bind address / receive buffer / no-data timeout, output directory, file name pattern,
  allowed duration range, `start` / `standby_after` defaults.
  Command line options override it.
- **viewer:** `timezone` of the displayed timestamp (`UTC` or e.g. `Europe/Berlin`),
  `recordings_dir` (start folder of the Open dialogs, opened when no path is given and no folder
  was opened before).

After an update that adds keys, copy them from the template into your `*-local.yaml`; the error
message names the missing key. Keys that were removed are reported the same way (e.g.
`unknown key static` after the static model was dropped): delete them from your file.

## Recording (on the udp_dest host)

In a clone, use `uv run --package recorder recorder …`, or `uvx --from …` without one (see
[Setup](#setup)).

```sh
recorder sensor info                # status, udp_dest, warnings
recorder record --frames 1          # a single frame
recorder record -n 50               # 50 frames
recorder record --duration 30s      # within recording.min/max_duration_s (also: 5, 1500ms, 1m)
recorder record -d 10s -o lab.osf   # explicit file name
recorder record -d 5s --no-standby-after   # leave the sensor running afterwards
```

- Default file name: `recording.out_dir` / `recording.filename`, by default
  `recordings/<serial>_<YYYYmmdd_HHMMSS>_<n>f.osf`.
- A duration is converted to a frame count (10 Hz: `1s` = 10 frames).
- A recording starts with the next complete revolution.
- While recording, the file is named `*.osf.part`, so a file with the final name is always
  complete. Ctrl+C stops early and keeps the frames recorded so far.
- If no data arrives within `udp.no_data_timeout_s` (sensor in STANDBY with `--no-start`, or
  `udp_dest` is another host), it exits with code 2 and prints the sensor status.

`--sensor HOST` selects another sensor than `sensor.host`.

## Viewing

```sh
scp <udp_dest host>:lider-viewer-recorder/recordings/*.osf recordings/
viewer recordings/992000000000_20261021_150446_20f.osf
viewer recordings/   # folder: pick a recording from the side panel
viewer               # then File › Open (Ctrl+O) or drop an .osf file on the window
```

In a clone, use `uv run viewer …`, or `uvx --from …` without one (see [Setup](#setup)).

The window opens maximized. A folder (also via File › Open folder, Ctrl+Shift+O, or by dropping it on the window) opens
the **Recordings** panel, which lists its `.osf` files with size and date. Click one, or move with
↑ / ↓, to load it. View › Recordings shows or hides the panel. The last opened folder is
remembered (`~/.config/lidar/viewer.ini`) and reopened when the viewer starts without a path.

| Control | Action |
|---|---|
| Space | play / pause |
| ← / → | previous / next frame |
| Home / End | first / last frame |
| slider | jump to a frame |
| speed, Loop | playback speed 0.25×–4×, loop at the end |
| mouse | left-drag rotates, wheel zooms, middle-drag or Ctrl+left-drag pans |
| Color / Map | channel field and colormap (scaled to the 1st–99th percentile of the whole frame, so Azimuth and Max range do not change the colors) |
| Max range | hide points farther away (helps indoors) |
| Azimuth, Full | simulate the sensor's `azimuth_window` [start, end] in millidegrees (0…360000, start > end wraps through 0): points outside are hidden, so the two values can go straight into the sensor config. Full resets to 0…360000 |
| Edit › Copy frame timestamp (Ctrl+Shift+C), Copy | frame time in the configured `timezone` as a Postgres `timestamptz`, e.g. `2026-10-21 14:20:42.800342+00:00` |
| File › Save frame as PLY (Ctrl+S) | current frame as `.ply` (xyz + all fields) for CloudCompare/MeshLab |
| File › Export frame as PNG (Ctrl+E) | current frame as `<name>_2d.png` (the 2D image at the sensor's resolution, e.g. 1024 × 128, however far it is zoomed; transparent outside the azimuth window) and / or `<name>_3d.png` (the 3D view as shown, with or without the grid) |
| View › Show grid | show or hide the ground grid of the 3D view (remembered) |

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format .
uv run pytest            # uses a synthetic fake sensor, no hardware needed
```

Tests live in the root `tests/`: `test_recorder.py`, `test_viewer.py`, `test_config.py`,
`test_viewer_window.py` (viewer window, offscreen Qt), and `test_pipeline.py` (records with
the recorder, reads back with the viewer).
