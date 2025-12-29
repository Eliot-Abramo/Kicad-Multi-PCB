"""
Multi-Board PCB Manager - Constants and Configuration
Centralized configuration values for consistent behavior.
Author: Eliot Abramo
"""

# =============================================================================
# Directory and File Names
# =============================================================================
BOARDS_DIR = "boards"
CONFIG_FILE = ".kicad_multiboard.json"

# =============================================================================
# Library Names
# =============================================================================
BLOCK_LIB_NAME = "MultiBoard_Blocks"
PORT_LIB_NAME = "MultiBoard_Ports"

# =============================================================================
# Configuration
# =============================================================================
CONFIG_VERSION = "10.0"

# =============================================================================
# Default Values
# =============================================================================
DEFAULT_BLOCK_WIDTH = 50.0
DEFAULT_BLOCK_HEIGHT = 35.0
DEFAULT_PORT_POSITION = 0.5

# =============================================================================
# File Patterns
# =============================================================================

TEMP_NETLIST_NAME = ".multiboard_netlist.xml"
DEBUG_LOG_NAME = "multiboard_debug.log"

# =============================================================================
# Performance Tuning
# =============================================================================

# Maximum footprints to load in parallel
MAX_PARALLEL_FOOTPRINTS = 8

# Grid spacing for component packing (mm)
PACK_GRID_SPACING = 10.0

# Maximum components per row when packing
PACK_MAX_PER_ROW = 10
