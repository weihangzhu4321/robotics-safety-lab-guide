#!/usr/bin/env bash
# Root-only, one-shot local SSH lab. Creates two isolated netns and removes them on exit.
set -Eeuo pipefail
umask 077

DICT= CLIENT= VENDOR= HYDRA= EVIDENCE= PYTHON=python3 TARGET_INDEX=4 MAX_ATTEMPTS=18
while (($#)); do
  case "$1" in
    --dictionary) DICT=${2:?}; shift 2;;
    --client) CLIENT=${2:?}; shift 2;;
    --vendor) VENDOR=${2:?}; shift 2;;
    --hydra) HYDRA=${2:?}; shift 2;;
    --evidence) EVIDENCE=${2:?}; shift 2;;
    --python) PYTHON=${2:?}; shift 2;;
    --target-index) TARGET_INDEX=${2:?}; shift 2;;
    --max-attempts) MAX_ATTEMPTS=${2:?}; shift 2;;
    -h|--help)
      echo 'Usage: sudo bash isolated_openssh_trial.sh --dictionary FILE --client FILE [--vendor DIR] [--hydra PATH] [--evidence TAR] [--target-index 4] [--max-attempts 18]'
      exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done

[[ $EUID -eq 0 ]] || { echo 'Run as root inside the authorized M guest.' >&2; exit 2; }
[[ -n $DICT && -f $DICT && -n $CLIENT && -f $CLIENT ]] || { echo 'Dictionary and client script files are required.' >&2; exit 2; }
[[ $TARGET_INDEX =~ ^[1-9][0-9]*$ && $MAX_ATTEMPTS =~ ^[1-9][0-9]*$ ]] || { echo 'Index and attempt limit must be positive integers.' >&2; exit 2; }
[[ $MAX_ATTEMPTS -le 18 && $TARGET_INDEX -le 18 && $TARGET_INDEX -le $MAX_ATTEMPTS ]] || { echo 'Require target index <= attempt limit <= 18.' >&2; exit 2; }
for tool in ip sshd ssh ssh-keygen tcpdump ss unshare mount tar sha256sum timeout setsid awk sed grep; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 2; }
done
[[ -x /usr/sbin/sshd ]] || { echo 'Expected /usr/sbin/sshd is not executable.' >&2; exit 2; }
[[ -x $PYTHON ]] || command -v "$PYTHON" >/dev/null || { echo "Python not found: $PYTHON" >&2; exit 2; }
[[ -z $VENDOR || -d $VENDOR ]] || { echo 'Paramiko vendor path is not a directory.' >&2; exit 2; }
[[ -z $HYDRA || -x $HYDRA ]] || { echo 'Hydra path must be executable (or a wrapper).' >&2; exit 2; }

