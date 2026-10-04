#!/bin/sh
# Run once as root with a pre-initialized dedicated state and public deployment key.
set -eu
root=/opt/w2f-v040
test "$(id -u)" -eq 0
test -f "$root/state/.w2f-v040"
test -f "$root/deploy_key.pub"
test -f "$root/release.py"
test -f "$root/compose.yaml"
test -f "$root/Caddyfile"

if ! id w2f-v040-deploy >/dev/null 2>&1; then
    useradd --create-home --shell /bin/sh w2f-v040-deploy
fi
if id -nG w2f-v040-deploy | tr ' ' '\n' | grep -Eq '^(docker|sudo)$'; then
    echo 'Deployment user must not belong to docker or sudo groups' >&2
    exit 1
fi
install -o root -g root -m 755 "$root/release.py" /usr/local/sbin/w2f-v040-release
cat > /usr/local/bin/w2f-v040-ssh <<'EOF'
#!/bin/sh
exec sudo -n /usr/local/sbin/w2f-v040-release "${SSH_ORIGINAL_COMMAND:-}"
EOF
chown root:root /usr/local/bin/w2f-v040-ssh
chmod 755 /usr/local/bin/w2f-v040-ssh
printf '%s\n' 'w2f-v040-deploy ALL=(root) NOPASSWD: /usr/local/sbin/w2f-v040-release' > /etc/sudoers.d/w2f-v040
chmod 440 /etc/sudoers.d/w2f-v040
visudo -cf /etc/sudoers.d/w2f-v040
install -d -o root -g root -m 755 /home/w2f-v040-deploy/.ssh
{ printf '%s ' 'restrict,command="/usr/local/bin/w2f-v040-ssh"'; cat "$root/deploy_key.pub"; } > /home/w2f-v040-deploy/.ssh/authorized_keys
chown root:root /home/w2f-v040-deploy /home/w2f-v040-deploy/.ssh/authorized_keys
chmod 755 /home/w2f-v040-deploy
chmod 644 /home/w2f-v040-deploy/.ssh/authorized_keys
chmod 700 "$root" "$root/state"
echo 'Dedicated deployment account provisioned; no application started'
