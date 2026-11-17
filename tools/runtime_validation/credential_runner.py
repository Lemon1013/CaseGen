#!/usr/bin/env python3
"""Load private credentials for one of the two opt-in runtime validators."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MAX_CONFIG_BYTES = 16384
COMMANDS = {'prepare': 'prepare_sources.py', 'validate': 'validate_coverage_flow.py'}


class CredentialError(Exception):
    """Only fixed diagnostics may cross the CLI boundary."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CredentialError('invalid command line; use --help')


def load_config(path: str | Path) -> dict:
    """Read once through a verified descriptor; never evaluate configuration."""
    try:
        target = Path(os.path.abspath(os.fspath(path)))
        for component in [*reversed(target.parents), target]:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise CredentialError('credential path must not contain symlinks')
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise CredentialError('credential file must be owned by current user with mode 0600')
            if info.st_size > MAX_CONFIG_BYTES:
                raise CredentialError('credential file exceeds size limit')
            raw = handle.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise CredentialError('credential file exceeds size limit')
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise CredentialError('invalid credential configuration')
        username, password = config.get('username'), config.get('password')
        if (not isinstance(username, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{2,31}', username)
                or not isinstance(password, str) or not 10 <= len(password) <= 1024
                or '\x00' in password):
            raise CredentialError('invalid credential fields')
        if (config.get('base_url') != 'http://127.0.0.1:8001'
                or config.get('origin') != 'http://127.0.0.1:5173'):
            raise CredentialError('only authorized loopback base URL and origin are allowed')
        return config
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise CredentialError('cannot safely load credential configuration') from None


def run(command: str, arguments: list[str], config_path: str | Path) -> int:
    if command not in COMMANDS:
        raise CredentialError('only prepare and validate subcommands are allowed')
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    protected = {'--username-env', '--password-env', '--credentials-stdin'}
    if any(option.startswith('--') and any(flag.startswith(option) for flag in protected)
           for option in (arg.split('=', 1)[0] for arg in arguments)):
        raise CredentialError('credential source overrides are forbidden')
    config = load_config(config_path)
    env = os.environ.copy()
    env['CASEGEN_USERNAME'] = config['username']
    env['CASEGEN_PASSWORD'] = config['password']
    argv = [str(ROOT / 'backend/.venv/bin/python'),
            str(ROOT / 'tools/runtime_validation' / COMMANDS[command]), *arguments]
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, check=False)
    except (OSError, ValueError):
        raise CredentialError('runtime validator could not be started') from None
    finally:
        env.pop('CASEGEN_USERNAME', None)
        env.pop('CASEGEN_PASSWORD', None)
    for output, stream in ((result.stdout, sys.stdout), (result.stderr, sys.stderr)):
        text = output.decode('utf-8', errors='replace')
        for value in (config['password'], config['username']):
            text = text.replace(value, '[redacted]')
        stream.write(text)
    return result.returncode


def main(argv=None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Explicit private JSON path (0600)')
    parser.add_argument('command', choices=tuple(COMMANDS))
    parser.add_argument('arguments', nargs=argparse.REMAINDER,
                        help='Arguments forwarded unchanged; --execute remains opt-in')
    try:
        args = parser.parse_args(argv)
        return run(args.command, args.arguments, args.config)
    except CredentialError as exc:
        print(f'credential runner: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('credential runner: interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
