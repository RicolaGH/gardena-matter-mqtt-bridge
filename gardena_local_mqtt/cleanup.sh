#!/bin/sh
# Fixed allowlist. Quarantine rather than erase; never modify vendor data or MQTT.
set -eu
umask 077
root=''
units='gardena-matter-restore.path gardena-matter-status.timer gardena-matter-toggle.socket gardena-matter-web.socket gardena-matter-restore.service gardena-matter-status.service gardena-matter-toggle.service gardena-matter-web.service gardena-matter-bridge.service'
backup="$root/usr/local/lib/gardena-local/matter-backup"
# Reject redirected ancestors before any mutation.
for path in /etc /etc/systemd /etc/systemd/system /usr /usr/local /usr/local/lib /usr/local/lib/gardena-local /usr/share /usr/share/gateway-config-interface /usr/share/gateway-config-interface/www /usr/share/gateway-config-interface/www/assets; do
    test ! -L "$root$path"
done
test ! -L "$backup"
# Reject unrecognised legacy units instead of stopping a partially known installation.
inventory=$(systemctl list-unit-files 'gardena-matter-*' --no-legend --no-pager)
for unit in $(printf '%s\n' "$inventory" | awk '{print $1}'); do
    case " $units " in *" $unit "*) ;; *) exit 21 ;; esac
done
systemctl is-active --quiet gardena-local.service
mkdir -p "$backup"
chmod 700 "$backup"
if test ! -e "$backup/unit-states.txt"; then
    printf '%s\n' "$inventory" > "$backup/unit-states.txt"
fi
# Existing backups are never overwritten; partial runs can be resumed.
for unit in $units; do
    file="$root/etc/systemd/system/$unit"
    if test -e "$file" || test -L "$file"; then
        if test "$(readlink "$file" 2>/dev/null || true)" != /dev/null; then
            if test -e "$backup/$unit" || test -L "$backup/$unit"; then
                if test -L "$file"; then
                    test -L "$backup/$unit"
                    test "$(readlink "$file")" = "$(readlink "$backup/$unit")"
                else
                    test ! -L "$backup/$unit"
                    cmp -s "$file" "$backup/$unit"
                fi
            else
                cp -Pp "$file" "$backup/$unit"
            fi
        fi
    fi
done
# Disable activation triggers before stopping their services.
for unit in $units; do
    state=$(systemctl show "$unit" -p LoadState)
    state=${state#LoadState=}
    case "$state" in
        not-found|masked) ;;
        loaded) systemctl disable "$unit"; systemctl stop "$unit" ;;
        *) exit 22 ;;
    esac
    state=$(systemctl show "$unit" -p ActiveState)
    state=${state#ActiveState=}
    case "$state" in inactive|failed) ;; *) exit 23 ;; esac
done
# Mask only known legacy units to prevent restart by old restore hooks.
for unit in $units; do
    file="$root/etc/systemd/system/$unit"
    rm -f "$file"
    ln -s /dev/null "$file"
done
systemctl daemon-reload
# Move known project directories and the project-specific web page out of live paths.
# Keep shared qrcode.min.js and vendor web files; they may have other users.
for item in etc/gardena-matter usr/local/lib/gardena-matter usr/share/gateway-config-interface/www/assets/matter.html usr/share/gateway-config-interface/www/matter.html; do
    source="$root/$item"
    if test -e "$source" || test -L "$source"; then
        target="$backup/files/$item"
        test ! -e "$target" && test ! -L "$target"
        mkdir -p "$(dirname "$target")"
        mv "$source" "$target"
    fi
done
systemctl is-active --quiet gardena-local.service
for unit in $units; do
    state=$(systemctl show "$unit" -p ActiveState)
    state=${state#ActiveState=}
    case "$state" in inactive|failed) ;; *) exit 24 ;; esac
done
test ! -e "$root/usr/share/gateway-config-interface/www/assets/matter.html"
printf '%s\n' complete > "$backup/complete"
