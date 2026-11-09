#!/usr/bin/env python3
"""Offline candidate generation and explicitly authorized SSH password check."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import math
import hashlib
import itertools
import sys
import time
from pathlib import Path

HARD_MAX_ATTEMPTS = 50
MIN_DELAY_SECONDS = 1.0
CONNECT_TIMEOUT = 5.0
AUTH_TIMEOUT = 5.0
BANNER_TIMEOUT = 5.0


def candidates(surname: str, given_initials: str, student_number: str) -> list[str]:
    """Return unique, deterministic concatenations of all 6 segment orders."""
    parts = (surname.strip(), given_initials.strip(), student_number.strip())
    if any(not p for p in parts):
        raise ValueError("surname, given initials, and student number must be non-empty")
    if any(any(ch.isspace() for ch in p) for p in parts):
        raise ValueError("segments must not contain whitespace")
    return list(dict.fromkeys("".join(order) for order in itertools.permutations(parts)))


def generate(args: argparse.Namespace) -> int:
    try:
        words = candidates(args.surname, args.given_initials, args.student_number)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.output)
    out.write_text("".join(word + "\n" for word in words), encoding="utf-8", newline="\n")
    print(f"Generated {len(words)} unique candidates at {out}; contents are sensitive.")
    return 0


def _fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _run_ssh(args: argparse.Namespace, passwords: list[str], paramiko_module=None,
             sleep=time.sleep) -> int:
    if paramiko_module is None:
        try:
            import paramiko as paramiko_module
        except ImportError:
            print("Paramiko is required for SSH mode; no package was installed.", file=sys.stderr)
            return 2

    host = args.host.strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        print("Host must be a literal IPv4 or IPv6 address.", file=sys.stderr)
        return 2
    if ("%" in host or address.is_unspecified or address.is_multicast or address.is_reserved
            or (address.version == 4 and address == ipaddress.IPv4Address("255.255.255.255"))):
        print("Host must be a unicast IP address; scoped, unspecified, multicast, reserved, and broadcast addresses are rejected.", file=sys.stderr)
        return 2
    if not args.confirm_live:
        print("Live mode blocked: pass --confirm-live after confirming written authorization.", file=sys.stderr)
        return 2
    if not args.username.strip():
        print("Username is required.", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    if (bool(args.known_hosts) == bool(args.host_key)):
        print("Select exactly one: --known-hosts or --host-key SHA256:...", file=sys.stderr)
        return 2
    if not 1 <= args.max_attempts <= HARD_MAX_ATTEMPTS:
        print(f"Attempt limit must be between 1 and {HARD_MAX_ATTEMPTS}.", file=sys.stderr)
        return 2
    if not math.isfinite(args.delay) or args.delay < MIN_DELAY_SECONDS:
        print(f"Delay must be at least {MIN_DELAY_SECONDS:.0f} second(s).", file=sys.stderr)
        return 2
    passwords = [p.rstrip("\r\n") for p in passwords]
    passwords = [p for p in passwords if p]
    if not passwords:
        print("Password list is empty.", file=sys.stderr)
        return 2
    passwords = passwords[:args.max_attempts]

    client = paramiko_module.SSHClient()
    PinnedHostKeyMismatch = type("PinnedHostKeyMismatch", (paramiko_module.SSHException,), {})
    try:
        if args.known_hosts:
            known_hosts_path = Path(args.known_hosts)
            if not known_hosts_path.is_file():
                print("Specified known-hosts file does not exist or is not a regular file.", file=sys.stderr)
                return 2
            try:
                client.load_host_keys(str(known_hosts_path))
            except (OSError, paramiko_module.SSHException):
                print("Specified known-hosts file could not be read or parsed.", file=sys.stderr)
                return 2
            client.set_missing_host_key_policy(paramiko_module.RejectPolicy())
        else:
            expected = args.host_key
            if not expected.startswith("SHA256:"):
                print("Pinned host key must be a SHA256: fingerprint.", file=sys.stderr)
                return 2

            class PinnedPolicy(paramiko_module.MissingHostKeyPolicy):
                def missing_host_key(self, _client, _hostname, key):
                    if _fingerprint(key) != expected:
                        raise PinnedHostKeyMismatch("Pinned host key mismatch")
                    return None

            # The callback accepts only this exact key; known keys still use Paramiko comparison.
            client.set_missing_host_key_policy(PinnedPolicy())

        for index, password in enumerate(passwords, start=1):
            try:
                client.connect(
                    hostname=host, port=args.port, username=args.username,
                    password=password, timeout=CONNECT_TIMEOUT,
                    auth_timeout=AUTH_TIMEOUT, banner_timeout=BANNER_TIMEOUT,
                    allow_agent=False, look_for_keys=False,
                )
                print(f"Authentication accepted after {index} attempt(s). Password value suppressed.")
                return 0
            except paramiko_module.AuthenticationException:
                print(f"Attempt {index}/{len(passwords)}: authentication rejected.")
            except paramiko_module.BadHostKeyException:
                print("Stopped: SSH host key did not match the trusted key.", file=sys.stderr)
                return 3
            except PinnedHostKeyMismatch:
                print("Stopped: SSH host key did not match the pinned fingerprint.", file=sys.stderr)
                return 3
            except (paramiko_module.SSHException, OSError, TimeoutError) as exc:
                print(f"Stopped on connection/SSH error ({type(exc).__name__}); no further attempts.", file=sys.stderr)
                return 4
            finally:
                client.close()
            if index < len(passwords):
                sleep(args.delay)
        print(f"No accepted password in {len(passwords)} attempt(s).")
        return 1
    finally:
        client.close()


def ssh_command(args: argparse.Namespace) -> int:
    if not args.confirm_live:
        print("Live mode blocked: pass --confirm-live after confirming written authorization.", file=sys.stderr)
        return 2
    if not 1 <= args.max_attempts <= HARD_MAX_ATTEMPTS:
        print(f"Attempt limit must be between 1 and {HARD_MAX_ATTEMPTS}.", file=sys.stderr)
        return 2
    if not math.isfinite(args.delay) or args.delay < MIN_DELAY_SECONDS:
        print(f"Delay must be at least {MIN_DELAY_SECONDS:.0f} second(s).", file=sys.stderr)
        return 2
    try:
        passwords = []
        with Path(args.password_file).open("r", encoding="utf-8-sig") as stream:
            for line in stream:
                password = line.rstrip("\r\n")
                if password:
                    passwords.append(password)
                    if len(passwords) >= args.max_attempts:
                        break
    except OSError as exc:
        print(f"Cannot read password file ({type(exc).__name__}).", file=sys.stderr)
        return 2
    return _run_ssh(args, passwords)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="offline generation from three supplied segments")
    gen.add_argument("--surname", required=True, help="surname in pinyin (fictional data for practice)")
    gen.add_argument("--given-initials", required=True, help="given-name initials")
    gen.add_argument("--student-number", required=True)
    gen.add_argument("--output", required=True, help="local candidate file; protect its contents")
    gen.set_defaults(func=generate)

    ssh = sub.add_parser("ssh", help="test a candidate file against one authorized SSH endpoint")
    ssh.add_argument("--host", required=True, help="one explicit authorized IP or hostname")
    ssh.add_argument("--port", type=int, required=True)
    ssh.add_argument("--username", required=True)
    ssh.add_argument("--password-file", required=True)
    trust = ssh.add_mutually_exclusive_group(required=True)
    trust.add_argument("--known-hosts", help="trusted known_hosts file; unknown keys rejected")
    trust.add_argument("--host-key", help="pre-verified SHA256: fingerprint")
    ssh.add_argument("--max-attempts", required=True, type=int, help=f"hard maximum {HARD_MAX_ATTEMPTS}")
    ssh.add_argument("--delay", required=True, type=float, help=f"seconds between failures; minimum {MIN_DELAY_SECONDS:g}")
    ssh.add_argument("--confirm-live", action="store_true", help="confirm written course authorization and endpoint scope")
    ssh.set_defaults(func=ssh_command)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
