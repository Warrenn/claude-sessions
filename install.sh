#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/claude_sessions.py"
INSTALL_DIR="${HOME}/.local/bin"
TARGET="$INSTALL_DIR/claude-sessions"

# Check Python 3.8+
check_python() {
    local py=""
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            py="$candidate"
            break
        fi
    done

    if [[ -z "$py" ]]; then
        echo "Error: Python 3 not found. Install Python 3.8+ first." >&2
        exit 1
    fi

    local version
    version=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    local major minor
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)

    if [[ "$major" -lt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -lt 8 ]]; }; then
        echo "Error: Python 3.8+ required (found $version)." >&2
        exit 1
    fi

    echo "Found Python $version ($py)"
}

install() {
    check_python

    # Create install directory
    mkdir -p "$INSTALL_DIR"

    # Copy script
    cp "$SOURCE" "$TARGET"
    chmod +x "$TARGET"

    echo "Installed claude-sessions to $TARGET"

    # Check if ~/.local/bin is in PATH
    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        echo ""
        echo "Warning: $INSTALL_DIR is not in your PATH."
        echo "Add this to your shell profile (~/.zshrc or ~/.bashrc):"
        echo ""
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo ""
    else
        echo "Run 'claude-sessions --help' to get started."
    fi
}

uninstall() {
    if [[ -f "$TARGET" ]]; then
        rm "$TARGET"
        echo "Uninstalled claude-sessions from $TARGET"
    else
        echo "claude-sessions not found at $TARGET"
    fi
}

case "${1:-install}" in
    install)   install ;;
    uninstall) uninstall ;;
    *)
        echo "Usage: $0 [install|uninstall]"
        exit 1
        ;;
esac
