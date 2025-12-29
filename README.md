# Multi-Board PCB Manager for KiCad

A professional KiCad plugin for managing multiple PCBs from a single schematic.

## Version 11.0

### Features

- **Unified Schematic**: All boards share the same schematic via hardlinks
- **Component Assignment**: Assign components to specific boards
- **Search & Filter**: Instant filtering of boards by name or description
- **Context Menus**: Right-click for quick actions
- **Health Reports**: One-click health check for all boards
- **Board Comparison**: Diff view to compare component placement
- **PCB Open Detection**: Prevents conflicts when boards are open in KiCad
- **Inter-Board Ports**: Define connection points between boards
- **Block Footprints**: Visual representation for assembly views

### New in v11.0

- **Search Box**: Filter boards instantly by name or description
- **Context Menu**: Right-click on any board for quick actions
- **Health Report**: Comprehensive health check with status indicators
- **Diff View**: Compare components between two boards
- **PCB Open Detection**: Shows which boards are open in KiCad
  - Uses lock files (`.lck`) for cross-instance detection
  - Prevents update/delete of open boards
  - Visual indicator (◉) for open boards
- **Open Boards Indicator**: Shows count of boards open in KiCad

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+N | Create new board |
| Ctrl+H | Health report |
| Ctrl+F | Focus search box |
| F5 | Update selected board (or refresh list) |
| Enter | Open selected board |
| Del | Delete selected board |
| Esc | Close dialog |

### Installation

1. Download `multiboard_manager.zip`
2. Extract to your KiCad plugins directory:
   - **Windows**: `%APPDATA%\kicad\9.0\scripting\plugins\`
   - **Linux**: `~/.local/share/kicad/9.0/scripting/plugins/`
   - **macOS**: `~/Library/Application Support/kicad/9.0/scripting/plugins/`
3. Restart KiCad
4. Access via **Tools → External Plugins → Multi-Board Manager**

### Requirements

- KiCad 9.0+
- kicad-cli (included with KiCad)

### Usage

1. Open any PCB in your project
2. Launch the Multi-Board Manager
3. Create new boards with the **New** button
4. Use **Update** to sync components from the schematic
5. Define **Ports** for inter-board connections
6. Use **Health** to check all boards
7. Use **Compare** to diff two boards

### Status Indicators

| Status | Meaning |
|--------|---------|
| ✓ | Board is healthy |
| → Current | Currently open in this KiCad instance |
| ◉ Open | Open in another KiCad instance |
| ⚠ | Warning (check health report) |
| ✕ | Error (file missing or corrupt) |

### License

MIT License - See LICENSE file
