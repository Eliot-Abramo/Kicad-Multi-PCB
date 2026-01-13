#!/usr/bin/env bash
# ==============================================================================
# Multi-Board PCB Manager - Installation Script
# ==============================================================================
# Automatically detects OS and KiCad version, then installs to the correct
# plugins directory.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# Author: Eliot Abramo
# License: MIT
# ==============================================================================

set -euo pipefail

# Configuration
PLUGIN_NAME="multi_pcb_manager_v2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_KICAD_VERSION="9.0"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==============================================================================
# Helper Functions
# ==============================================================================

print_header() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  Multi-Board PCB Manager - Installation${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

# ==============================================================================
# Detection Functions
# ==============================================================================

detect_os() {
    case "$(uname -s)" in
        Linux*)     echo "linux" ;;
        Darwin*)    echo "macos" ;;
        MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
        *)          echo "unknown" ;;
    esac
}

detect_kicad_version() {
    local os="$1"
    local versions=("9.0" "8.0" "7.0")
    
    for ver in "${versions[@]}"; do
        case "$os" in
            linux)
                if [[ -d "$HOME/.local/share/kicad/$ver" ]]; then
                    echo "$ver"
                    return
                fi
                ;;
            macos)
                if [[ -d "$HOME/Library/Application Support/kicad/$ver" ]]; then
                    echo "$ver"
                    return
                fi
                ;;
            windows)
                if [[ -d "$APPDATA/kicad/$ver" ]]; then
                    echo "$ver"
                    return
                fi
                ;;
        esac
    done
    
    # Default to 9.0 if nothing found (will be created)
    echo "9.0"
}

get_plugin_dir() {
    local os="$1"
    local version="$2"
    
    case "$os" in
        linux)
            echo "$HOME/.local/share/kicad/$version/scripting/plugins"
            ;;
        macos)
            echo "$HOME/Library/Application Support/kicad/$version/scripting/plugins"
            ;;
        windows)
            echo "$APPDATA/kicad/$version/scripting/plugins"
            ;;
        *)
            echo ""
            ;;
    esac
}

check_kicad_cli() {
    if command -v kicad-cli &> /dev/null; then
        print_success "kicad-cli found: $(command -v kicad-cli)"
        return 0
    else
        print_warning "kicad-cli not found in PATH"
        print_info "The plugin requires kicad-cli for netlist export"
        print_info "Make sure KiCad is fully installed"
        return 1
    fi
}

# ==============================================================================
# Installation
# ==============================================================================

install_plugin() {
    local plugin_dir="$1"
    local target_dir="$plugin_dir/$PLUGIN_NAME"
    
    # Create directory if needed
    if [[ ! -d "$plugin_dir" ]]; then
        print_info "Creating plugin directory..."
        mkdir -p "$plugin_dir"
    fi
    
    # Remove old installation
    if [[ -d "$target_dir" ]]; then
        print_info "Removing previous installation..."
        rm -rf "$target_dir"
    fi
    
    # Copy plugin files
    print_info "Installing plugin files..."
    cp -r "$SCRIPT_DIR" "$target_dir"
    
    # Remove install scripts from installed copy (not needed there)
    rm -f "$target_dir/install.sh" "$target_dir/install.bat"
    
    print_success "Plugin installed to: $target_dir"
}

# ==============================================================================
# Main
# ==============================================================================

main() {
    print_header
    
    # Detect environment
    local os=$(detect_os)
    if [[ "$os" == "unknown" ]]; then
        print_error "Unknown operating system. Please install manually."
        exit 1
    fi
    print_success "Detected OS: $os"
    
    local version=$(detect_kicad_version "$os")
    print_success "Target KiCad version: $version"
    
    if [[ "${version%.*}" -lt "${MIN_KICAD_VERSION%.*}" ]]; then
        print_warning "This plugin is designed for KiCad $MIN_KICAD_VERSION+"
        print_warning "Found KiCad $version - some features may not work"
    fi
    
    local plugin_dir=$(get_plugin_dir "$os" "$version")
    if [[ -z "$plugin_dir" ]]; then
        print_error "Could not determine plugin directory"
        exit 1
    fi
    print_info "Plugin directory: $plugin_dir"
    
    echo ""
    
    # Check dependencies
    check_kicad_cli || true
    
    echo ""
    
    # Install
    install_plugin "$plugin_dir"
    
    # Success message
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  ✓ Installation Complete!${NC}"
    echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Open KiCad PCB Editor"
    echo "  2. Go to: Tools → External Plugins → Refresh Plugins"
    echo "  3. Access: Tools → External Plugins → Multi-Board Manager"
    echo ""
    echo "Documentation: https://github.com/yourusername/multi-pcb-manager"
    echo ""
}

main "$@"
