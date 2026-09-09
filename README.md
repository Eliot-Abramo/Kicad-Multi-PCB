<!-- Copyright (c) 2026 Eliot Abramo -->
<!-- SPDX-License-Identifier: MIT -->

# Multi-Board PCB Manager

[![KiCad-10.0](https://img.shields.io/badge/KiCad-10.0-blue)](https://www.kicad.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)

One schematic. Several PCBs. And you always know where every component is.

## Contents

- Getting going
  - [What it does](#what-it-does)
  - [Installation](#installation)
  - [Quick start](#quick-start)
  - [The window](#the-window)
  - [Keyboard shortcuts](#keyboard-shortcuts)
- Using it
  - [Finding components](#finding-components)
  - [How ownership works](#how-ownership-works)
  - [Assignment rules](#assignment-rules)
  - [A typical week](#a-typical-week)
  - [Updating a board](#updating-a-board)
  - [Ports](#ports)
  - [DRC](#drc)
  - [Fabrication output](#fabrication-output)
  - [Doctor](#doctor)
- Reference
  - [Command line and CI](#command-line-and-ci)
  - [Project layout](#project-layout)
  - [The config file](#the-config-file)
  - [Environment variables](#environment-variables)
  - [Troubleshooting](#troubleshooting)
  - [Compatibility](#compatibility)
- Project
  - [Architecture](#architecture)
  - [Upgrading from version 1](#upgrading-from-version-1)
  - [Contributing](#contributing)

---

## What it does

KiCad is built around one schematic driving one PCB. Real products often need
more than one board: a power board and a control board, a main board and a
daughterboard, a rigid section and a flex tail. There is no native support for
that in KiCad 10, and the usual workarounds either duplicate the schematic (which
then drifts) or abandon a single BOM.

This plugin keeps one schematic as the source of truth and gives each board its
own PCB, linked to that schematic rather than copied from it.

| Capability | What it means |
| --- | --- |
| **One schematic, many boards** | Each board directory holds a hardlink to the root schematic. Not a copy — the same file, so they cannot drift apart. |
| **Find any component instantly** | A cross-board index answers "where is this part?" without opening a single PCB. Search by reference, value, footprint, sheet, net, or status. |
| **Assignment you decide, not discover** | Rules driven by your schematic hierarchy: one rule per board assigns everything, including parts you add next month. |
| **Conflicts are reported, not hidden** | A part on two boards, a part placed somewhere other than where it was assigned, a part on a board but gone from the schematic — all surfaced with a suggested fix. |
| **Preview before you commit** | Update shows exactly what it will add, change, and remove, with a checkbox per row. Removals start unchecked. |
| **Doctor** | A preflight check with one-click repairs: broken schematic links, unresolvable libraries, stale lock files, malformed generated footprints. |
| **Works headless** | A CLI with no GUI and no `pcbnew` dependency, so CI can check conflicts, run DRC, and build fabrication output. |

**What it does not do.** It never writes your schematic. It never edits a board
that is open in KiCad. It does not do panelisation, and it does not merge boards
into one layout — each board stays a real, independent `.kicad_pcb` you lay out
by hand in KiCad as usual.

---

## Where is R42?

New addition, easy way to index and find components:

```text
Ctrl+P  →  type "R42"

R42   10k   Power   /Power/Regulators/   R_0402_1005Metric
      Assigned to Power by rule 1: sheet /Power/*
      Placed at 42.50, 18.25 mm on Power (front)
      Status: OK
```

Press Enter and KiCad zooms to it. If it's on a different board, the plugin opens
that board and reveals the component there.

Same question from a terminal or a CI job:

```console
$ multiboard where R42
R42  10k
  footprint  Resistor_SMD:R_0402_1005Metric
  sheet      /Power/Regulators/
  assigned   Power  [rule 1: sheet /Power/*]
  placed on  Power  at 42.50, 18.25 mm  0deg  front
  status     OK
```

---

## Installation

**Requirements:** KiCad 10.0 or newer (10.x only — see [Compatibility](#compatibility)).
`kicad-cli` ships with KiCad and is found automatically on all three platforms.

### From the Plugin and Content Manager

Open KiCad → **Plugin and Content Manager** → **Install from File...** and choose
the release ZIP.

> **Note:** the CI artifact is named `pcm-package.zip` and *contains* the ZIP you
> actually install. Unzip it once and select the `multiboard-*-pcm.zip` inside.
> The ZIP attached to a GitHub release needs no unzipping. Flattening the CI
> artifact is on the list for v2.1.

### For the command line

This is by far more complicated and requires the user to know how to get into the
kicad python environment and handle dependencies. Not recommended. Use .zip when
possible.

```console
pip install -e .        # from a clone
multiboard --help
```

The CLI never imports `pcbnew`, so it runs anywhere Python does — including a
container with no KiCad GUI. There are no runtime dependencies; `lxml` is used
if present and the standard library parser if not.

---

## Quick start

Open any PCB in your project and launch the plugin. On a project it has not seen
before it runs a four-step setup.

**1. Confirm the source of truth.** Which `.kicad_pro` and schematic drive the
boards. If there is only one candidate it is preselected.

**2. Check that linking works.** It actually creates and deletes a test hardlink
rather than assuming. Filesystem and permission problems are much easier to deal
with now than three steps later.

**3. Create your boards.** If your schematic has top-level hierarchical sheets it
offers one board per sheet, which is the usual structure. Each board gets a
directory under `boards/`, its own `.kicad_pro` and `.kicad_pcb`, and a hardlink
to the root schematic.

**4. Assign automatically.** One rule per sheet, so every component already knows
where it belongs before you place anything.

Then, for each board:

1. Select it in the **Boards** tab.
2. Press **Update...**, review the plan, and apply.
3. Press **Open in KiCad** and lay the board out normally.

That is the whole loop. Everything below is detail on the parts you will meet as
the project grows.

### If your schematic has no hierarchy

Step 3 will not have sheets to offer, so create boards by hand with **New
board...** and assign components with [reference-range or regex
rules](#assignment-rules) instead of sheet rules. Everything else is identical.

---

## The window

Two tabs, and three buttons in the header that are always available.

**Header:** **Find component (Ctrl+P)**, **Rules**, **Doctor**.

### Boards tab

One row per board, with a filter box above the grid.

| Column | Meaning |
| --- | --- |
| (colour) | The board's stable colour, used everywhere else in the UI |
| **Board** | Board name |
| **Placed** | Components physically on this board |
| **Pending** | Assigned to it but not laid out yet — these are what Update will add |
| **Conflicts** | Misplaced, duplicated, or orphaned components involving this board |
| **Ports** | How many ports it declares |
| **Description** | Yours to set |
| **Path** | Where the `.kicad_pcb` lives |

Buttons below: **New board...**, **Open in KiCad**, **Update...**, **Ports...**,
**Delete**, **Refresh**, **Run DRC**. Everything except New, Refresh and DRC
needs a board selected.

Right-click a row for the same actions plus **Rename...**, **Edit
description...**, **Build fabrication output**, and **Copy path**. Update and
Delete are greyed out while the board is open in KiCad.

Double-click a row to open that board in KiCad.

### Components tab

The cross-reference: every component in the project, wherever it lives.

| Column | Meaning |
| --- | --- |
| **Ref** | Reference designator |
| **Value** | From the schematic |
| **Footprint** | From the schematic |
| **Sheet** | Hierarchical sheet path |
| **Assigned** | The board it *should* be on |
| **Why** | What decided that — `rule 1: sheet /Power/*`, `pinned manually`, `field MB_Board = IO` |
| **Placed on** | The board it *is* on |
| **Side**, **X**, **Y** | Where on that board |
| **Status** | The [reconciliation status](#how-ownership-works) |

Above the table are a search box and **filter chips** — one per status and one
per board, each showing a live count. Click to filter, click again to clear.

Right-click one or more rows for **Go to component**, **Assign to board ▸**,
**Adopt placement as intent**, **Clear assignment**, **Copy reference(s)**,
**Copy row(s) as text**, and **Show nets...**.

Nothing in this tab writes a PCB or a schematic. Assignment changes are *intent
only*; they take effect on the board when you next run Update.

---

## Keyboard shortcuts

| Key | Does |
| --- | --- |
| `Ctrl+P` | Command palette — find a component anywhere |
| `Ctrl+F` | Jump to the Components tab and focus its search box |
| `Ctrl+N` | New board |
| `Ctrl+R` | Assignment rules |
| `F5` | Refresh the index |
| `Enter` | Boards tab: open the selected board in KiCad. Components tab: go to the selected component |
| `Delete` | Boards tab only: delete the selected board |
| `Esc` | Close |

Shortcuts are suppressed while you are typing in a text field, so `Delete` and
`Backspace` behave normally inside the filter and search boxes.

### The command palette

`Ctrl+P` opens a frameless search overlay. Type a reference to jump to it, or
type `>` for commands:

| Command | Does |
| --- | --- |
| `> reindex` | Re-read every board and the schematic |
| `> doctor` | Check the project for problems |
| `> rules` | Edit assignment rules |
| `> conflicts` | Show components whose intent and placement disagree |
| `> drc` | Run DRC on every board |
| `> xref` | Open the full cross-reference |

---

## Finding components

The search box in the Components tab and the command palette share one query
grammar.

| Filter | Example | Matches |
| --- | --- | --- |
| *(free text)* | `r42`, `10k`, `0402` | Reference, value, or footprint |
| `board:` | `board:Power` | Where it is, or where it is assigned |
| `sheet:` | `sheet:/Power/` | Hierarchical sheet prefix |
| `net:` | `net:GND` | Connected to that net |
| `fp:` | `fp:0402` | Footprint substring |
| `status:` | `status:duplicate` | [Reconciliation status](#how-ownership-works) |
| `side:` | `side:back` | Which side of the board |
| `dnp:` | `dnp:yes` | Do-not-populate flag |
| `origin:` | `origin:rule` | How it was assigned — `rule`, `pin`, `field`, or `none` |

Filters **AND** across different fields and **OR** within one field, so this is a
single query:

```text
net:GND status:ok side:front
```

and this finds anything on either board:

```text
board:Power board:IO status:duplicate
```

Search runs against the in-memory index — about 10 ms on a ten-thousand-component
design, filters included — so it genuinely keeps up with typing.

---

## How ownership works

Three layers, kept separate on purpose. Version 1 had only the middle one, which
is why a component placed on two boards silently became whichever board happened
to be last in a dictionary.

```text
INTENT        where a component should go
              ← an explicit pin, then the MB_Board field, then the first matching rule

REALITY       where it actually is
              ← read from the board files themselves, as a list of placements

RECONCILE     the difference between the two, classified and explained
```

Every component lands in exactly one state:

| Status | Meaning | Offered fix |
| --- | --- | --- |
| **OK** | Assigned to a board, placed on that board | — |
| **Not placed** | Assigned, not laid out yet | Update that board |
| **Unassigned** | Placed, but nothing assigns it | Adopt the placement as intent |
| **Misplaced** | Assigned to one board, placed on another | Reassign, or move it |
| **Duplicate** | Placed on more than one board | Shows every placement |
| **Orphan** | On a board, absent from the schematic | Remove it from the board |
| **No home** | In the schematic, not assigned, not placed | Assign it |
| **Skipped** | DNP, excluded from board, or no footprint | — |

**Misplaced**, **Duplicate** and **Orphan** are the three that count as
conflicts. They are what `multiboard check` reports and what the Conflicts column
counts.

Intent always records *why*, and the Components view has a column for it:
`rule 2: sheet /Power/*`, `pinned manually`, `field MB_Board = IO`. You never
have to guess, and you never have to open a JSON file to find out.

### Adopting placements

If you already have boards laid out, everything on them starts as **Unassigned**:
the part is somewhere, but nothing says it *belongs* there. Select the rows and
choose **Adopt placement as intent** to turn "this happens to be here" into "this
belongs here". Until you do, conflict detection has nothing to compare against.

---

## Assignment rules

**Rules** in the header, `Ctrl+R`, or **Rules → Suggest from sheets** for the
fast path.

| Kind | Example | Matches |
| --- | --- | --- |
| **Sheet path** | `/Power/` | That sheet and everything under it |
| | `/*/Filters/` | A `Filters` subsheet anywhere |
| **Reference range** | `R100-R199, U1, C10-C19` | Numerically — `R9` *is* inside `R1-R10` |
| | `R100-199` | Same as `R100-R199`; the second prefix is optional |
| | `J` | Every reference starting with `J` |
| **Regex** | `^TP\d+$` | Every test point (`re.fullmatch` against the reference) |

Useful details:

- A sheet rule needs no glob to be recursive. `/Power/` claims `/Power/` and
  every subsheet beneath it. Add `*` or `?` only when you want a pattern.
- A leading `/` is added for you, so `Power/` and `/Power/` are the same rule.
- Reference ranges compare the numeric part as a number. Lexically `"R9"` sorts
  after `"R10"`, which is why plain string ranges get this wrong.
- Reversed ranges are normalised: `R199-R100` means `R100-R199`.
- A half-typed or malformed rule is skipped, never fatal. The editor tells you
  when a rule has no valid terms.

The **first matching rule wins**, and rules can be reordered. The editor shows a
live count per rule and lists exactly what each one claims — a rule that looks
right but is shadowed by an earlier one shows zero, which is the feedback that
makes priority make sense. It also tells you which sheets no rule covers.

Anyone whose schematic is already organised hierarchically gets the whole design
assigned from one rule per board, and parts added later inherit automatically.

### Overriding a rule

Two things override rules, in this order:

1. **A manual pin.** Right-click any component (or a multi-selection) in the
   Components view → *Assign to board*. Undo it with *Clear assignment*.
2. **A schematic field.** Add a field called `MB_Board` to a symbol in Eeschema
   and set it to a board name. The plugin **reads** this field and never writes
   it — your schematic is not modified by this tool, ever.

Use a pin for a one-off. Use `MB_Board` when the assignment is a property of the
design that should live in the schematic and survive for anyone who opens it.
The field name is configurable — see [The config file](#the-config-file).

---

## A typical week

Once the project is set up, the loop is short.

**You added parts to the schematic.** Press `F5`. New components appear as **Not
placed** on whichever board their rule assigns. Select that board, **Update...**,
apply, and lay them out.

**You are not sure where something went.** `Ctrl+P`, type the reference, Enter.

**You moved a part to a different board by hand.** It shows as **Misplaced**,
because intent still says otherwise. Either reassign it (right-click → *Assign to
board*) or move it back.

**You want to know what is left to do.** Look at the **Pending** column, or click
the `Not placed` filter chip.

**Before a release.** Run **Doctor**, then `multiboard check
--exit-code-conflicts` and `multiboard drc --all`. Both are one line in CI.

**You renamed a board.** Assignments, rules and colours follow it automatically.

**You deleted a board.** It goes to `boards/.trash/`, not to `/dev/null`, and
every assignment and rule pointing at it is dropped.

---

## Updating a board

Update pulls components from the schematic onto a board. It always shows a plan
first:

```text
Update plan for board 'Power': Add 47, Update 3, Replace footprint 1, Remove 2, Skip 12

Add (47):
    C7   - Assigned to this board (rule 1: sheet /Power/*)
    ...
Remove (2):
  - R88  - Not present in the schematic
```

Every row has a checkbox. Rows prefixed `-` start **unchecked**: removals and
footprint replacements discard existing layout work, so accepting them is always
deliberate. **Safe selection** checks everything additive and nothing
destructive.

What Update does, in order:

1. Replace changed footprints, keeping position, rotation and layer
2. Add new components
3. Refresh values
4. Link each footprint to its schematic symbol
5. Clear stale nets
6. Apply nets from the netlist
7. Rebuild connectivity
8. Save

A board open in KiCad is never written to. Close it first, or use **Doctor →
Clear lock files** if KiCad crashed and left a lock behind.

To see what an update *would* do without opening the GUI:

```console
multiboard sync Power --dry-run
```

---

## Ports

Ports document where a net leaves a board — a connector, a flex tail, a
board-to-board header.

Select a board → **Ports...**. Each port has a name, a net (defaults to the
name), an edge, and a position along that edge from 0.0 to 1.0. They do two
things:

- become pads on the generated block footprint for that board, so you can place a
  representation of one board on another;
- suppress "unconnected" DRC violations for their nets, which are expected to
  leave the board.

Port markers and block footprints are generated into `MultiBoard_Ports.pretty`
and `MultiBoard_Blocks.pretty` in your project, and registered in the project
`fp-lib-table` automatically.

---

## DRC

**Run DRC** in the Boards tab, or `> drc` in the palette, runs `kicad-cli` design
rule checks across every board and collects the results in one report. Nets
declared as ports are filtered out of the "unconnected" violations, since leaving
the board is what they are for.

From the command line:

```console
multiboard drc --all                          # every board
multiboard drc --board Power --board IO       # named boards
multiboard drc --all --schematic-parity       # also check the boards against the schematic
multiboard drc --all --exit-code-violations   # exit 4 if anything is reported
multiboard drc --all --json report.json       # machine-readable
```

---

## Fabrication output

Right-click a board → **Build fabrication output**, or:

```console
multiboard fab --board Power
multiboard fab --all
multiboard fab --all --out release/          # choose the destination
multiboard fab --all --jobset custom.kicad_jobset
```

The first time a board needs one, a starter `fabrication.kicad_jobset` is written
into that board's directory with gerbers, drill files, placement, BOM and a STEP
model. From then on it is **yours**: the plugin never overwrites it, so tuning it
is a normal thing to do rather than something you have to fight.

Output goes to `.multiboard/fab/<board>/` unless you pass `--out`.

---

## Doctor

**Doctor** in the header, or `multiboard doctor`. It runs fourteen checks and
repairs most problems with one click.

| Check | Looks for |
| --- | --- |
| `kicad-cli` | Found, runnable, and a supported version |
| Version match | `kicad-cli` is the same KiCad version as the running editor |
| Root schematic | Configured and present |
| Link capability | This filesystem can actually hold hardlinks |
| Links | Every board's schematic link is intact |
| Boards exist | Every configured board directory is there |
| Board paths | Every `pcb_path` resolves to a real file |
| Block library | Generated block footprints parse |
| Locks | Stale `~*.lck` files from a crashed KiCad |
| Orphan directories | Board directories nothing in the config knows about |
| Rules | Rules that are malformed or point at a board that no longer exists |
| Cache writable | `.multiboard/` can be written |
| lxml | Present or absent (informational — the stdlib parser is used without it) |
| Trash | What is sitting in `boards/.trash/` |

Repairs offered include **Re-detect** `kicad-cli`, **Clear lock files**, and
**Regenerate blocks**. The same report is available as JSON:

```console
multiboard doctor --json
```

---

## Command line and CI

Every command accepts `-C DIR` / `--project DIR` to point at a project other than
the current directory, and `-v` for verbose output.

| Command | Does |
| --- | --- |
| `multiboard where REF` | Which board a component is on, and why |
| `multiboard index` | Rebuild the index and print a summary |
| `multiboard xref` | Full cross-reference of every component |
| `multiboard check` | Report assignment conflicts |
| `multiboard drc` | Design rule checks |
| `multiboard fab` | Fabrication output |
| `multiboard sync BOARD` | Preview an update to a board |
| `multiboard boards` | Placed / pending / conflicts per board |
| `multiboard doctor` | Check the project for problems |
| `multiboard version` | Print the version |

With their options:

```console
multiboard where REF [--json]
multiboard index [--json] [--force] [--no-export]
multiboard xref [--board B]... [--status S]... [-q QUERY] [--csv FILE] [--json]
multiboard check [--exit-code-conflicts] [--json]
multiboard drc [--board B]... [--all] [--schematic-parity]
               [--exit-code-violations] [--json FILE]
multiboard fab [--board B]... [--all] [--jobset FILE] [--out DIR]
multiboard sync BOARD [--dry-run] [--json]
multiboard boards [--json]
multiboard doctor [--json]
```

`--force` on `index` ignores the cache; `--no-export` skips the netlist export
and reads board data only, which is much faster when the schematic has not
changed. `--csv -` writes CSV to stdout. `--board` and `--status` are repeatable.

**Exit codes:** `0` success, `1` error, `2` not found, `3` conflicts,
`4` DRC violations.

A GitHub Actions job — the full file is in [docs/ci-example.yml](docs/ci-example.yml):

```yaml
jobs:
  boards:
    runs-on: ubuntu-latest
    container: ghcr.io/kicad/kicad:10.0
    steps:
      - uses: actions/checkout@v4
      - run: pip install --no-deps -e .
      - run: multiboard -C . doctor
      - run: multiboard -C . check --exit-code-conflicts
      - run: multiboard -C . drc --all --exit-code-violations
      - run: multiboard -C . xref --csv xref.csv
      - uses: actions/upload-artifact@v4
        with: { name: component-xref, path: xref.csv }
```

This works because the index, conflict checking, and cross-reference need only
file parsing, and DRC needs only `kicad-cli`. No `pcbnew`, no X server, no GUI.

---

## Project layout

```text
my_project/
├── my_project.kicad_pro
├── my_project.kicad_sch          ← the source of truth
├── my_project.kicad_pcb          ← optional top-level board
├── .kicad_multiboard.json        ← plugin config (commit this)
├── .multiboard/                  ← index cache, reports, logs (gitignored)
├── fp-lib-table
│
├── MultiBoard_Blocks.pretty/     ← generated board blocks
├── MultiBoard_Ports.pretty/      ← generated port markers
│
└── boards/
    ├── Power/
    │   ├── Power.kicad_pro
    │   ├── Power.kicad_sch       ← hardlink to the root schematic
    │   ├── Power.kicad_pcb
    │   ├── fabrication.kicad_jobset   ← created on first fab run, then yours
    │   └── fp-lib-table
    ├── IO/
    │   └── ...
    └── .trash/                   ← deleted boards land here, not /dev/null
```

Inside `.multiboard/`: the index cache, the debug log (`multiboard.log`), DRC
reports, and fabrication output.

Add to `.gitignore`:

```gitignore
.multiboard/
boards/.trash/
~*.lck
```

Commit `.kicad_multiboard.json`. It is deterministic — sorted keys — so it diffs
cleanly in review.

---

## The config file

`.kicad_multiboard.json` is storage rather than an interface: almost everything
in it is created and edited from the GUI, and every write is atomic and leaves a
`.bak`. Two settings have no GUI yet and can be edited by hand.

| Key | Default | Meaning |
| --- | --- | --- |
| `schema` | `3` | Config format version. Managed for you; older files migrate automatically. |
| `plugin_version` | current | Which version last wrote the file. |
| `root_schematic` | — | The schematic every board links to. |
| `root_pcb` | — | Optional top-level board. |
| `variant` | `""` | **No GUI.** A KiCad 10 design variant to export netlists against. |
| `board_field` | `MB_Board` | **No GUI.** The schematic field read for assignment. |
| `assignments` | `{}` | Manual pins, as `{"R42": "Power"}`. |
| `rules` | `[]` | Assignment rules, in priority order. |
| `board_colors` | `{}` | Stable colour per board. |
| `boards` | `{}` | The boards themselves: path, description, block size, ports. |

If you edit it by hand, close the plugin first — it writes the whole file on
change and will overwrite you otherwise.

---

## Environment variables

| Variable | Effect |
| --- | --- |
| `KICAD_CLI` | Full path to `kicad-cli`, overriding auto-detection. The fix for a non-standard install. |
| `KICAD_CONFIG_HOME` | Where KiCad keeps its configuration, if you have moved it. |
| `KICAD10_3RD_PARTY` | Where PCM-installed plugins live, if you have moved it. |

---

## Troubleshooting

**Run Doctor first.** It checks fourteen things and repairs most of them with one
click.

| Symptom | Cause and fix |
| --- | --- |
| `kicad-cli not found` | Set `KICAD_CLI` to its full path. On macOS it lives inside the app bundle at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` and is not on `PATH`. Doctor → *Re-detect*. |
| "Board is open in KiCad" | Close it. If KiCad crashed, Doctor → *Clear lock files*. |
| "Cannot link schematic" | The project and `boards/` must be on the same filesystem. On Windows, hardlinks need Developer Mode or Administrator. Network drives do not work. |
| Footprints fail to load during Update | A library is not registered for this project. Doctor checks the project *and* global `fp-lib-table` and reports unresolvable `${...}` variables. |
| Components do not appear after Update | Check the Components tab: they may be assigned elsewhere, DNP, excluded from board, or have no footprint. The Update plan states the reason per component. |
| Everything shows as "Unassigned" | Nothing assigns those parts yet. Create a [rule](#assignment-rules), or select all and *Adopt placement as intent*. |
| A rule matches nothing | An earlier rule already claimed those components — first match wins. The rules editor shows a live count per rule; reorder it. |
| Block footprints will not open | If this project was created with version 1, every generated block footprint is malformed. Doctor → *Regenerate blocks*. |
| Search feels stale | Press `F5`, or `> reindex` in the palette. The index is cached per board on modification time, so this is normally instant. |
| A very large board is slow to scan | Scanning costs about 0.25 s per MB, and results are cached on modification time, so only an edited board is re-read. |

The debug log is at `.multiboard/multiboard.log`. Run the CLI with `-v` for the
same detail on stderr.

---

## Compatibility

| KiCad | Status |
| --- | --- |
| 10.x | **Supported.** |
| 9.x and earlier | Not supported. Use [release v1](https://github.com/Eliot-Abramo/Kicad-Multi-PCB/releases). |
| 11.x | Not yet. KiCad 11 removes the SWIG `pcbnew` bindings this plugin is built on. |

Tested on Linux, macOS and Windows, against Python 3.9 through 3.13.

On KiCad 11: the replacement is the IPC API, but as of KiCad 10 it cannot read
schematics, cannot run headless, and explicitly cannot open or switch documents —
all three of which this plugin's workflow requires. The port is planned and the
code is already structured for it; see
[`multiboard/backend/ipc_backend.py`](multiboard/backend/ipc_backend.py) for
exactly what is blocked and what changes when 11 lands. The CLI already works
without `pcbnew` and is unaffected.

---

## Architecture

```text
multiboard/
├── core/          pure Python — never imports pcbnew or wx
│   ├── sexpr.py       tolerant s-expression scanner
│   ├── pcb_scan.py    read a .kicad_pcb as text
│   ├── netlist.py     read the schematic via kicad-cli
│   ├── index.py       the three-layer ownership model
│   ├── rules.py       assignment rules
│   ├── plan.py        what an update would do
│   ├── doctor.py      diagnostics and repairs
│   └── workspace.py   a project, without KiCad
├── backend/       the only place that imports pcbnew
├── ui/            wxPython
├── cli.py         headless entry point
└── compat.py      KiCad 10 API surface and probes
```

`core/` may not import `pcbnew` or `wx`. That rule is enforced by a test that
walks the AST of every module, and it is what buys two things: the CLI runs on a
bare Python, and the KiCad 11 port is a bounded change rather than a rewrite.

Reading a board as text rather than through `pcbnew.LoadBoard` avoids building
the connectivity engine and design-rule state that a read-only query never uses.
That is worth 1–3 seconds per board, it runs off the GUI thread, and it is what
makes per-keystroke search across every board possible.

### Development

```console
pip install -e ".[dev]"
pytest                                    # 373 tests, no KiCad needed
ruff check multiboard/ tests/ tools/
ruff format --check multiboard/ tests/ tools/
python tools/check_version.py             # every version string agrees
python tools/build_package.py             # reproducible PCM archive
```

---

## Upgrading from version 1

Your `.kicad_multiboard.json` is migrated automatically and losslessly on first
open; a `.bak` is kept. Boards, descriptions, and ports carry over. Assignment
starts empty, which reproduces version 1's behaviour exactly — ownership derived
purely from placement — until you create a rule.

Two things are worth doing straight away:

1. **Doctor → Regenerate blocks.** Every block footprint version 1 wrote carries
   stray closing parentheses and cannot be parsed by KiCad. Nothing reported it
   because nothing ever tried to read them.
2. **Adopt your placements.** In the Components tab, select everything and choose
   *Adopt placement as intent*. That turns "this happens to be here" into "this
   belongs here", after which conflicts become meaningful.

Also fixed in this release, among others: the filter box (Backspace used to
prompt to delete a board), a stale netlist silently updating a board, footprints
never being linked to their schematic symbols, nets never being cleared, KiCad 9
being preferred over KiCad 10 when both were installed, `kicad-cli` being
undiscoverable on macOS, and a board-deletion path that could target the
project's parent directory.

> **On version numbers:** version 1 was published as `12.0`, under a scheme that
> tracked KiCad's own version. Releases now count from one, so this is version 2.
> Configs written by version 1 still record `"version": "12.0"`; that is expected
> and migrates cleanly.

---

## Contributing

Pull requests welcome. Please keep `core/` free of `pcbnew` and `wx` — there is a
test for it — add a test for anything behavioural, and run `ruff check` and
`ruff format` before submitting.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

Eliot Abramo — original idea, development, and maintenance.
