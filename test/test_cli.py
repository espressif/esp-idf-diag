#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
"""CLI-level tests for esp-idf-diag.

These exercise the rich-click command-line interface without a real ESP-IDF
build or hardware: argument parsing, exit codes, stdout/stderr routing, and the
IDF_PATH gate. The recipe-loading commands only require IDF_PATH to be *set*
(not a real installation), so these run anywhere.
"""

import os
import re
import subprocess
import sys
from typing import Optional, Tuple

ANSI_RE = re.compile(r'\x1b\[')
SUBCOMMANDS = ['create', 'list', 'check', 'zip']


def run_diag(
    *args: str,
    idf_path: Optional[str] = '/tmp',
    cwd: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run the diag CLI and return (returncode, stdout, stderr)."""
    env = dict(os.environ)
    if idf_path is None:
        env.pop('IDF_PATH', None)
    else:
        env['IDF_PATH'] = idf_path
    p = subprocess.run(
        [sys.executable, '-m', 'esp_idf_diag', *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
    )
    return p.returncode, p.stdout, p.stderr


def test_no_args_exits_1_with_help_on_stderr(tmp_path) -> None:
    rc, out, err = run_diag(cwd=str(tmp_path))
    assert rc == 1
    assert out == ''
    assert 'Usage' in err


def test_root_help_exits_0(tmp_path) -> None:
    rc, out, err = run_diag('--help', cwd=str(tmp_path))
    assert rc == 0
    assert 'Usage' in out
    for cmd in SUBCOMMANDS:
        assert cmd in out


def test_h_alias_exits_0(tmp_path) -> None:
    rc, out, err = run_diag('-h', cwd=str(tmp_path))
    assert rc == 0
    assert 'Usage' in out


def test_subcommand_help_exits_0(tmp_path) -> None:
    for cmd in SUBCOMMANDS:
        rc, out, err = run_diag(cmd, '--help', cwd=str(tmp_path))
        assert rc == 0, f'{cmd} --help exited {rc}: {err}'
        assert 'Usage' in out


def test_unknown_option_exits_2(tmp_path) -> None:
    rc, out, err = run_diag('list', '--bogus', cwd=str(tmp_path))
    assert rc == 2


def test_options_before_subcommand_rejected(tmp_path) -> None:
    # Common options live on the subcommands (argparse parity), so passing
    # them before the subcommand is a usage error.
    rc, out, err = run_diag('--debug', 'list', cwd=str(tmp_path))
    assert rc == 2


def test_list_lists_builtin_recipes(tmp_path) -> None:
    rc, out, err = run_diag('list', cwd=str(tmp_path))
    assert rc == 0
    assert 'short name: idf' in out
    assert 'builtin: True' in out


def test_check_passes(tmp_path) -> None:
    rc, out, err = run_diag('check', cwd=str(tmp_path))
    assert rc == 0
    assert 'OK' in out


def test_list_no_color_has_no_ansi(tmp_path) -> None:
    rc, out, err = run_diag('list', '--no-color', cwd=str(tmp_path))
    assert rc == 0
    assert not ANSI_RE.search(out)


def test_log_prefix_adds_severity_char(tmp_path) -> None:
    rc, out, err = run_diag('check', '--log-prefix', cwd=str(tmp_path))
    assert rc == 0
    # Report/output lines get an 'O ' severity prefix when --log-prefix is set.
    assert any(line.startswith('O ') for line in out.splitlines())


def test_missing_idf_path_exits_128(tmp_path) -> None:
    rc, out, err = run_diag('list', idf_path=None, cwd=str(tmp_path))
    assert rc == 128
    assert 'IDF_PATH is not set' in err
