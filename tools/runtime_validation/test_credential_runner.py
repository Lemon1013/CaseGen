"""Offline credential loader tests: synthetic values only; no database or HTTP."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('credential_runner', Path(__file__).with_name('credential_runner.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / 'credentials.json'
    values = {'username': 'sample_admin', 'password': 'synthetic-test-password-12345',
              'base_url': 'http://127.0.0.1:8001', 'origin': 'http://127.0.0.1:5173'}
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path, values


def test_safe_loading_relative_and_absolute(config, monkeypatch):
    path, values = config
    assert c.load_config(path) == values
    monkeypatch.chdir(path.parent)
    assert c.load_config('./credentials.json') == values


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o400, 0o666, 0o700])
def test_unsafe_permissions_rejected(config, mode):
    path, _ = config
    path.chmod(mode)
    with pytest.raises(c.CredentialError):
        c.load_config(path)


def test_other_owner_rejected(config, monkeypatch):
    path, _ = config
    monkeypatch.setattr(c.os, 'getuid', lambda: path.stat().st_uid + 1)
    with pytest.raises(c.CredentialError):
        c.load_config(path)


def test_symlink_file_and_parent_rejected(config):
    path, _ = config
    link = path.parent / 'linked.json'
    link.symlink_to(path)
    with pytest.raises(c.CredentialError):
        c.load_config(link)
    directory = path.parent / 'linked-directory'
    directory.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(c.CredentialError):
        c.load_config(directory / path.name)


def test_hardlink_rejected(config):
    path, _ = config
    os.link(path, path.with_name('linked.json'))
    with pytest.raises(c.CredentialError):
        c.load_config(path)


@pytest.mark.parametrize('value', ['x' * (c.MAX_CONFIG_BYTES + 1), '{broken', '[]'])
def test_invalid_or_large_config_safe(config, value, capsys):
    path, values = config
    path.write_text(value)
    assert c.main(['--config', str(path), 'prepare']) == 2
    output = capsys.readouterr()
    assert values['password'] not in output.err + output.out
    assert value not in output.err + output.out


@pytest.mark.parametrize('key,value', [('base_url', 'https://example.com'),
    ('base_url', 'http://127.0.0.1:8000'), ('origin', 'http://127.0.0.1:5173@evil.com'),
    ('username', 'invalid name'), ('password', 'short'), ('password', None)])
def test_invalid_fields_rejected(config, key, value):
    path, values = config
    values[key] = value
    path.write_text(json.dumps(values))
    with pytest.raises(c.CredentialError):
        c.load_config(path)


@pytest.mark.parametrize('command', ['prepare', 'validate'])
def test_child_env_forwarding_default_dryrun_and_redaction(config, monkeypatch, capsys, command):
    path, values = config
    monkeypatch.setenv('CASEGEN_PASSWORD', 'original-parent-value')
    seen = {}
    def fake(argv, **kwargs):
        seen['argv'] = argv
        seen['env_values'] = dict(kwargs['env'])
        assert kwargs['capture_output'] and not kwargs['check']
        assert kwargs['cwd'] == c.ROOT
        return subprocess.CompletedProcess(argv, 0, values['password'].encode(), values['username'].encode())
    monkeypatch.setattr(c.subprocess, 'run', fake)
    arguments = ['--project-id', '4', '--space-id', '3'] if command == 'validate' else ['--deadline', '30']
    assert c.main(['--config', str(path), command, *arguments]) == 0
    assert seen['env_values']['CASEGEN_PASSWORD'] == values['password']
    assert seen['env_values']['CASEGEN_USERNAME'] == values['username']
    assert seen['argv'][0] == str(c.ROOT / 'backend/.venv/bin/python')
    assert seen['argv'][1] == str(c.ROOT / 'tools/runtime_validation' / c.COMMANDS[command])
    assert seen['argv'][2:] == arguments
    assert '--execute' not in seen['argv']
    assert values['password'] not in repr(seen['argv'])
    assert os.environ['CASEGEN_PASSWORD'] == 'original-parent-value'
    out = capsys.readouterr()
    assert values['password'] not in out.out + out.err
    assert values['username'] not in out.out + out.err


def test_explicit_execute_forwarded_without_addition(config, monkeypatch):
    path, _ = config
    seen = []
    def fake(argv, **kwargs):
        seen.extend(argv)
        return subprocess.CompletedProcess(argv, 7, b'', b'')
    monkeypatch.setattr(c.subprocess, 'run', fake)
    assert c.main(['--config', str(path), 'prepare', '--', '--execute']) == 7
    assert seen.count('--execute') == 1


@pytest.mark.parametrize('command', ['shell', '/bin/sh', 'python', 'invalid-sensitive-placeholder'])
def test_arbitrary_commands_refused(config, monkeypatch, capsys, command):
    path, _ = config
    monkeypatch.setattr(c.subprocess, 'run', lambda *a, **k: pytest.fail('must not spawn'))
    assert c.main(['--config', str(path), command]) == 2
    assert command not in capsys.readouterr().err


@pytest.mark.parametrize('argument', ['--username-env=X', '--password-env', '--credentials-stdin',
                                     '--user=X', '--password=X', '--cred'])
def test_credential_overrides_refused(config, argument):
    path, _ = config
    with pytest.raises(c.CredentialError):
        c.run('prepare', [argument], path)


def test_subprocess_exception_does_not_echo_secret(config, monkeypatch, capsys):
    path, values = config
    def fail(*args, **kwargs):
        raise OSError(values['password'])
    monkeypatch.setattr(c.subprocess, 'run', fail)
    assert c.main(['--config', str(path), 'prepare']) == 2
    assert values['password'] not in capsys.readouterr().err


def test_missing_path_does_not_echo_path(capsys):
    assert c.main(['--config', '/missing/sensitive-placeholder', 'prepare']) == 2
    assert 'sensitive-placeholder' not in capsys.readouterr().err