DICT=$(readlink -f -- "$DICT")
CLIENT=$(readlink -f -- "$CLIENT")
[[ -z $VENDOR ]] || VENDOR=$(readlink -f -- "$VENDOR")
[[ -z $HYDRA ]] || HYDRA=$(readlink -f -- "$HYDRA")
mapfile -t CANDIDATES < <(awk '{sub(/\r$/, ""); if (NF) print}' "$DICT")
[[ ${#CANDIDATES[@]} -eq 18 ]] || { echo 'Expected exactly 18 non-empty fictional candidate rows.' >&2; exit 2; }
for word in "${CANDIDATES[@]}"; do
  [[ $word =~ ^[A-Za-z0-9._@+-]+$ ]] || { echo 'Candidate rows must be non-empty printable safe tokens.' >&2; exit 2; }
done

TOKEN=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
CLIENT_NS="l10c${TOKEN}"; SERVER_NS="l10s${TOKEN}"
CLIENT_IF="v10c${TOKEN}"; SERVER_IF="v10s${TOKEN}"
[[ ${#CLIENT_NS} -le 15 && ${#CLIENT_IF} -le 15 ]] || { echo 'Generated Linux interface/namespace name is too long.' >&2; exit 2; }
if ip netns list | awk '{print $1}' | grep -Fxq "$CLIENT_NS" || ip netns list | awk '{print $1}' | grep -Fxq "$SERVER_NS"; then
  echo 'Generated namespace name collision; retry later.' >&2; exit 2
fi
if ip link show "$CLIENT_IF" >/dev/null 2>&1 || ip link show "$SERVER_IF" >/dev/null 2>&1; then
  echo 'Generated interface name collision; retry later.' >&2; exit 2
fi

if [[ -z $EVIDENCE ]]; then EVIDENCE="$PWD/lab10-ssh-evidence-${TOKEN}.tar.gz"; fi
if [[ -e $EVIDENCE || -L $EVIDENCE ]]; then echo "Evidence output already exists: $EVIDENCE" >&2; exit 2; fi
EVIDENCE_PARENT=$(dirname -- "$EVIDENCE")
[[ -d $EVIDENCE_PARENT ]] || { echo 'Evidence output directory must already exist.' >&2; exit 2; }
EVIDENCE_PARENT=$(readlink -f -- "$EVIDENCE_PARENT")
EVIDENCE="$EVIDENCE_PARENT/$(basename -- "$EVIDENCE")"
RUN=$(mktemp -d "/tmp/lab10-sshd-${TOKEN}.XXXXXX")
PRIVATE="$RUN/private"; EVDIR="$RUN/evidence"
PCAP="$RUN/isolated-ssh.pcap"
CLIENT_OWNED=0; SERVER_OWNED=0; LINK_CREATED=0; PRIVSEP_CREATED=0
CAP_PID=; SSHD_LAUNCH_PID=; FINAL_STATUS=0

cleanup() {
  local ns pid
  [[ -z $CAP_PID ]] || { kill "$CAP_PID" 2>/dev/null || true; wait "$CAP_PID" 2>/dev/null || true; }
  for ns in "$CLIENT_NS" "$SERVER_NS"; do
    if { [[ $ns == "$CLIENT_NS" && $CLIENT_OWNED -eq 1 ]] || [[ $ns == "$SERVER_NS" && $SERVER_OWNED -eq 1 ]]; } && ip netns list | awk '{print $1}' | grep -Fxq "$ns"; then
      while read -r pid; do [[ -z $pid ]] || kill "$pid" 2>/dev/null || true; done < <(ip netns pids "$ns" 2>/dev/null || true)
      sleep 0.1
      while read -r pid; do [[ -z $pid ]] || kill -9 "$pid" 2>/dev/null || true; done < <(ip netns pids "$ns" 2>/dev/null || true)
      ip netns del "$ns" 2>/dev/null || true
    fi
  done
  # The unshare parent waits for its sshd child; close that child in the namespace first.
  [[ -z $SSHD_LAUNCH_PID ]] || { kill "$SSHD_LAUNCH_PID" 2>/dev/null || true; wait "$SSHD_LAUNCH_PID" 2>/dev/null || true; }
  if [[ $LINK_CREATED -eq 1 ]]; then
    ip link del "$CLIENT_IF" 2>/dev/null || true
    ip link del "$SERVER_IF" 2>/dev/null || true
  fi
  if [[ $PRIVSEP_CREATED -eq 1 ]]; then rmdir /run/sshd 2>/dev/null || true; fi
  unset TARGET_PASSWORD LAB10_ASKPASS_PASSWORD CANDIDATES 2>/dev/null || true
  rm -rf -- "$RUN"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
chmod 700 "$RUN"
mkdir -m 700 "$PRIVATE" "$EVDIR"

# Verify candidate/Paramiko tooling before creating target resources; do not print dictionary data.
export PYTHONPATH="$VENDOR"
PARAMIKO_VERSION=$("$PYTHON" -B -c 'import paramiko; print(paramiko.__version__)' 2>/dev/null) || {
  echo 'Paramiko unavailable to selected Python; pass --vendor DIR. No package was installed.' >&2; exit 2;
}
PYTHON_VERSION=$("$PYTHON" --version 2>&1)
for tool in chown chmod cp; do command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 2; }; done
if [[ ! -d /run/sshd ]]; then mkdir -m 755 /run/sshd; PRIVSEP_CREATED=1; fi

CLIENT_IP=10.203.254.1; SERVER_IP=10.203.254.2
PORT=$((20000 + RANDOM % 30000))
LAB_USER=lab10demo
TARGET_PASSWORD=${CANDIDATES[$((TARGET_INDEX - 1))]}

# Make an ephemeral password hash without putting the password in process arguments or logs.
PASSWORD_HASH=$(printf '%s' "$TARGET_PASSWORD" | "$PYTHON" -B -W ignore::DeprecationWarning -c 'import crypt,sys; print(crypt.crypt(sys.stdin.read(), crypt.mksalt(crypt.METHOD_SHA512)))' 2>/dev/null) || {
  echo 'Python crypt SHA-512 support unavailable; refusing to create a weaker account.' >&2; exit 2;
}
[[ $PASSWORD_HASH == \$6\$* ]] || { echo 'Could not create SHA-512 shadow hash.' >&2; exit 2; }
cp -- /etc/passwd "$PRIVATE/passwd"; cp -- /etc/shadow "$PRIVATE/shadow"; cp -- /etc/group "$PRIVATE/group"
chmod 644 "$PRIVATE/passwd" "$PRIVATE/group"; chmod 600 "$PRIVATE/shadow"
LAB_UID=61000
while awk -F: -v n="$LAB_UID" '$3==n {found=1} END {exit !found}' "$PRIVATE/passwd" "$PRIVATE/group"; do
  ((LAB_UID+=1)); [[ $LAB_UID -lt 64000 ]] || { echo 'No unused temporary UID/GID in reserved range.' >&2; exit 2; }
done
LAB_GID=$LAB_UID
[[ ! $(awk -F: -v n="$LAB_USER" '$1==n {print $1}' "$PRIVATE/passwd") ]] || { echo 'Temporary account name already exists; refusing collision.' >&2; exit 2; }
printf '%s:x:%s:%s:Lab10 isolated test:/tmp/lab10-home-%s:/bin/sh\n' "$LAB_USER" "$LAB_UID" "$LAB_GID" "$TOKEN" >> "$PRIVATE/passwd"
printf '%s:x:%s:\n' "$LAB_USER" "$LAB_GID" >> "$PRIVATE/group"
TODAY=$(( $(date +%s) / 86400 ))
printf '%s:%s:%s:0:99999:7:::\n' "$LAB_USER" "$PASSWORD_HASH" "$TODAY" >> "$PRIVATE/shadow"
unset PASSWORD_HASH
mkdir "$PRIVATE/home"; chown "$LAB_UID:$LAB_GID" "$PRIVATE/home"; chmod 700 "$PRIVATE/home"
sed -i "s|/tmp/lab10-home-${TOKEN}|$PRIVATE/home|" "$PRIVATE/passwd"

ssh-keygen -q -t ed25519 -N '' -f "$PRIVATE/ssh_host_ed25519_key" >/dev/null
chmod 600 "$PRIVATE/ssh_host_ed25519_key"
printf '[%s]:%s %s\n' "$SERVER_IP" "$PORT" "$(cat "$PRIVATE/ssh_host_ed25519_key.pub")" > "$PRIVATE/known_hosts"
chmod 600 "$PRIVATE/known_hosts"

cat > "$PRIVATE/sshd_config" <<EOF
Port $PORT
ListenAddress $SERVER_IP
AddressFamily inet
HostKey $PRIVATE/ssh_host_ed25519_key
PidFile $PRIVATE/sshd.pid
UsePAM no
PasswordAuthentication yes
KbdInteractiveAuthentication no
AuthenticationMethods password
PermitEmptyPasswords no
PubkeyAuthentication no
PermitRootLogin no
AllowUsers $LAB_USER
StrictModes yes
PermitTTY no
ForceCommand /usr/bin/true
DisableForwarding yes
AllowTcpForwarding no
AllowAgentForwarding no
AllowStreamLocalForwarding no
X11Forwarding no
PermitTunnel no
PermitUserRC no
PermitUserEnvironment no
LoginGraceTime 10
MaxAuthTries 6
MaxStartups 2
UseDNS no
LogLevel VERBOSE
EOF

# Account files are bind-mounted only inside this short-lived mount namespace.
unshare --mount --propagation private --fork /bin/bash -c '
  set -e
  mount --bind "$1" /etc/passwd
  mount --bind "$2" /etc/shadow
  mount --bind "$3" /etc/group
  /usr/sbin/sshd -t -f "$4"
' _ "$PRIVATE/passwd" "$PRIVATE/shadow" "$PRIVATE/group" "$PRIVATE/sshd_config" || {
  echo 'sshd configuration or isolated account validation failed.' >&2; exit 2;
}

ip netns add "$CLIENT_NS"; CLIENT_OWNED=1
ip netns add "$SERVER_NS"; SERVER_OWNED=1
ip link add "$CLIENT_IF" type veth peer name "$SERVER_IF"; LINK_CREATED=1
ip link set "$CLIENT_IF" netns "$CLIENT_NS"
ip link set "$SERVER_IF" netns "$SERVER_NS"
ip -n "$CLIENT_NS" link set lo up
ip -n "$SERVER_NS" link set lo up
ip -n "$CLIENT_NS" addr add "$CLIENT_IP/30" dev "$CLIENT_IF"
ip -n "$SERVER_NS" addr add "$SERVER_IP/30" dev "$SERVER_IF"
ip -n "$CLIENT_NS" link set "$CLIENT_IF" up
ip -n "$SERVER_NS" link set "$SERVER_IF" up
ip netns exec "$CLIENT_NS" sysctl -q -w net.ipv6.conf.all.disable_ipv6=1 net.ipv4.ip_forward=0
ip netns exec "$SERVER_NS" sysctl -q -w net.ipv6.conf.all.disable_ipv6=1 net.ipv4.ip_forward=0
[[ -z $(ip -n "$CLIENT_NS" route show default) && -z $(ip -n "$SERVER_NS" route show default) ]] || {
  echo 'A default route appeared in an isolated namespace; refusing to continue.' >&2; exit 2;
}
[[ $(ip -o -n "$CLIENT_NS" link show | wc -l) -eq 2 && $(ip -o -n "$SERVER_NS" link show | wc -l) -eq 2 ]] || {
  echo 'Namespace contains an unexpected interface.' >&2; exit 2;
}

if [[ -x /usr/sbin/sshd ]]; then :; fi
unshare --mount --propagation private --fork /bin/bash -c '
  set -e
  mount --bind "$1" /etc/passwd
  mount --bind "$2" /etc/shadow
  mount --bind "$3" /etc/group
  exec ip netns exec "$4" /usr/sbin/sshd -D -e -f "$5"
' _ "$PRIVATE/passwd" "$PRIVATE/shadow" "$PRIVATE/group" "$SERVER_NS" "$PRIVATE/sshd_config" >"$RUN/sshd.log" 2>&1 &
SSHD_LAUNCH_PID=$!
for _ in {1..50}; do
  kill -0 "$SSHD_LAUNCH_PID" 2>/dev/null || { echo 'sshd exited during startup.' >&2; exit 3; }
  if ip netns exec "$SERVER_NS" ss -H -ltn "sport = :$PORT" | grep -q .; then break; fi
  sleep 0.1
done
ip netns exec "$SERVER_NS" ss -H -ltn "sport = :$PORT" | grep -q . || { echo 'sshd listener did not become ready.' >&2; exit 3; }

ip netns exec "$CLIENT_NS" tcpdump -U -n -i "$CLIENT_IF" -w "$PCAP" "tcp and host $SERVER_IP and port $PORT" >"$RUN/tcpdump.log" 2>&1 &
CAP_PID=$!
sleep 0.2

# Native OpenSSH client: strict ephemeral host-key pin and one password auth.
cat > "$PRIVATE/askpass" <<'EOF'
#!/bin/sh
printf '%s\n' "${LAB10_ASKPASS_PASSWORD:?}"
EOF
chmod 700 "$PRIVATE/askpass"
NATIVE_STATUS=0
if LAB10_ASKPASS_PASSWORD="$TARGET_PASSWORD" DISPLAY=:lab10 SSH_ASKPASS="$PRIVATE/askpass" SSH_ASKPASS_REQUIRE=force \
  ip netns exec "$CLIENT_NS" setsid -w ssh -F /dev/null -E "$RUN/native-ssh.log" -p "$PORT" \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no -o IdentityAgent=none \
    -o NumberOfPasswordPrompts=1 -o BatchMode=no -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$PRIVATE/known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o HostKeyAlgorithms=ssh-ed25519 -o LogLevel=ERROR "$LAB_USER@$SERVER_IP" true \
    >"$RUN/native.out" 2>"$RUN/native.err"; then
  NATIVE_RESULT=PASS
else NATIVE_STATUS=$?; NATIVE_RESULT=FAIL; FINAL_STATUS=1
fi
unset LAB10_ASKPASS_PASSWORD

# Existing client program, pointed only at the target namespace address.
PARAMIKO_STATUS=0
if PYTHONPATH="$VENDOR" ip netns exec "$CLIENT_NS" "$PYTHON" -B "$CLIENT" ssh --host "$SERVER_IP" --port "$PORT" \
  --username "$LAB_USER" --password-file "$DICT" --known-hosts "$PRIVATE/known_hosts" \
  --max-attempts "$MAX_ATTEMPTS" --delay 1 --confirm-live >"$RUN/paramiko.log" 2>&1; then
  PARAMIKO_RESULT=PASS
else PARAMIKO_STATUS=$?; PARAMIKO_RESULT=FAIL; FINAL_STATUS=1
fi

HYDRA_RESULT=NOT_REQUESTED; HYDRA_STATUS=NA
if [[ -n $HYDRA ]]; then
  # The 18-row list is the hard candidate ceiling; one worker and a one-second connection delay.
  if timeout --kill-after=4s 25s ip netns exec "$CLIENT_NS" "$HYDRA" -l "$LAB_USER" -P "$DICT" -t 1 -w 5 -W 1 -f -s "$PORT" "ssh://$SERVER_IP" >"$RUN/hydra.log" 2>&1; then
    HYDRA_STATUS=0
    if grep -Eq '1 valid password found' "$RUN/hydra.log"; then
      HYDRA_RESULT=PASS
    else HYDRA_RESULT=NO_MATCH; FINAL_STATUS=1
    fi
  else HYDRA_STATUS=$?; HYDRA_RESULT=FAIL; FINAL_STATUS=1
  fi
fi

sleep 0.2
[[ -z $CAP_PID ]] || { kill -INT "$CAP_PID" 2>/dev/null || true; wait "$CAP_PID" 2>/dev/null || true; CAP_PID=; }
sleep 0.2
AUTH_FAILURES=$(grep -c "Failed password for $LAB_USER from $CLIENT_IP " "$RUN/sshd.log" || true)
AUTH_SUCCESSES=$(grep -c "Accepted password for $LAB_USER from $CLIENT_IP " "$RUN/sshd.log" || true)
PARAMIKO_SUMMARY=$(grep -E '^(Attempt [0-9]+/|Authentication accepted after|No accepted password|Stopped:|Stopped on connection)' "$RUN/paramiko.log" | sed -E 's/Password value suppressed\./Password value suppressed./' || true)
PCAP_PACKETS=$(tcpdump -nn -r "$PCAP" 2>/dev/null | wc -l | tr -d ' ')
{
  SSHD_VERSION=$(/usr/sbin/sshd -V 2>&1 | head -n1 || true)
  printf 'scope=loopback-veth-netns-only\npython=%s\nparamiko=%s\nopenssh_server=%s\n' "$PYTHON_VERSION" "$PARAMIKO_VERSION" "$SSHD_VERSION"
  printf 'client_namespace=%s\nserver_namespace=%s\nclient_ip=%s\nserver_ip=%s\nport=%s\n' "$CLIENT_NS" "$SERVER_NS" "$CLIENT_IP" "$SERVER_IP" "$PORT"
  printf 'default_routes=none\nphysical_interfaces_in_namespaces=none\nfictional_dictionary_rows=18\ntarget_candidate_index=%s\nattempt_cap=%s\n' "$TARGET_INDEX" "$MAX_ATTEMPTS"
  printf 'native_ssh=%s\nnative_ssh_exit=%s\nparamiko_client=%s\nparamiko_exit=%s\nhydra=%s\nhydra_exit=%s\n' "$NATIVE_RESULT" "$NATIVE_STATUS" "$PARAMIKO_RESULT" "$PARAMIKO_STATUS" "$HYDRA_RESULT" "$HYDRA_STATUS"
  printf 'server_auth_failures=%s\nserver_auth_successes=%s\nfiltered_pcap_packets=%s\n' "$AUTH_FAILURES" "$AUTH_SUCCESSES" "$PCAP_PACKETS"
  printf '\nParamiko status lines (no candidate values):\n%s\n' "$PARAMIKO_SUMMARY"
  printf '\nEvidence excludes: candidate dictionary, password/hash, private host key, temporary passwd/shadow/group, raw process logs.\n'
} > "$EVDIR/run-summary.txt"
cp -- "$PCAP" "$EVDIR/isolated-ssh.pcap"
(cd "$EVDIR" && sha256sum run-summary.txt isolated-ssh.pcap > SHA256SUMS.txt)
tar -C "$EVDIR" -czf "$EVIDENCE" run-summary.txt isolated-ssh.pcap SHA256SUMS.txt
chmod 600 "$EVIDENCE"
echo "Evidence archive: $EVIDENCE"
echo "Native SSH: $NATIVE_RESULT; Paramiko: $PARAMIKO_RESULT; Hydra: $HYDRA_RESULT; server auth failures=$AUTH_FAILURES successes=$AUTH_SUCCESSES; filtered packets=$PCAP_PACKETS"
exit "$FINAL_STATUS"
