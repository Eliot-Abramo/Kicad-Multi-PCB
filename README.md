# Multi-Board PCB Manager

[![KiCad-10.0](https://img.shields.io/badge/KiCad-10.0-blue)](https://www.kicad.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)

One schematic. Several PCBs. And you always know where every component is.

---

## Where is R42?

That question is why this exists. In a multi-board project the honest answer used
to be "open each PCB and look." Now it takes about a second:

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

## Contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [Getting started](#getting-started)
- [How ownership works](#how-ownership-works)
- [Assignment rules](#assignment-rules)
- [Updating a board](#updating-a-board)
- [Ports](#ports)
- [Command line and CI](#command-line-and-ci)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Compatibility](#compatibility)
- [Architecture](#architecture)
- [Upgrading from version 12](#upgrading-from-version-12)
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

---

## Installation

**Requirements:** KiCad 10.0 or newer (10.x only — see [Compatibility](#compatibility)).
`kicad-cli` ships with KiCad and is found automatically on all three platforms.

### From the Plugin and Content Manager

Open KiCad → **Plugin and Content Manager** → **Install from File...** and choose
the release ZIP.

### Manually

Copy the `multiboard/` directory into KiCad 10's plugin folder:

| OS | Path |
| --- | --- |
| Windows | `%APPDATA%\kicad\10.0\scripting\plugins\` |
| Linux | `~/.local/share/kicad/10.0/scripting/plugins/` |
| macOS | `~/Library/Application Support/kicad/10.0/scripting/plugins/` |

Then **Tools → External Plugins → Refresh Plugins**, and launch it from
**Tools → External Plugins → Multi-Board Manager**.

### For the command line

```console
pip install -e .        # from a clone
multiboard --help
```

The CLI never imports `pcbnew`, so it runs anywhere Python does.

---

## Getting started

Open any PCB in your project and launch the plugin. On a project it has not seen
before it runs a four-step setup:

1. **Confirm the source of truth.** Which `.kicad_pro` and schematic drive the
   boards. If there is only one candidate it is preselected.
2. **Check that linking works.** It actually creates and deletes a test hardlink
   rather than assuming — filesystem and permission problems are much easier to
   deal with now than three steps later.
3. **Create your boards.** If your schematic has top-level hierarchical sheets it
   offers one board per sheet, which is the usual structure.
4. **Assign automatically.** One rule per sheet, so every component already knows
   where it belongs.

Then, for each board: select it, press **Update**, review the plan, and apply.
Open the board in KiCad and lay it out.

---

## How ownership works

Three layers, kept separate on purpose. Version 12 had only the middle one, which
is why a component placed on two boards silently became whichever board happened
to be last in a dictionary.

```text
INTENT        where a component should go
              ← an explicit pin, or the MB_Board schematic field, or a rule

REALITY       where it actually is
              ← read from the board files themselves

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

Intent always records *why*, and the Components view has a column for it:
`rule 2: sheet /Power/*`, `pinned manually`, `field MB_Board = IO`. You never
have to guess, and you never have to open a JSON file to find out.

---

## Assignment rules

**Rules → Add**, or **Rules → Suggest from sheets** for the fast path.

| Kind | Example | Matches |
| --- | --- | --- |
| **Sheet path** | `/Power/` | Everything on that sheet and its children |
| | `/*/Filters/` | A `Filters` subsheet anywhere |
| **Reference range** | `R100-R199, U1, C10-C19` | Numerically — `R9` is inside `R1-R10` |
| | `J` | Every reference starting with `J` |
| **Regex** | `^TP\d+$` | Every test point |

The first matching rule wins, and rules can be reordered. The editor shows a live
count per rule and lists exactly what each one claims — a rule that looks right
but is shadowed by an earlier one shows zero, which is the feedback that makes
priority make sense. It also tells you which sheets no rule covers.

Two things override rules, in this order:

1. **A manual pin.** Right-click any component (or a multi-selection) in the
   Components view → *Assign to board*.
2. **A schematic field.** Add a field called `MB_Board` to a symbol in Eeschema
   and set it to a board name. The plugin **reads** this field and never writes
   it — your schematic is not modified by this tool, ever.

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

Rows prefixed `-` start unchecked. Removals and footprint replacements discard
existing work, so accepting them is always deliberate. "Safe selection" checks
everything additive and nothing destructive.

What Update does, in order: replace changed footprints (keeping position, rotation
and layer), add new components, refresh values, link each footprint to its
schematic symbol, clear stale nets, apply nets from the netlist, rebuild
connectivity, and save.

A board open in KiCad is never written to. Close it first, or use
**Doctor → Clear lock files** if KiCad crashed.

---

## Ports

Ports document where a net leaves a board — a connector, a flex tail, a
board-to-board header.

Select a board → **Ports**. Each port has a name, a net (defaults to the name),
an edge, and a position along that edge. They do two things:

- become pads on the generated block footprint for that board, so you can place a
  representation of one board on another;
- suppress "unconnected" DRC violations for their nets, which are expected to
  leave the board.

Port markers and block footprints are generated into `MultiBoard_Ports.pretty`
and `MultiBoard_Blocks.pretty` in your project, and registered in the project
`fp-lib-table` automatically.

---

## Command line and CI

```console
multiboard where REF                     # which board is this component on
multiboard index [--json] [--force]      # rebuild the index, print a summary
multiboard xref [--board B] [--csv F]    # full cross-reference
multiboard check --exit-code-conflicts   # exit 3 if any conflict exists
multiboard drc --all --exit-code-violations
multiboard boards                        # placed / pending / conflicts per board
multiboard sync BOARD --dry-run          # print the update plan
multiboard fab --all                     # fabrication output per board
multiboard doctor [--json]
```

Exit codes: `0` success, `1` error, `2` not found, `3` conflicts, `4` DRC
violations.

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
├── .multiboard/                  ← index cache, reports (gitignored)
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
    │   └── fp-lib-table
    ├── IO/
    │   └── ...
    └── .trash/                   ← deleted boards land here, not /dev/null
```

`.kicad_multiboard.json` is storage, not an interface. Everything in it is
created and edited from the GUI. It is deterministic (sorted keys) so it diffs
cleanly, and every write leaves a `.bak`.

Add to `.gitignore`:

```gitignore
.multiboard/
boards/.trash/
~*.lck
```

---

## Troubleshooting

**Run Doctor first.** It checks fifteen things and repairs most of them with one
click.

| Symptom | Cause and fix |
| --- | --- |
| `kicad-cli not found` | Set `KICAD_CLI` to its full path. On macOS it lives inside the app bundle at `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` and is not on `PATH`. Doctor → *Re-detect*. |
| "Board is open in KiCad" | Close it. If KiCad crashed, Doctor → *Clear lock files*. |
| "Cannot link schematic" | The project and `boards/` must be on the same filesystem. On Windows, hardlinks need Developer Mode or Administrator. Network drives do not work. |
| Footprints fail to load during Update | A library is not registered for this project. Doctor checks the project *and* global `fp-lib-table` and reports unresolvable `${...}` variables. |
| Components do not appear after Update | Check the Components view: they may be assigned elsewhere, DNP, excluded from board, or have no footprint. The Update plan states the reason per component. |
| Block footprints will not open | If this project was created with version 12, every generated block footprint is malformed. Doctor → *Regenerate blocks*. |
| Search feels stale | Press F5, or `> reindex` in the palette. The index is cached per board on modification time, so this is normally instant. |

The debug log is at `.multiboard/multiboard.log`.

---

## Compatibility

| KiCad | Status |
| --- | --- |
| 10.x | **Supported.** |
| 9.x and earlier | Not supported. Use [release v12](https://github.com/Eliot-Abramo/Kicad-Multi-PCB/releases). |
| 11.x | Not yet. KiCad 11 removes the SWIG `pcbnew` bindings this plugin is built on. |

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

Reading a board as text rather than through `pcbnew.LoadBoard` is roughly ten
times faster and runs off the GUI thread, which is what makes per-keystroke search
possible.

### Development

```console
pip install -e ".[dev]"
pytest                              # 280+ tests, no KiCad needed
ruff check multiboard/ tests/
ruff format --check multiboard/ tests/
python tools/check_version.py       # every version string agrees
python tools/build_package.py       # reproducible PCM archive
```

---

## Upgrading from version 12

Your `.kicad_multiboard.json` is migrated automatically and losslessly on first
open; a `.bak` is kept. Boards, descriptions, and ports carry over. Assignment
starts empty, which reproduces version 12's behaviour exactly — ownership derived
purely from placement — until you create a rule.

Two things are worth doing straight away:

1. **Doctor → Regenerate blocks.** Every block footprint version 12 wrote carries
   stray closing parentheses and cannot be parsed by KiCad. Nothing reported it
   because nothing ever tried to read them.
2. **Adopt your placements.** In the Components view, select everything and choose
   *Adopt placement as intent*. That turns "this happens to be here" into "this
   belongs here", after which conflicts become meaningful.

Also fixed in this release, among others: the filter box (Backspace used to prompt
to delete a board), a stale netlist silently updating a board, footprints never
being linked to their schematic symbols, nets never being cleared, KiCad 9 being
preferred over KiCad 10 when both were installed, `kicad-cli` being undiscoverable
on macOS, and a board-deletion path that could target the project's parent
directory.

---

## Contributing

Pull requests welcome. Please keep `core/` free of `pcbnew` and `wx` — there is a
test for it — add a test for anything behavioural, and run `ruff check` and
`ruff format` before submitting.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

Eliot Abramo — original idea, development, and maintenance.
