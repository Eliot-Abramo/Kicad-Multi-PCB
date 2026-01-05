# Multi-Board PCB Manager (KiCad Plugin)

A KiCad Action Plugin that lets you manage **multiple PCB files** (“sub-boards”) that all share **one schematic**.

The workflow is intentionally simple:

- One schematic is the **source of truth**.
- Each sub-board gets its own `.kicad_pcb`.
- Components are **assigned to a board** by *placing them on that board*.
- When you press **Update**, the plugin:
  - imports components from the schematic netlist,
  - **skips** anything already placed on another board,
  - updates values / footprints,
  - assigns nets,
  - and drops any newly-added footprints into a neat grid so you can pack them properly.

---

## Contents

- [Why this exists](#why-this-exists)
- [Project model](#project-model)
- [Install](#install)
- [Quick start](#quick-start)
- [Project structure](#project-structure)
- [How schematic sharing works](#how-schematic-sharing-works)
- [How component ownership works](#how-component-ownership-works)
- [Update pipeline](#update-pipeline)
- [Ports](#ports)
- [Health / DRC / status tools](#health--drc--status-tools)
- [KiCad-specific subtleties](#kicad-specific-subtleties)
- [Performance notes](#performance-notes)
- [Troubleshooting](#troubleshooting)
- [Extending the plugin](#extending-the-plugin)
- [References](#references)

---

## Why this exists

KiCad is very good at “one schematic → one PCB”.  
It’s less direct when you want:

- one schematic,
- *multiple* PCBs (different physical areas, rigid-flex sections, daughterboards, panelized variants, etc.),
- and still keep a single source of truth for symbols, connectivity, and BOM data.

This plugin provides a pragmatic middle ground that stays compatible with the normal KiCad file formats:
it doesn’t invent a new “meta format”, it just creates normal boards and normal libraries, and uses `kicad-cli`
to export the netlist.

---

## Project model

The plugin stores a small JSON file (`.kicad_multiboard.json`) at the project root.

- The **root schematic** is shared across all sub-boards.
- Each **BoardConfig** points to its own `.kicad_pcb` path.
- Each board can define **ports** (named connection points) that show up as pads on the generated “block footprint”.

### Architecture at a glance

```mermaid
flowchart LR
  UI[dialogs.py (wx UI)] -->|calls| MGR[manager.py (engine)]
  MGR -->|loads/saves| CFG[.kicad_multiboard.json]
  MGR -->|uses| PCBNEW[pcbnew API]
  MGR -->|exports netlist| CLI[kicad-cli]
  MGR -->|writes libs| BLK[MultiBoard_Blocks.pretty]
  MGR -->|writes libs| PORT[MultiBoard_Ports.pretty]
```

---

## Install

### Requirements

- **KiCad 9.0+** (targeted; earlier versions may work but are not guaranteed)
- `kicad-cli` available in PATH (or installed in a standard KiCad location)
- Python environment shipped with KiCad (the plugin uses `pcbnew` + `wx`)

> I don’t know every lock-file naming variant across every KiCad version/OS combo.  
> The open-board detection covers common patterns and the current-instance board reliably, but it’s still “best effort”.

### Plugin folder locations (KiCad 9)

Copy this plugin folder into the KiCad scripting plugins directory:

- **Windows:** `%APPDATA%/kicad/9.0/scripting/plugins/`
- **Linux:** `~/.local/share/kicad/9.0/scripting/plugins/`
- **macOS:** `~/Library/Application Support/kicad/9.0/scripting/plugins/`

---

## Quick start

1. Open any PCB in the project (root PCB or a sub-board).
2. Run **Tools → External Plugins → Multi-Board Manager**.
3. Click **New** and create a board.
4. Double-click the board row (or press **Enter**) to open it.
5. In the opened board:
   - place a few footprints,
   - save,
   - go back to the manager and click **Update**.
6. New components from the schematic will appear (packed into a grid near the origin).

### “Ownership” rule (the mental model)

A component ref (e.g., `R12`) “belongs” to the first board where it is already present.
When updating a board, the plugin will skip refs that it detects on other boards.

This is the key rule that makes the whole thing deterministic without needing extra annotation in the schematic.

---

## Project structure

Typical project layout after creating a couple of sub-boards:

```text
my_project/
  my_project.kicad_pro
  my_project.kicad_sch
  my_project.kicad_pcb               # optional "root" PCB
  .kicad_multiboard.json             # plugin config
  fp-lib-table                       # project footprint lib table (may be created/edited)
  sym-lib-table                      # copied into sub-board dirs for convenience

  MultiBoard_Blocks.pretty/          # generated block footprints
    Block_Power.kicad_mod
    Block_IO.kicad_mod
  MultiBoard_Ports.pretty/           # generated port markers
    Port_USB.kicad_mod

  boards/
    Power/
      Power.kicad_pro
      Power.kicad_sch                # hardlink/symlink/copy to root schematic
      Power.kicad_pcb
      fp-lib-table                   # copied with ${KIPRJMOD} resolved
      sym-lib-table
      (hierarchical sheets copied/linked too)
    IO/
      IO.kicad_pro
      IO.kicad_sch
      IO.kicad_pcb
      fp-lib-table
      sym-lib-table
```

---

## How schematic sharing works

When a board is created or updated, the manager runs `_setup_board_project()`:

1. Creates a `.kicad_pro` file in the sub-board directory (if missing).
2. Creates a `.kicad_sch` next to the board that points at the root schematic by:
   - hardlink (preferred),
   - symlink (fallback),
   - or a plain copy (final fallback).
3. Scans the root schematic for hierarchical sheet references and links/copies those too.
4. Copies `fp-lib-table` and `sym-lib-table` into the sub-board directory with `${KIPRJMOD}` expanded to the *root* project directory.

### Why the library-table copy matters

KiCad resolves `${KIPRJMOD}` relative to the project file you opened.
If each board has its own `.kicad_pro`, you want it to resolve paths the same way as the root project.

So this plugin copies tables and replaces `${KIPRJMOD}` with an absolute path to the root.
It’s not the most elegant thing in the world, but it avoids “works in root, breaks in sub-board” library weirdness.

---

## How component ownership works

The plugin decides “where a reference lives” by scanning all sub-boards:

- `scan_all_boards()` loads each `.kicad_pcb` and records:
  - `ref -> (board_name, footprint_id)`
- Anything starting with `#` or `MB_` is ignored.

When you update a specific board:
- if `R12` exists in **another** board file, it is skipped.

That means you can “move” a component between boards by:
1. deleting it from board A,
2. updating board B,
3. and it will then be placed on board B.

---

## Update pipeline

This is the core loop: “sync board from schematic”.

### Pipeline diagram

```mermaid
flowchart TD
  A[Update board] --> B[Refesh schematic links + tables]
  B --> C[Scan all boards to build ownership map]
  C --> D[Export netlist via kicad-cli]
  D --> E[Parse netlist (fast XML parse)]
  E --> F[Load target PCB]
  F --> G[Split refs into: update vs add]
  G --> H[Update existing footprints]
  G --> I[Add missing footprints]
  I --> J[Pack new footprints in grid]
  H --> K[Assign nets]
  J --> K
  K --> L[Save board]
```

### Important details inside that pipeline

#### Netlist export
The plugin uses:

- `kicad-cli sch export netlist --format kicadxml`

If `kicad-cli` is missing, update will fail.

#### Netlist parsing (DNP / Exclude from board)
KiCad’s exported netlist expresses boolean properties in a way that can be surprising:
sometimes the “true” state is an empty string.

The parser treats a part as **skipped** if:

- it has a `DNP` property whose value is `yes/true/1/dnp` **or empty**, or
- it has any “exclude from board” property whose value is `yes/true/1` **or empty**, or
- its value text is literally `"DNP"`, or
- it has no footprint assigned.

This is implemented in `_parse_netlist_optimized()`.

#### Footprint replacement
If a footprint exists but the netlist footprint ID changed:

- the old footprint is removed,
- a new one is loaded and inserted,
- position / rotation / layer are preserved.

Why “load fresh” every time?
Because the `pcbnew` Python API is SWIG-based, and certain object reuse patterns can get crashy or weird.
So `FootprintResolver` caches **library paths**, not footprint objects.

#### Net assignment
Nets are assigned by:

- ensuring the net exists on the board (`NETINFO_ITEM` is created if missing),
- matching netlist nodes to refs/pins,
- `FindPadByNumber(pin)` and `pad.SetNet(netinfo)`.

This is deliberately direct: it avoids trying to mimic the whole KiCad “Update PCB from Schematic” tool,
and instead focuses on getting correct connectivity for routing.

---

## Ports

Ports are a lightweight way to document / enforce inter-board connectivity.

Each board has a `ports` dict in config:

- port name (e.g. `USB_D+`)
- optional net override (if the net name differs from the port name)
- side: left/right/top/bottom
- position: 0.0 → 1.0 along that edge

### What ports do

1. Ports appear as pads on the generated **block footprint** for that board.
2. DRC “unconnected” violations involving those nets can be filtered from reports (because those nets
   are expected to leave the board via connectors).

### Block footprints

When you create a board, `_generate_block_footprint()` writes `Block_<BoardName>.kicad_mod` into:

- `MultiBoard_Blocks.pretty/`

The generated footprint includes:
- silk/fab outlines,
- labels,
- port pads and names,
- and is tagged with attributes so it behaves like a non-BOM “board-only” object.

---

## Health / DRC / status tools

The UI provides additional tools that are basically “sanity check accelerators”.

### Health report
`get_full_health_report()` / `get_board_health()` includes:
- file exists
- component count (rough)
- is open (lock file / active board)
- ports count
- last modified timestamp

### DRC / connectivity check
`check_connectivity()` runs:

- `kicad-cli pcb drc --format json`

Then it filters out “unconnected” violations if they refer to nets used by ports.
That way you don’t get spammed for intentional off-board connections.

### Status view
`get_status()` compares:
- the set of “valid” components from netlist (not skipped),
- against refs found across boards,
and reports “missing refs”.

---

## KiCad-specific subtleties

This plugin is built around a few KiCad realities.

### 1) “Don’t update boards that are open”
Updating a `.kicad_pcb` that is open in KiCad can lead to:
- file write conflicts,
- corrupt saves,
- random “operation failed” messages.

So `update_board()` hard-blocks when the board looks open.

Detection:
- If the board is the active board in *this* KiCad instance → open.
- Otherwise, if a lock file exists next to it → open.

Common lock patterns checked:
- `~board.kicad_pcb.lck`
- `.~lock.board.kicad_pcb#`
- `board.kicad_pcb.lck`

If KiCad crashes, lock files can be left behind. In that case, you may need to delete the lock file manually.

### 2) SWIG / lifetime quirks
Some pcbnew objects behave like they’re “owned” by the board or by internal C++ structures.
Reusing footprints loaded once and cloned repeatedly can be unstable.

This is why:
- the resolver caches *paths*, not footprint objects,
- and each footprint load is a fresh call to `pcbnew.FootprintLoad()`.

### 3) `${KIPRJMOD}` is relative
If each board has its own `.kicad_pro`, then `${KIPRJMOD}` is different depending on what you opened.
Copying library tables into each sub-board with `${KIPRJMOD}` expanded avoids subtle path resolution bugs.

### 4) Netlist booleans are weird
As mentioned above: empty-string boolean “true” shows up in some exports.
The parser treats empty string as “true” for those properties.

---

## Performance notes

This plugin is written to be “fast enough” on real projects, not benchmark-winning.

The main performance choices:

- **Board scan caching:** `scan_all_boards()` results are cached until invalidated.
- **Fast XML parsing:** uses `lxml` if available (fallback to `xml.etree`).
- **Avoid footprint object caching:** reduces weird SWIG issues.
- **Grid packing:** fast, deterministic placement (then you can use KiCad’s native packing tool).

There’s also a `ThreadPoolExecutor` import; in the current code path most operations are kept single-threaded
because KiCad’s API calls are not guaranteed thread-safe. (In other words: parallelizing the wrong thing
is a fantastic way to create “Heisenbugs”.)

---

## Troubleshooting

### Update says “kicad-cli not found”
- Ensure KiCad is installed with `kicad-cli`.
- On Linux/macOS, verify `kicad-cli` is in PATH.
- On Windows, the plugin searches common KiCad install paths.

### Update says the board is open (lock file present)
- Close that PCB in KiCad (including other KiCad windows).
- If KiCad crashed earlier, delete the lock file next to the PCB.

### Footprints fail to load
The footprint resolver tries, in order:
1. project fp-lib-table nicknames,
2. KiCad shared footprint libraries,
3. direct `pcbnew.FootprintLoad(lib_nick, fp_name)`.

So if loads fail:
- verify the footprint ID is correct in the schematic,
- verify the footprint library exists and is accessible,
- verify the project `fp-lib-table` has correct URIs (especially `${KIPRJMOD}` usage).

### Components don’t show up on a board
Common causes:
- That ref already exists on another board (ownership rule).
- The component is DNP / excluded from board.
- The component has no footprint in the schematic.

### Status panel says “missing refs”
That means:
- the netlist contains refs that are considered valid,
- but they are not found on any board file.

The fix is usually:
- open the board you want them on,
- press Update.

---

## Extending the plugin

Places to start:

- `manager.py`
  - `update_board()` for the main pipeline
  - `_parse_netlist_optimized()` for any schematic-driven rules
  - `_generate_block_footprint()` if you want different block visuals
  - `is_pcb_open()` if you want stronger or different “open board” detection
- `dialogs.py`
  - `MainDialog` for UI actions and keyboard shortcuts
  - dialogs for health/diff/status if you want more output

### Debug logging
The manager writes to:

- `multiboard_debug.log` (project root)

If something fails in the field, this is the first place to look.

---

## License

MIT (see header in source files).
