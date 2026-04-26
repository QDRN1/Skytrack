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
#   • Writes /usr/share/skytrack/splash-fallback.html — a self-contained
#     branded HTML file that xinitrc will load if the primary splash at
#     /opt/skytrack/static/splash/index.html is ever missing (bad OTA,
#     partial install, disk corruption). Guarantees no black screen.
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
FALLBACK_DIR="/usr/share/skytrack"
FALLBACK_SPLASH="${FALLBACK_DIR}/splash-fallback.html"

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
#
# `systemctl mask` is idempotent, so we call it unconditionally. We used to
# guard with `systemctl is-enabled | grep -qv '^masked$'`, but under
# `set -euo pipefail` a non-zero exit from `systemctl is-enabled` (returned
# for disabled/static/masked units) propagates through the pipe and makes
# the `if` read as false — so we silently skipped every unit and left
# getty@tty2..6 unmasked. Drop the guard.
# ---------------------------------------------------------------------------
mask_extra_gettys() {
  for n in 2 3 4 5 6; do
    local unit="getty@tty${n}.service"
    log "masking $unit"
    systemctl mask "$unit" >/dev/null 2>&1 || true
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
# 6. Emergency fallback splash — a self-contained HTML file that xinitrc
#    loads if the primary /opt/skytrack/static/splash/index.html ever
#    disappears. Lives in /usr/share/skytrack/ (owned by root, not touched
#    by OTA) so it's always available. Polls /healthz, never shows a
#    Chromium error page, always paints a branded dark gradient.
# ---------------------------------------------------------------------------
write_fallback_splash() {
  mkdir -p "$FALLBACK_DIR"

  cat >"$FALLBACK_SPLASH" <<'EOF'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>SkyTrack</title>
<style>
  html, body {
    margin: 0; padding: 0; height: 100%; width: 100%;
    background:
      radial-gradient(ellipse at 30% 20%, rgba(46,108,246,0.28), transparent 60%),
      radial-gradient(ellipse at 78% 80%, rgba(242,145,53,0.18), transparent 60%),
      linear-gradient(180deg, #05070b 0%, #02040e 100%);
    color: #e6ecf2;
    font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    overflow: hidden;
    cursor: none;
    -webkit-user-select: none; user-select: none;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  .pill-wrap {
    position: fixed; top: 6vh; left: 0; right: 0; text-align: center;
    z-index: 10; pointer-events: none;
  }
  .pill {
    display: inline-block;
    background: rgba(0,0,0,0.55);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 999px;
    padding: 12px 28px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 1.4em;
    letter-spacing: 1.5px;
    color: #fff;
    text-shadow: 0 1px 12px rgba(0,0,0,0.85);
  }
  .card {
    position: fixed; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    text-align: center; max-width: 720px; padding: 0 32px;
    z-index: 5;
  }
  .brand {
    font-size: 54px; font-weight: 300; letter-spacing: 0.22em;
    margin: 0 0 12px 0; text-transform: uppercase;
  }
  .brand strong { font-weight: 600; color: #7ec8ff; }
  .tag {
    font-size: 16px; letter-spacing: 0.16em;
    color: #8aa0b8; text-transform: uppercase; margin: 0 0 32px 0;
  }
  .spinner {
    display: inline-block; width: 52px; height: 52px;
    border: 4px solid rgba(126,200,255,0.18);
    border-top-color: #7ec8ff;
    border-radius: 50%;
    animation: spin 1.1s linear infinite;
    margin: 8px 0 18px 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .msg { font-size: 19px; color: #c2cedb; margin: 0; }
  .msg.small {
    font-size: 13px; color: #6f8397; margin-top: 10px; letter-spacing: 0.04em;
  }
</style>
</head>
<body>
  <div class="pill-wrap"><span class="pill">SkyTrack</span></div>
  <div class="card">
    <h1 class="brand">Sky<strong>Track</strong></h1>
    <p class="tag">ADS-B Tracker · QDRN</p>
    <div class="spinner" aria-hidden="true"></div>
    <p class="msg">Starting services&hellip;</p>
    <p class="msg small">Using system fallback splash</p>
  </div>
  <script>
    (function () {
      function poll() {
        fetch("http://127.0.0.1:8080/healthz", { cache: "no-store" })
          .then(function (r) {
            if (r && r.ok) {
              window.location.replace("http://127.0.0.1:8080/kiosk");
            } else {
              setTimeout(poll, 1000);
            }
          })
          .catch(function () { setTimeout(poll, 1000); });
      }
      ["contextmenu","dragstart","selectstart","gesturestart"].forEach(function (ev) {
        document.addEventListener(ev, function (e) { e.preventDefault(); }, { passive: false });
      });
      poll();
    })();
  </script>
</body>
</html>
EOF

  chmod 0644 "$FALLBACK_SPLASH"
  log "fallback splash → $FALLBACK_SPLASH"
}

# ---------------------------------------------------------------------------
# 7. Seed /etc/skytrack/display.env so xinitrc can apply the default
#    rotation on first boot. SkyTrack ships in portrait mode (rotate right
#    = 90°) unless the operator changes it in Settings → Display.
# ---------------------------------------------------------------------------
seed_display_env() {
  local etc_dir="/etc/skytrack"
  local etc_env="${etc_dir}/display.env"
  mkdir -p "$etc_dir"
  if [[ ! -f "$etc_env" ]]; then
    log "seeding $etc_env (default rotation: 90 / right)"
    cat >"$etc_env" <<'EOF'
# Default SkyTrack display settings — install-time seed.
# xinitrc prefers /var/lib/skytrack/display.env if present (written by
# Settings → Display). This file is the fallback on a fresh install.
SKYTRACK_DISPLAY_ROTATION=90
SKYTRACK_DISPLAY_OUTPUT=HDMI-1
EOF
    chmod 0644 "$etc_env"
  else
    log "$etc_env already present — leaving operator-set value"
  fi
}

# ---------------------------------------------------------------------------
# 8. Announce safe-mode availability (we never create the marker ourselves).
# ---------------------------------------------------------------------------
announce_safe_mode() {
  local dir="/boot/firmware"
  [[ -d "$dir" ]] || dir="/boot"
  log "safe-mode escape hatch: touch ${dir}/skytrack-safe-mode to skip kiosk takeover"
}

# ---------------------------------------------------------------------------
# 9. HDMI stabilization — prevent screen flickering, timeout, and blanking.
#    Ensures a forced HDMI hotplug, boost signal, and disables DPMS/blanking
#    at the firmware level so the kiosk display never goes dark.
# ---------------------------------------------------------------------------
stabilize_hdmi() {
  local cfg="/boot/firmware/config.txt"
  [[ -f "$cfg" ]] || cfg="/boot/config.txt"
  if [[ ! -f "$cfg" ]]; then
    log "config.txt not found — skipping HDMI stabilization (not on a Pi?)"
    return 0
  fi

  local need_write=0
  local content
  content="$(cat "$cfg")"

  # Parameters to ensure are present. Each is "key=value".
  local -a params=(
    "hdmi_force_hotplug=1"
    "config_hdmi_boost=4"
    "disable_overscan=1"
    "hdmi_blanking=0"
  )

  # Ensure vc4-kms-v3d or vc4-fkms-v3d overlay is present (required for
  # modern Pi display stack). On Pi 4/5 Bookworm+ this is usually there
  # already; we only add it if completely absent.
  if ! grep -qE 'dtoverlay=vc4-(f?kms)-v3d' "$cfg"; then
    params+=("dtoverlay=vc4-fkms-v3d")
  fi

  for entry in "${params[@]}"; do
    local key="${entry%%=*}"
    local val="${entry#*=}"
    # If the key exists (possibly commented or with a different value), replace it.
    if grep -qE "^#?\s*${key}\b" "$cfg"; then
      # Only rewrite if value differs or is commented out
      if ! grep -q "^${key}=${val}$" "$cfg"; then
        content="$(echo "$content" | sed -E "s|^#?\s*${key}\b.*|${key}=${val}|")"
        need_write=1
      fi
    else
      # Key not present at all — append under [all] or at end
      content="${content}
${key}=${val}"
      need_write=1
    fi
  done

  if [[ "$need_write" -eq 0 ]]; then
    log "HDMI config already stabilized"
    return 0
  fi

  log "stabilizing HDMI in $cfg (backup: ${cfg}.bak)"
  cp -f "$cfg" "${cfg}.bak"
  printf '%s\n' "$content" > "$cfg"
}

# ---------------------------------------------------------------------------
install_plymouth_theme
lock_down_cmdline
mask_extra_gettys
relax_xwrapper
write_openbox_config
write_fallback_splash
seed_display_env
stabilize_hdmi
announce_safe_mode

log "appliance configuration complete"
