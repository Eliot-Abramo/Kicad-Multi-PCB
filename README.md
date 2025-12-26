# Multi-Board PCB Manager - KiCad Plugin

A hierarchical multi-board PCB management plugin for KiCad, similar to schematic hierarchical sheets. Manage multiple independent PCBs within a single project with semantic inter-board connections.

## Concept

```
┌─────────────────────────────────────────────────────────────┐
│                    ROOT PROJECT                              │
│                                                              │
│  ┌─────────────────┐     ┌─────────────────┐                │
│  │   MainBoard     │     │   PowerSupply   │                │
│  │   (6-layer)     │     │   (4-layer)     │                │
│  │                 │     │                 │                │
│  │  [OUT] UART_TX ─┼─────┼─► [IN] UART_RX  │                │
│  │  [IN] 5V_IN ◄───┼─────┼── [OUT] 5V_OUT  │                │
│  │                 │     │                 │                │
│  └─────────────────┘     └─────────────────┘                │
│           │                                                  │
│           │              ┌─────────────────┐                │
│           │              │   Interface     │                │
│           │              │   (4-layer)     │                │
│           └──────────────┼─► [IN] DATA     │                │
│                          │                 │                │
│                          └─────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

## Features

### Hierarchical Board Management
- **Sub-PCBs**: Each board is independent with its own:
  - Layer count (2, 4, 6, 8+ layers)
  - Stackup configuration
  - Design rules
  - PCB file (`.kicad_pcb`)

### Inter-Board Ports (Like Hierarchical Labels)
- **INPUT ports**: Signals coming into the board
- **OUTPUT ports**: Signals leaving the board
- **BIDIRECTIONAL ports**: Two-way signals
- Associate ports with specific connectors and pins

### Semantic Connections
- Connect ports between boards (like hierarchical pins)
- No physical traces - represents logical signal flow
- Used for connectivity validation, not routing

### Connectivity Check (Multi-Board ERC)
- Detect unconnected output ports (ERROR)
- Detect unconnected input ports (WARNING)
- Detect direction mismatches (output→output, input→input)
- Generate connectivity reports

## Installation

### Linux
```bash
cp -r multi_pcb_manager_v2 ~/.local/share/kicad/8.0/scripting/plugins/
```

### Windows
```
Copy to: %APPDATA%\kicad\8.0\scripting\plugins\
```

### macOS
```bash
cp -r multi_pcb_manager_v2 ~/Library/Preferences/kicad/8.0/scripting/plugins/
```

Then restart KiCad or use **Tools → External Plugins → Refresh Plugins**.

## Usage

### 1. Open the Plugin
- Open any PCB file in your project
- Go to **Tools → External Plugins → Multi-Board Manager**

### 2. Add Sub-PCBs
Click **Add Board** to create each sub-PCB:
- **MainBoard**: 6-layer, high-speed design rules
- **PowerSupply**: 4-layer, standard rules
- **Interface**: 4-layer, fine-pitch rules

Each board gets its own `.kicad_pcb` file.

### 3. Define Ports on Each Board
Select a board and click **Edit Ports**:

| Port Name | Direction | Connector | Net |
|-----------|-----------|-----------|-----|
| UART_TX | output | J1 | UART1_TX |
| UART_RX | input | J1 | UART1_RX |
| 5V_IN | input | J2 | VCC_5V |
| GND | bidirectional | J2 | GND |

Use **Auto-detect from Connectors** to automatically create ports from J*, P*, CN* footprints.

### 4. Create Inter-Board Connections
Click **Add Connection** to link ports:

```
PowerSupply.5V_OUT → MainBoard.5V_IN
MainBoard.UART_TX → Interface.UART_RX
```

### 5. Run Connectivity Check
Click **Run Connectivity Check** to validate:
- All outputs are connected
- Direction matching (output→input)
- No orphaned ports

### 6. Export Report
Generate a text report documenting:
- All boards and their configurations
- All ports and their assignments
- All inter-board connections
- Connectivity check results

## Workflow with Your Multi-Board Layout

Based on your screenshot showing 3 PCBs in one view:

1. **Create the project structure**:
   ```
   project/
   ├── .kicad_multiboard.json    # Plugin config
   ├── top_board.kicad_pcb       # Your top PCB
   ├── middle_board.kicad_pcb    # Your middle PCB
   └── bottom_board.kicad_pcb    # Your bottom PCB
   ```

2. **Register each board** in the plugin with its actual PCB file

3. **Define ports** at connector locations (your HDRM_1, HDRM_2, ECG labels etc.)

4. **Connect** the ports to show signal flow between boards

5. **Run ERC** - the plugin will verify all inter-board connections are valid

## Configuration File

The plugin stores configuration in `.kicad_multiboard.json`:

```json
{
  "name": "Multi-Board Project",
  "version": "2.0",
  "boards": {
    "MainBoard": {
      "name": "MainBoard",
      "layers": 6,
      "pcb_filename": "main.kicad_pcb",
      "stackup_preset": "6-Layer Standard",
      "ports": [
        {
          "name": "UART_TX",
          "direction": "output",
          "net_name": "UART1_TX",
          "connector_ref": "J1",
          "pin_number": "3"
        }
      ]
    }
  },
  "connections": [
    {
      "id": "a1b2c3d4",
      "source_board": "MainBoard",
      "source_port": "UART_TX",
      "target_board": "Interface",
      "target_port": "UART_RX",
      "signal_name": "Debug UART"
    }
  ]
}
```

## Tips

1. **Version control**: Add `.kicad_multiboard.json` to git
2. **Naming**: Use clear port names that match your schematic net names
3. **Connectors**: Document which connector/pin each port represents
4. **Bidirectional**: Use for buses, I2C, SPI where direction varies
5. **Incremental**: Add ports as you design, don't try to define everything upfront

## Comparison with Schematic Hierarchical Sheets

| Schematic | Multi-Board Plugin |
|-----------|-------------------|
| Root sheet | Root project |
| Sub-sheet | Sub-PCB |
| Hierarchical label | Port |
| Hierarchical pin | Connection |
| ERC | Connectivity Check |

## Troubleshooting

### PCB creation fails
The plugin tries multiple methods to create PCB files. If all fail:
1. Create the PCB manually in KiCad
2. Check "Use existing PCB file" when adding the board

### Ports not showing nets
Open the actual PCB file first, then edit ports - the plugin reads nets from the loaded PCB.

### Plugin not appearing
- Verify KiCad version (8.0+)
- Check plugins directory path
- Refresh plugins in KiCad

## Requirements

- KiCad 8.0 or later
- Python 3.x (bundled with KiCad)
- wxPython (bundled with KiCad)

## License

MIT License
