#!/usr/bin/env python
# SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
import getpass
import logging
import os
import sys
from pathlib import Path
from shutil import copytree
from subprocess import PIPE, STDOUT, run
from tempfile import TemporaryDirectory
from typing import Any, Optional, Tuple, Union

IDF_PATH = Path(os.environ['IDF_PATH'])
IDF_PY_PATH = IDF_PATH / 'tools' / 'idf.py'
IDF_DIAG_PY_PATH = IDF_PATH / 'tools' / 'idf_diag.py'
HELLO_WORLD_PATH = IDF_PATH / 'examples' / 'get-started' / 'hello_world'

PathLike = Union[str, Path]


def run_cmd(
    *cmd: PathLike,
    cwd: Optional[PathLike] = None,
    check: bool = True,
    text: bool = True,
) -> Tuple[int, str]:
    logging.info('running: {}'.format(' '.join([str(arg) for arg in cmd])))
    p = run(cmd, stdout=PIPE, stderr=STDOUT, cwd=cwd, check=check, text=text)
    return p.returncode, p.stdout


def run_idf_py(*args: PathLike, **kwargs: Any) -> Tuple[int, str]:
    return run_cmd(sys.executable, IDF_PY_PATH, *args, **kwargs)


def run_diag(*args: PathLike, **kwargs: Any) -> Tuple[int, str]:
    return run_cmd(sys.executable, '-m', 'esp_idf_diag', *args, **kwargs)


def test_idf_diag() -> None:
    # Basic test, compile the hello_world example, generate a report
    # directory, and archive it.

    # temporary directories
    tmpdir = TemporaryDirectory()
    app_path = Path(tmpdir.name) / 'app'
    report_path = Path(tmpdir.name) / 'report'

    # build hello world example
    logging.info('building testing hello_world example')
    copytree(HELLO_WORLD_PATH, app_path)
    run_idf_py('fullclean', cwd=app_path)
    run_idf_py('build', cwd=app_path)

    # create report
    logging.info('creating report')
    run_diag('create', '--output', report_path, cwd=app_path)

    # archive report
    logging.info('creating report archive')
    run_diag('zip', report_path)

    # list recipes
    logging.info('list recipes')
    run_diag('list')

    # check recipes
    logging.info('check recipes')
    run_diag('check')

    # check redaction
    logging.info('check redaction')
    idf_component_path = app_path / 'idf_component.yml'
    idf_component_path.write_text(
        (
            'https://username:password@github.com/username/repository.git\n'
            'MAC EUI-48 00:1A:2B:3C:4D:5E\n'
            'MAC EUI-64 00-1A-2B-FF-FE-3C-4D-5E\n'
            f'USERNAME {getpass.getuser()}\n'
        )
    )
    run_diag('create', '--force', '--output', report_path, cwd=app_path)
    idf_component_path.unlink()
    with open(
        report_path / 'manager' / 'idf_component' / 'idf_component.yml', 'r'
    ) as f:
        data = f.read()
        assert 'https://[XXX]@github.com/username/repository.git' in data
        assert 'MAC EUI-48 [XXX]' in data
        assert 'MAC EUI-64 [XXX]' in data
        assert 'USERNAME [XXX]' in data
