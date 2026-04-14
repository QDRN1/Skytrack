#!/usr/bin/env bash
# =============================================================================
# SkyTrack — appliance provisioning helper
#
# Locks down the Raspberry Pi as a single-purpose SkyTrack kiosk:
#
#   • Installs the Plymouth "skytrack" boot splash theme
#   • Rewrites /boot/firmware/cmdline.txt for quiet, logo-less boot and moves
#     the console away from tty1 so Linux output never flashes on the screen
#   • Masks getty@tty2..6 so no hidden console is reachable
#   • Relaxes /etc/X11/Xwrapper.config so our non-root kiosk user can start X
#   • Writes the openbox config for skytrack-kiosk (empty menu, no keybinds)
#
# Idempotent — safe to re-run. Skips work already done. Expects the
# skytrack-kiosk user to already exist (created by install.sh).
# =============================================================================

set -euo pipefail

REPO_DIR="${SKYTRACK_REPO_DIR:-/opt/skytrack}"
KIOSK_USER="skytrack-kiosk"
KIOSK_HOME="/home/${KIOSK_USER}"
BOOT_CMDLINE="/boot/firmware/cmdline.txt"
LEGACY_CMDLINE="/boot/cmdline.txt"

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "configure_appliance: must run as root" >&2
    exit 1
  fi
}

log() { echo "  [appliance] $*"; }

need_root

# ---------------------------------------------------------------------------
# 1. Plymouth theme — simple script theme that shows a branded still.
# ---------------------------------------------------------------------------
install_plymouth_theme() {
  local theme_dir="/usr/share/plymouth/themes/skytrack"

  if [[ -f "$theme_dir/skytrack.plymouth" ]] &&
     plymouth-set-default-theme 2>/dev/null | grep -q '^skytrack$'; then
    log "plymouth theme already active"
    return 0
  fi

  log "installing plymouth theme → $theme_dir"
  mkdir -p "$theme_dir"

  # Reuse the repo logo if present; otherwise a tiny placeholder.
  local src_logo="$REPO_DIR/static/img/skytrack-logo.png"
  if [[ -f "$src_logo" ]]; then
    cp "$src_logo" "$theme_dir/logo.png"
  else
    # 1x1 transparent PNG fallback — plymouth still needs *some* image.
    printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82' \
      > "$theme_dir/logo.png"
  fi

  cat >"$theme_dir/skytrack.plymouth" <<'EOF'
[Plymouth Theme]
Name=SkyTrack
Description=SkyTrack appliance boot splash
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/skytrack
ScriptFile=/usr/share/plymouth/themes/skytrack/skytrack.script
EOF

  cat >"$theme_dir/skytrack.script" <<'EOF'
# SkyTrack Plymouth splash — solid dark backdrop + centered logo with a
# gentle pulse. No progress text (we cover that inside Chromium).

Window.SetBackgroundTopColor(0.02, 0.03, 0.04);
Window.SetBackgroundBottomColor(0.02, 0.03, 0.04);

logo.image  = Image("logo.png");
logo.sprite = Sprite(logo.image);
logo.sprite.SetX(Window.GetWidth()  / 2 - logo.image.GetWidth()  / 2);
logo.sprite.SetY(Window.GetHeight() / 2 - logo.image.GetHeight() / 2);

progress = 0;
fun refresh_callback () {
  progress += 0.02;
  opacity   = 0.75 + 0.25 * Math.Sin(progress);
  logo.sprite.SetOpacity(opacity);
}
Plymouth.SetRefreshFunction(refresh_callback);
EOF

  # Select the theme and rebuild initramfs so it shows at boot.
  if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    plymouth-set-default-theme -R skytrack || \
      plymouth-set-default-theme skytrack || true
  fi
}

# ---------------------------------------------------------------------------
# 2. Kernel cmdline — quiet, no logo, hide cursor, move console off tty1.
#    We edit in place, once, with a backup. Never duplicate parameters.
# ---------------------------------------------------------------------------
lock_down_cmdline() {
  local target="$BOOT_CMDLINE"
  [[ -f "$target" ]] || target="$LEGACY_CMDLINE"
  if [[ ! -f "$target" ]]; then
    log "cmdline file not found — skipping (not on a Pi?)"
    return 0
  fi

  local line
  line="$(tr -d '\n' <"$target")"

  local required=(
    "quiet"
    "splash"
    "loglevel=0"
    "vt.global_cursor_default=0"
    "logo.nologo"
    "consoleblank=0"
    "plymouth.ignore-serial-consoles"
  )

  # Replace any existing console=tty* with console=tty3 so Linux output
  # doesn't land on tty1 (which our kiosk service owns).
  if [[ "$line" =~ console=tty[0-9]+ ]]; then
    line="$(echo "$line" | sed -E 's/console=tty[0-9]+/console=tty3/')"
  else
    line="$line console=tty3"
  fi

  local need_write=0
  for token in "${required[@]}"; do
    if [[ " $line " != *" $token "* ]]; then
      line="$line $token"
      need_write=1
    fi
  done

  # Collapse runs of whitespace
  line="$(echo "$line" | tr -s ' ')"
  line="${line# }"
  line="${line% }"

  if [[ "$need_write" -eq 0 ]] && diff -q <(printf '%s\n' "$line") "$target" >/dev/null 2>&1; then
    log "cmdline already locked down"
    return 0
  fi

  log "rewriting $target (backup: ${target}.bak)"
  cp -f "$target" "${target}.bak"
  printf '%s\n' "$line" >"$target"
}

