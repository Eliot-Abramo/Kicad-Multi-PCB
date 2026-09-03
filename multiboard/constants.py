# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Multi-Board PCB Manager - Constants
===================================

Names and tunables shared across the package. Version numbers live in
``version.py``; nothing version-related belongs here.
"""

# =============================================================================
# Directory and file names
# =============================================================================

BOARDS_DIR = "boards"
"""Subdirectory under the project root where sub-board projects are created."""

TRASH_DIR = ".trash"
"""
Subdirectory of ``boards/`` where deleted boards are moved.

Deletion never calls ``shutil.rmtree``. A rename into here is reversible, is
atomic on the same filesystem, and cannot escape the project tree.
"""

WORK_DIR = ".multiboard"
"""
Per-project scratch directory: index cache, netlist exports, DRC reports,
pending-focus handoff. Gitignored. Never the project root, which is where v12
wrote its temp netlist.
"""

CONFIG_FILE = ".kicad_multiboard.json"
"""Plugin configuration file, at the project root."""

INDEX_CACHE_FILE = "index-cache.json"
"""Component index cache, inside ``WORK_DIR``."""

PENDING_FOCUS_FILE = "pending-focus.json"
"""Cross-board "reveal this component" handoff, inside ``WORK_DIR``."""

DEBUG_LOG_NAME = "multiboard.log"
"""Debug log, inside ``WORK_DIR``."""

# =============================================================================
# Generated footprint libraries
# =============================================================================

BLOCK_LIB_NAME = "MultiBoard_Blocks"
"""Project-local .pretty holding one block footprint per sub-board."""

PORT_LIB_NAME = "MultiBoard_Ports"
"""Project-local .pretty holding inter-board port markers."""

MANAGED_REF_PREFIX = "MB_"
"""Reference prefix for footprints this plugin owns; excluded from ownership."""

# =============================================================================
# Defaults
# =============================================================================

DEFAULT_BLOCK_WIDTH = 50.0
DEFAULT_BLOCK_HEIGHT = 35.0
DEFAULT_PORT_POSITION = 0.5

DEFAULT_BOARD_FIELD = "MB_Board"
"""
Schematic symbol field the plugin READS to learn a component's intended board.

The plugin never writes it. A user who prefers to keep assignment in the
schematic can set this field in Eeschema and the index will honour it, ranked
below an explicit pin and above a rule.
"""

# =============================================================================
# Tuning
# =============================================================================

PACK_GRID_SPACING = 10.0
"""Grid pitch, in mm, when dropping newly added footprints onto a board."""

PACK_MAX_PER_ROW = 20
"""Footprints per row when packing."""

PACK_ORIGIN = (50.0, 50.0)
"""Where the packing grid starts, in mm."""

CLI_TIMEOUT = 180.0
"""Default seconds before a kicad-cli invocation is killed. v12 had no timeout,
so a hung kicad-cli froze KiCad permanently."""

CLI_POLL_INTERVAL = 0.05
"""
Seconds between checks on a running kicad-cli.

Twenty times a second: fast enough that Cancel feels immediate and a progress
bar animates smoothly, slow enough that polling costs nothing measurable.
"""

DISCOVERY_CACHE_TTL = 60.0
"""Seconds to remember a failed KiCad installation lookup."""

SEARCH_RESULT_LIMIT = 200
"""Rows returned by the command palette; the xref view is unbounded."""

FOCUS_REQUEST_MAX_AGE = 300.0
"""Seconds a pending cross-board focus request stays valid."""
