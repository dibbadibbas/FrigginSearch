#!/bin/bash
# Install FrigginShell as the login shell for an unprivileged kiosk account.
#
# Run as root on the server:   sudo ./install/install-frigginshell.sh
#
# Creates the account, installs the application under /opt/markscraper, and
# locks the SSH session down so a visitor gets the menu and nothing else.
set -euo pipefail

ACCOUNT="${ACCOUNT:-friggin}"
HOME_DIR="${HOME_DIR:-/opt/markscraper}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$HOME_DIR/bin/frigginshell"

[[ $EUID -eq 0 ]] || { echo "run this with sudo" >&2; exit 1; }

echo "==> installing application to $HOME_DIR"
mkdir -p "$HOME_DIR"
cp -r "$SRC/markscraper" "$SRC/bin" "$HOME_DIR/"
chmod +x "$LAUNCHER"

echo "==> creating the virtual environment"
python3 -m venv "$HOME_DIR/.venv" 2>/dev/null || true
if [[ ! -x "$HOME_DIR/.venv/bin/pip" ]]; then
  # Some distributions ship venv without ensurepip.
  curl -sS https://bootstrap.pypa.io/get-pip.py | "$HOME_DIR/.venv/bin/python"
fi
# The shell itself needs no third-party packages; only the scraper does.
"$HOME_DIR/.venv/bin/pip" -q install requests beautifulsoup4 lxml || true

echo "==> installing the archive"
mkdir -p "$HOME_DIR/data"
if [[ -f "$SRC/data/marksfriggin.db" ]]; then
  "$SRC/.venv/bin/python" -m markscraper.cli export \
      --to "$HOME_DIR/data/marksfriggin.db" 2>/dev/null \
    || cp "$SRC/data/marksfriggin.db" "$HOME_DIR/data/marksfriggin.db"
fi
chmod 444 "$HOME_DIR/data/marksfriggin.db" 2>/dev/null || true

echo "==> creating the $ACCOUNT account"
if ! id -u "$ACCOUNT" >/dev/null 2>&1; then
  useradd --system --home-dir "$HOME_DIR" --shell "$LAUNCHER" "$ACCOUNT"
else
  usermod --home "$HOME_DIR" --shell "$LAUNCHER" "$ACCOUNT"
fi
grep -qxF "$LAUNCHER" /etc/shells || echo "$LAUNCHER" >> /etc/shells

# The account owns nothing it can write to: the archive is read-only and the
# application lives outside its reach.
chown -R root:root "$HOME_DIR"
chmod -R a+rX "$HOME_DIR"

echo "==> writing the sshd rule"
cat > /etc/ssh/sshd_config.d/60-frigginshell.conf <<CONF
# FrigginShell kiosk account. ForceCommand also covers "ssh $ACCOUNT@host <cmd>",
# which a login shell alone would not.
Match User $ACCOUNT
    ForceCommand $LAUNCHER
    PermitTTY yes
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding no
    PermitTunnel no
    PermitOpen none
    AllowStreamLocalForwarding no
    PermitUserRC no
CONF

sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo
echo "Done. Give the account a way in, either:"
echo "  sudo mkdir -p $HOME_DIR/.ssh && sudo nano $HOME_DIR/.ssh/authorized_keys"
echo "  sudo chown -R $ACCOUNT $HOME_DIR/.ssh && sudo chmod 700 $HOME_DIR/.ssh"
echo "or, for an open guest login:"
echo "  sudo passwd $ACCOUNT"
echo
echo "Then:  ssh $ACCOUNT@<host>"