# ---------------------------------------------------------------------------
# 3. Mask getty@tty2..6 — no hidden console logins.
# ---------------------------------------------------------------------------
mask_extra_gettys() {
  for n in 2 3 4 5 6; do
    local unit="getty@tty${n}.service"
    if systemctl is-enabled "$unit" 2>/dev/null | grep -qv '^masked$'; then
      log "masking $unit"
      systemctl mask "$unit" >/dev/null 2>&1 || true
    fi
  done
}

# ---------------------------------------------------------------------------
# 4. Xwrapper.config — allow non-console users to start X (required for our
#    systemd-launched xinit session).
# ---------------------------------------------------------------------------
relax_xwrapper() {
  local cfg="/etc/X11/Xwrapper.config"
  mkdir -p /etc/X11
  if [[ -f "$cfg" ]] && grep -q '^allowed_users=anybody' "$cfg"; then
    log "Xwrapper.config already permissive"
    return 0
  fi
  log "writing $cfg"
  cat >"$cfg" <<'EOF'
# Written by SkyTrack installer — required so the skytrack-kiosk user can
# launch X from a systemd service (no console required).
allowed_users=anybody
needs_root_rights=yes
EOF
}

# ---------------------------------------------------------------------------
# 5. openbox config for skytrack-kiosk — empty menu, no keybinds, no
#    decorations. Prevents right-click or keyboard-driven escape to a
#    desktop-like state if something goes wrong.
# ---------------------------------------------------------------------------
write_openbox_config() {
  local obdir="${KIOSK_HOME}/.config/openbox"

  if ! id "$KIOSK_USER" >/dev/null 2>&1; then
    log "ERROR: $KIOSK_USER user missing — run install.sh first"
    return 1
  fi

  mkdir -p "$obdir"

  cat >"${obdir}/rc.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <resistance>
    <strength>0</strength>
    <screen_edge_strength>0</screen_edge_strength>
  </resistance>
  <focus>
    <focusNew>yes</focusNew>
    <focusLast>yes</focusLast>
    <followMouse>no</followMouse>
  </focus>
  <placement>
    <policy>UnderMouse</policy>
    <center>yes</center>
  </placement>
  <theme>
    <name>Clearlooks</name>
    <titleLayout></titleLayout>
    <keepBorder>no</keepBorder>
    <animateIconify>no</animateIconify>
  </theme>
  <desktops>
    <number>1</number>
    <names><name>kiosk</name></names>
  </desktops>
  <!-- No keybinds and no mouse bindings — appliance lockdown. -->
  <keyboard/>
  <mouse/>
  <applications>
    <application class="*">
      <fullscreen>yes</fullscreen>
      <maximized>yes</maximized>
      <decor>no</decor>
    </application>
  </applications>
</openbox_config>
EOF

  cat >"${obdir}/autostart" <<'EOF'
# openbox autostart — keeps the cursor hidden and screen awake. The
# real kiosk (Chromium) is launched from xinitrc, not here.
xset -dpms      || true
xset s off      || true
xset s noblank  || true
unclutter -idle 0 -root >/dev/null 2>&1 &
EOF

  cat >"${obdir}/menu.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="SkyTrack"/>
</openbox_menu>
EOF

  chown -R "${KIOSK_USER}:${KIOSK_USER}" "${KIOSK_HOME}/.config"
}

# ---------------------------------------------------------------------------
# 6. Announce safe-mode availability (we never create the marker ourselves).
# ---------------------------------------------------------------------------
announce_safe_mode() {
  local dir="/boot/firmware"
  [[ -d "$dir" ]] || dir="/boot"
  log "safe-mode escape hatch: touch ${dir}/skytrack-safe-mode to skip kiosk takeover"
}

# ---------------------------------------------------------------------------
install_plymouth_theme
lock_down_cmdline
mask_extra_gettys
relax_xwrapper
write_openbox_config
announce_safe_mode

log "appliance configuration complete"
