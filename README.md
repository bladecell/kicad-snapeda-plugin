# SnapEDA Parts Search for KiCad

KiCad 10 PCB and Schematic Editor IPC action opens a native wxPython search window for SnapEDA parts, with symbol/footprint/3D availability, image preview, and authenticated HTTP export. Linux is the first supported platform. Sign-in is entered locally; credentials and cookies are held in memory. Optional password remembering uses an available secure OS keyring and is off by default. The plugin does not read or write a regular browser profile.

This project is distributed under **GPL-3.0-or-later**; see [LICENSE](LICENSE).

## Requirements

- KiCad 10 with its system Python 3.14 and wxPython 4.2.5 runtime.
- `curl_cffi`, `keyring`, and `kicad-python` installed for that same Python environment (`python -m pip install -r requirements-system.txt`, only where permitted). Keyring is optional at runtime; without an available supported OS backend, remembering is disabled and there is no file-based fallback.

The HTTP client uses KiCad's Python environment. An interactive anti-bot challenge or unsupported login flow fails closed; the plugin does not automate or bypass challenges.

## KiCad Plugin and Content Manager

The Linux PCM repository is [https://bladecell.github.io/kicad-snapeda-plugin/repository.json](https://bladecell.github.io/kicad-snapeda-plugin/repository.json). Add that repository in KiCad's Plugin and Content Manager. The archive packages the IPC action and runtime Python modules; PCM does not install Python dependencies or wxPython. Before running the action, select `SNAPEDA_KICAD_PYTHON` as described below or make `python3` resolve to the KiCad-capable interpreter. That interpreter must already provide KiCad's wxPython and `curl_cffi`; `keyring` is optional. The repository is intended for public GitHub Pages and publicly accessible GitHub Release assets.

The initial metadata version is `0.1.0` and remains marked `development` until a maintainer explicitly changes its status after validating installation in KiCad 10. The platform declaration is Linux only. The source tree includes a legacy browser helper for tests/development, but it and Camoufox are excluded from the PCM runtime archive.

## Build and verify a PCM release

Use Python 3.10+ with pytest and `jsonschema` available for tests. No Python runtime dependencies are installed by the packaging workflow.

```sh
python -m pytest -q tests/test_packaging.py tests/test_pcm_workflow.py
python tools/build_pcm.py --version 0.1.0 --output-dir dist
```

The builder produces a deterministic ZIP, `packages.json`, and `repository.json`. To retain published entries and older versions when building an update, pass a trusted existing `packages.json` explicitly with `--existing-packages PATH`. The builder rejects invalid versions and duplicate release versions. The release workflow treats only HTTP 404 as a missing first-release index; network failures and all other HTTP errors stop the run rather than discarding published history. Release workflows are serialized to avoid concurrent Pages updates.

Create a tag such as `v0.1.0` to run the tag-only release workflow. It runs the focused packaging tests, creates a GitHub Release with the PCM ZIP only if the pushed tag exists in the selected repository, and deploys the generated repository JSON to GitHub Pages using `GITHUB_TOKEN`. Before the first release, enable **Settings → Pages → Build and deployment → GitHub Actions**. The repository and release assets must be public for normal PCM clients to access them. Workflow actions use version tags and should be reviewed/pinned to verified commit SHAs according to repository policy.

The PCM metadata targets KiCad's official v2 schema (KiCad 10+) and follows the official [add-on packaging guide](https://dev-docs.kicad.org/en/addons/). The vendored test schema is from the official [KiCad metadata repository](https://gitlab.com/kicad/addons/metadata/-/raw/main/schema-v2.json); it is a validation fixture, not a runtime dependency. The existing `plugin.json` remains the IPC action manifest and is not PCM package metadata.

## Install the KiCad action

Run the one-time installer from this checkout with KiCad's system Python:

```sh
python3 install_plugin.py
```

It asks before copying to `${KICAD_DOCUMENTS_HOME}/10.0/plugins/snapeda-parts-search` (by default `~/.local/share/KiCad/10.0/plugins/snapeda-parts-search`) and refuses to replace an existing plugin directory. Restart KiCad and open the PCB Editor or Schematic Editor; use the **Search SnapEDA Parts** toolbar action. Set `SNAPEDA_KICAD_PYTHON` if the system Python with wxPython and curl_cffi is not discoverable as `python3`.

## Run standalone

From this checkout using KiCad's system Python:

```sh
python3 launch.py
```

Search for a part and select an export destination. On the first export, enter your SnapEDA username and password in the native dialog. Sign-in remains active only for this application session unless you explicitly enable keyring remembering. Use **Sign out** to clear the active session. The HTTP exporter makes a single login submission and does not retry credentials or handle interactive challenges.

## Import behavior and limitations

Choose project or personal/global scope. When IPC reports exactly one valid open PCB project, project imports automatically use `PROJECT/libs/SnapEDA/` for `SnapEDA.kicad_sym`, `SnapEDA.pretty/`, and `SnapEDA.3dshapes/`; the project-root `sym-lib-table` and `fp-lib-table` register the libraries once with `${KIPRJMOD}/libs/SnapEDA/...` paths. If project context is unavailable or ambiguous (including standalone and schematic-only launches), the plugin asks for the project root instead of guessing. New symbols are appended without replacing existing symbols; duplicate symbol names or footprint/model filenames are rejected. Imported 3D models use per-part subdirectories. Existing project libraries are left untouched and are not migrated. If the `SnapEDA` nickname is already registered at the old root-level location, import aborts and leaves the old library/table unchanged; manually resolve that conflict before importing to the new location. Global imports retain per-part libraries and update KiCad 10's user tables under `${KICAD_CONFIG_HOME}` or `~/.config/kicad/10.0`; the global asset directory is selected independently. Existing table entries and files are preserved; a nickname conflict aborts instead of taking over the entry. Global table changes require a separate confirmation.

Project imports serialize this plugin's work under a no-follow directory handle. Child library directories and asset/table files are opened relative to that pinned project directory; new assets are created without replacing a name another writer created first. The selected project directory and its ancestors must be trusted. Other library managers do not share the lock, and POSIX cannot provide a conditional atomic replacement against a non-cooperating writer changing an existing combined symbol or table file between the final check and rename. Do not edit the same project libraries or tables during import.

Only `.kicad_sym`, `.kicad_mod`, and STEP assets are extracted. HTML and other files are ignored, never executed. The importer bounds archive size and member count and rejects path traversal, symlinks, and duplicate filenames. HTTP ZIP downloads are capped at 64 MiB and use a private temporary directory. Image previews accept PNG/JPEG headers only and reject images above the pixel budget before decoding. STEP binding uses matching basenames or a unique one-footprint/one-model pairing. When pairing is ambiguous, the UI asks which STEP model belongs to each footprint. Model paths use `${KIPRJMOD}` for project libraries and absolute paths for global libraries. Confirm model orientation/scale in KiCad before fabrication.

Search bootstraps an anonymous session from SnapEDA's search page, then uses the site's website search endpoint; its response and token behavior may change without notice. Export uses the selected result's positive numeric `unipart_id` and public symbol/footprint IDs. If both symbol and footprint IDs exist but differ, export is disabled rather than guessing. A 3D model ID is never used as a KiCad symbol/footprint export ID. This mapping is based on observed website data and is not an official SnapEDA API contract. The project is not affiliated with SnapEDA.

The download limit is per file, not an aggregate disk quota. Temporary files and the saved copy may coexist, and monitoring can allow transient overshoot. Keep sufficient free disk space.

The part search dialog uses KiCad's read-only IPC document list to identify one open PCB project's root. It does not modify the board or schematic. KiCad does not provide verified schematic project context to this plugin; standalone or schematic-only use retains the project-root picker. Library-table writes are not followed by an unverified IPC reload command, so the user may need to reload libraries in KiCad for newly registered entries to become visible.

## HTTP login behavior

The HTTP module also retains a local diagnostic CLI for developers. It uses a no-echo terminal password prompt and can inspect a public part route without exporting. Credentials and cookies are not written to config, logs, or disk. The GUI asks for credentials through a masked field, makes one login submission, and gives a generic error without exposing raw response details. It does not support SSO, persistent sessions unless explicitly stored in a secure OS keyring, interactive CAPTCHA, or challenge bypass. Python cannot guarantee password bytes are erased from memory.

From this checkout using the Python environment with `curl_cffi`:

```sh
python -m snapeda_plugin.http_auth /path/to/new-component.zip --part-id 10314751 --unipart-id 4962805
```

The CLI still has a diagnostic default mapping for compatibility, so pass explicit IDs for other components. The output path must not already exist; downloads are capped at 64 MiB, checked as a ZIP with the existing KiCad archive validator, and written only after validation. Unsupported redirect/download hosts fail closed. The GUI uses only IDs from its selected search result and never the CLI default.

To investigate the missing general search-result-to-export ID mapping without downloading a file, use the canonical public part URL and its `unipart_id` from that part's search result:

```sh
python -m snapeda_plugin.http_auth --inspect-part-url 'https://www.snapeda.com/parts/PART/MANUFACTURER/view-part/' --expected-unipart-id 12345
```

The `12345` value is a placeholder for the selected result's actual numeric ID. Inspection does not imply the HTTP login or mapping has been verified for other parts.

KiCad integration follows its [IPC add-on guide](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/) and the official [plugin schema](https://go.kicad.org/api/schemas/v1). This plugin targets KiCad 10 IPC actions in both the PCB and Schematic Editors.

## Checks

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python -m compileall -q snapeda_plugin tests launch.py
```
