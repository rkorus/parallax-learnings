#!/bin/bash
# Parallax phone-body bringup — re-establishes the Tailscale leash + adb hands after a restart.
# Usage: bash /home/parallax/phonebridge/bodyup.sh
# The body (Pixel 6a) toolchain lives here durably; this reconnects it to a fresh container.
set -u
BR="/home/parallax/phonebridge"
TS="$BR/tailscale_1.104.2_amd64"
ADB="$BR/platform-tools/adb"
SOCK="$BR/pxtsd.sock"
PHONE_IP="100.x.x.x"   # >>> SET THIS: your phone's Tailscale IP (tailscale status)
FIXED_PORT="5555"      # stable adb port (adb tcpip) — survives hotspot/network drops; set once per boot
CONNECT_PORT="5555"    # BOOTSTRAP port after a REBOOT: Russell reads Settings>Developer options>Wireless debugging port
SOCKS_PORT="1055"

echo "=== Parallax body bringup $(date -u +%H:%M:%SZ) ==="

# 1. tailscaled (userspace mode) if not already running
if ! pgrep -x tailscaled >/dev/null; then
  echo "[bodyup] starting tailscaled (userspace)..."
  nohup "$TS/tailscaled" --tun=userspace-networking --socket="$SOCK" \
    --statedir="$BR/tsstate" --socks5-server=localhost:$SOCKS_PORT \
    --outbound-http-proxy-listen=localhost:1056 > "$BR/tsd.log" 2>&1 &
  sleep 4
  "$TS/tailscale" --socket="$SOCK" up 2>&1 | head -3
else
  echo "[bodyup] tailscaled already running (pid $(pgrep -x tailscaled | head -1))"
  SOCK=$(cat /proc/$(pgrep -x tailscaled|head -1)/cmdline 2>/dev/null | tr '\0' '\n' | sed -n 's/^--socket=//p')
  [ -z "$SOCK" ] && SOCK="$BR/pxtsd.sock"
fi

# 2. confirm phone reachable on the tailnet
echo "[bodyup] pinging phone $PHONE_IP ..."
"$TS/tailscale" --socket="$SOCK" ping --timeout=8s --c=2 "$PHONE_IP" 2>&1 | head -2

# helper: ensure a SOCKS forwarder maps local:$1 -> phone:$1, then adb connect; echo OK/FAIL
ensure_port() {
  local port="$1"
  if ! pgrep -f "socks_forward.py $port " >/dev/null; then
    nohup python3 "$BR/socks_forward.py" "$port" "$PHONE_IP" "$port" "$SOCKS_PORT" > "$BR/fwd_$port.log" 2>&1 &
    sleep 2
  fi
  "$ADB" connect localhost:$port >/dev/null 2>&1
  "$ADB" -s localhost:$port shell true 2>/dev/null && echo OK || echo FAIL
}

# 3+4. Connect on the FIXED port first; if that's dead (post-reboot), bootstrap on the
# wireless-debugging port and re-establish the fixed port with 'adb tcpip'.
echo "[bodyup] trying fixed adb port $FIXED_PORT ..."
if [ "$(ensure_port $FIXED_PORT)" = "OK" ]; then
  TARGET=$FIXED_PORT
elif [ "$CONNECT_PORT" != "$FIXED_PORT" ] && [ "$(ensure_port $CONNECT_PORT)" = "OK" ]; then
  echo "[bodyup] bootstrapped on $CONNECT_PORT -> re-establishing fixed port $FIXED_PORT ..."
  "$ADB" -s localhost:$CONNECT_PORT tcpip $FIXED_PORT 2>&1 | tail -1
  sleep 4
  [ "$(ensure_port $FIXED_PORT)" = "OK" ] && TARGET=$FIXED_PORT || TARGET=$CONNECT_PORT
else
  TARGET=""
fi

if [ -n "$TARGET" ]; then
  MODEL=$("$ADB" -s localhost:$TARGET shell getprop ro.product.model 2>&1 | tr -d '\r')
  echo "[bodyup] ✅ HANDS ONLINE on localhost:$TARGET — device: $MODEL"
  echo "[bodyup] speak:  python3 $BR/speak.py \"text\"   |  ear: nohup python3 $BR/parallax_ear.py &"
else
  echo "[bodyup] ❌ adb connect FAILED on both $FIXED_PORT and $CONNECT_PORT."
  echo "   After a phone REBOOT the fixed port resets. FIX: have Russell open"
  echo "   Settings > System > Developer options > Wireless debugging, read the port at the top,"
  echo "   set CONNECT_PORT in this script to it, and re-run — bodyup will restore the fixed port."
  echo "   (Pairing 'parallax@...' persists, so no re-pairing needed — only the port.)"
fi
