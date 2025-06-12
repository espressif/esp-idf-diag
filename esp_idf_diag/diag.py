# SPDX-FileCopyrightText: 2024-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
import argparse
import atexit
import difflib
import getpass
import os
import platform
import re
import shutil
import sys
import textwrap
import traceback
import uuid
import zipfile
from argparse import Namespace
from pathlib import Path
from string import Template
from subprocess import run
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

import yaml
from rich.console import Console

CONSOLE_STDERR = None
CONSOLE_STDOUT = None

# Logging levels and configurations.
# stderr
LOG_FATAL = 1
LOG_ERROR = 2
LOG_WARNING = 3
LOG_INFO = 4
LOG_DEBUG = 5
# stdout
LOG_STDOUT = 6
LOG_HINT = 7

LOG_LEVEL = LOG_INFO
LOG_PREFIX = False
LOG_HINTS = True

# A temporary directory is used to store the report. Once it is completely
# generated, it is moved to its final location.
TMP_DIR = TemporaryDirectory()
TMP_DIR_PATH = Path(TMP_DIR.name)
TMP_DIR_REPORT_PATH = TMP_DIR_PATH / 'report'
TMP_DIR_REPORT_REDACTED_PATH = TMP_DIR_PATH / 'redacted'

# The full debug log will be stored in the report directory alongside other
# collected files.
LOG_FILE = None
LOG_FILE_PATH = TMP_DIR_PATH / 'diag.log'

# Fixed path for the built-in recipes
BUILTIN_RECIPES_PATH = (Path(__file__).parent / 'data' / 'recipes').resolve()

# Fixed path for the built-in purge
BUILTIN_PURGE_PATH = (
    Path(__file__).parent / 'data' / 'purge' / 'purge.yml'
).resolve()


def cleanup() -> None:
    """Perform cleanup operations in case of unexpected termination."""
    try:
        if LOG_FILE:
            LOG_FILE.close()
        shutil.rmtree(TMP_DIR_PATH)
    except Exception:
        pass


atexit.register(cleanup)


def exception_tb() -> Optional[str]:
    """Return a string containing the message from the most recent exception,
    along with its traceback, if available.
    """
    ex_type, ex_value, ex_traceback = sys.exc_info()
    in_exception = ex_type is not None
    if not in_exception:
        return None
    ex_msg = f'exception {ex_type}:'
    if str(ex_value):
        ex_msg += f' {ex_value}'
    tb = ''.join(traceback.format_tb(ex_traceback))
    ex_msg += '\n' + tb.rstrip()
    ex_msg = textwrap.indent(ex_msg, prefix='> ')
    return ex_msg


def exception_msg() -> Optional[str]:
    """Return a string containing the message from the most recent exception,
    if available.
    """
    ex_type, ex_value, ex_traceback = sys.exc_info()
    in_exception = ex_type is not None
    if not in_exception or not str(ex_value):
        return None
    return str(ex_value)


def log(
    level: int, msg: str, prefix: str, no_prefix: bool = False, **kwargs: Any
) -> None:
    """Logs a message with a specified severity level and prefix.

    This function outputs log messages to standard error (stderr) based on the
    provided severity level. All messages are also saved to a log file, which
    is part of the diagnostic report. The log file entries include a severity
    prefix but do not contain any color formatting.

    Parameters:
    level (int): The severity level of the log message.
    msg (str): The message to be logged.
    prefix (str): A character prefix to indicate the log severity.
    no_prefix (bool): Do not add a prefix, even if requested globally. It can
                      be used for line continuation.
    kwargs (Any): Passed to the rich print function.

    Returns:
    None
    """
    global LOG_FILE
    if LOG_PREFIX and not no_prefix:
        log_prefix = f'{prefix} '
    else:
        log_prefix = ''

    if LOG_FILE:
        try:
            log_msg = textwrap.indent(msg, prefix=f'{prefix} ')
            LOG_FILE.write(log_msg + '\n')
            LOG_FILE.flush()
        except Exception:
            LOG_FILE.close()
            LOG_FILE = None
            err(
                (
                    f'Cannot write to log file "{LOG_FILE}". '
                    f'Logging to file is turned off.'
                )
            )

    msg = textwrap.indent(msg, prefix=log_prefix)

    color = {
        LOG_FATAL: '[red1]',
        LOG_ERROR: '[red]',
        LOG_WARNING: '[yellow]',
        LOG_INFO: '[steel_blue1]',
        LOG_DEBUG: '[grey46]',
        LOG_HINT: '[orange3]',
        LOG_STDOUT: '',
    }[level]
    msg = f'{color}{msg}'

    if level >= LOG_STDOUT:
        assert CONSOLE_STDOUT  # help mypy
        CONSOLE_STDOUT.print(msg, **kwargs)
        sys.stdout.flush()
    elif level <= LOG_LEVEL:
        assert CONSOLE_STDERR  # help mypy
        CONSOLE_STDERR.print(msg, **kwargs)
        sys.stderr.flush()


def die(msg: str, show_hint: bool = True) -> None:
    """Irrecoverable fatal error."""
    fatal(msg)
    # Avoid calling fatal, as it may print the exception again if present.
    log(LOG_FATAL, 'ESP-IDF diagnostic command failed.', 'F')
    if show_hint and LOG_LEVEL != LOG_DEBUG:
        # If the log level for stderr is not set to debug, suggest it.
        hint('Using the "-d/--debug" option may provide more information.')
    sys.exit(128)


def log_with_exception(level: int, msg: str, prefix: str) -> None:
    ex_msg = exception_msg()
    if ex_msg:
        msg += f': {ex_msg}'
    log(level, msg, prefix)
    ex_tb = exception_tb()
    if ex_tb:
        log(LOG_DEBUG, ex_tb, 'D')


def fatal(msg: str) -> None:
    log_with_exception(LOG_FATAL, 'fatal: ' + msg, 'F')


def err(msg: str) -> None:
    log_with_exception(LOG_ERROR, 'error: ' + msg, 'E')


def warn(msg: str) -> None:
    log_with_exception(LOG_WARNING, 'warning: ' + msg, 'W')


def info(msg: str) -> None:
    log_with_exception(LOG_INFO, msg, 'I')


def dbg(msg: str) -> None:
    log_with_exception(LOG_DEBUG, msg, 'D')


def oprint(msg: str, **kwargs: Any) -> None:
    log(LOG_STDOUT, msg, 'O', **kwargs)


def hint(msg: str) -> None:
    if LOG_HINTS:
        log(LOG_HINT, msg, 'H')


def set_logger(args: Namespace) -> None:
    global LOG_LEVEL
    global LOG_FILE
    global LOG_PREFIX
    global LOG_HINTS
    global CONSOLE_STDOUT
    global CONSOLE_STDERR

    if args.debug:
        LOG_LEVEL = LOG_DEBUG
    elif args.no_hints:
        LOG_LEVEL = LOG_INFO

    if args.log_prefix:
        LOG_PREFIX = True

    LOG_HINTS = not args.no_hints

    CONSOLE_STDOUT = Console(
        no_color=args.no_color,
        force_terminal=args.force_terminal,
        soft_wrap=True,
    )
    CONSOLE_STDERR = Console(
        stderr=True,
        no_color=args.no_color,
        force_terminal=args.force_terminal,
        soft_wrap=True,
    )

    try:
        LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE = open(LOG_FILE_PATH, 'w')
    except Exception:
        err(
            (
                f'Cannot open log file "{LOG_FILE}". '
                f'Log file will not be generated.'
            )
        )


def diff_dirs(dir1: Path, dir2: Path) -> None:
    """Show differences in files between two directories."""
    dir1_root_path = Path(dir1).resolve()
    dir2_root_path = Path(dir2).resolve()
    dbg(f'diff "{dir1_root_path}" to "{dir2_root_path}"')
    for dir1_file_path in dir1_root_path.rglob('*'):
        if not dir1_file_path.is_file():
            continue
        dir2_file_path = dir2_root_path / dir1_file_path.relative_to(
            dir1_root_path
        )

        with open(dir1_file_path, 'r') as f1, open(dir2_file_path, 'r') as f2:
            diff = difflib.unified_diff(
                f1.readlines(),
                f2.readlines(),
                fromfile=str(
                    dir1_file_path.relative_to(dir1_root_path.parent)
                ),
                tofile=str(dir2_file_path.relative_to(dir2_root_path.parent)),
                n=0,
            )
            for line in diff:
                dbg(line.strip())


def redact_files(dir1: Path, dir2: Path, purge: list) -> None:
    """Remove sensitive information from files in the report directory
    according to the purge instructions."""

    regexes: List = []
    for entry in purge:
        regex = re.compile(entry['regex'])
        repl = entry['repl']
        regexes.append((regex, repl))

    dir1_root_path = Path(dir1).resolve()
    dir2_root_path = Path(dir2).resolve()
    dbg(f'redacting files in "{dir1_root_path}" into "{dir2_root_path}"')
    for dir1_file_path in dir1_root_path.rglob('*'):
        if not dir1_file_path.is_file():
            continue
        dir2_file_path = dir2_root_path / dir1_file_path.relative_to(
            dir1_root_path
        )
        dir2_file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dir1_file_path, 'r') as f1, open(dir2_file_path, 'w') as f2:
            data = f1.read()
            for regex, repl in regexes:
                if not regex:
                    continue
                data = regex.sub(repl, data)
            f2.write(data)

    diff_dirs(dir1, dir2)


def validate_recipe(recipe: Dict) -> None:
    """Validate the loaded recipe file. This is done manually to avoid any
    dependencies and to provide more informative error messages.
    """
    recipe_keys = ['description', 'tags', 'output', 'steps']
    step_keys = ['name', 'cmds', 'output', 'system', 'port']
    recipe_description = recipe.get('description')
    recipe_tags = recipe.get('tags')
    recipe_output = recipe.get('output')
    recipe_steps = recipe.get('steps')

    for key in recipe:
        if key not in recipe_keys:
            raise RuntimeError(
                f'Unknown recipe key "{key}", expecting "{recipe_keys}"'
            )

    if not recipe_description:
        raise RuntimeError('Recipe is missing "description" key')

    if not isinstance(recipe_description, str):
        raise RuntimeError('Recipe "description" key is not of type "str"')

    if recipe_tags:
        if not isinstance(recipe_tags, list):
            raise RuntimeError('Recipe "tags" key is not of type "list"')
        for tag in recipe_tags:
            if not isinstance(tag, str):
                raise RuntimeError(
                    f'Recipe tag value "{tag}" is not of type "str"'
                )

    if recipe_output:
        if not isinstance(recipe_output, str):
            raise RuntimeError('Recipe "output" key is not of type "str"')

    if not recipe_steps:
        raise RuntimeError('Recipe is missing "steps" key')

    if not isinstance(recipe_steps, list):
        raise RuntimeError('Recipe "steps" key is not of type "list"')

    for step in recipe_steps:
        for key in step:
            if key not in step_keys:
                raise RuntimeError(
                    f'Unknown recipe step key "{key}", expecting "{step_keys}"'
                )

        step_name = step.get('name')
        step_output = step.get('output')
        step_cmds = step.get('cmds')
        step_system = step.get('system')
        step_port = step.get('port')

        if not step_name:
            raise RuntimeError('Recipe step is missing "name" key')
        if not isinstance(step_name, str):
            raise RuntimeError('Recipe step "name" key is not of type "str"')
        if not step_cmds:
            raise RuntimeError('Recipe step is missing "cmds" key')
        if not isinstance(step_cmds, list):
            raise RuntimeError('Recipe step "cmds" key is not of type "list"')
        if step_output:
            if not isinstance(step_output, str):
                raise RuntimeError('Step "output" key is not of type "str"')
        if step_system:
            if not isinstance(step_system, str):
                raise RuntimeError('Step "system" key is not of type "str"')
            if step_system not in ['Linux', 'Windows', 'Darwin']:
                raise RuntimeError(
                    (
                        f'Unknown "system" key value "{step_system}", '
                        f'expecting "Linux", "Windows" or "Darwin"'
                    )
                )
        if step_port:
            if not isinstance(step_port, bool):
                raise RuntimeError('Step "port" key is not of type "bool"')

        for cmd in step_cmds:
            if 'exec' in cmd:
                cmd_exec_keys = [
                    'exec',
                    'cmd',
                    'output',
                    'stderr',
                    'timeout',
                    'append',
                ]

                exec_cmd = cmd.get('cmd')
                output = cmd.get('output')
                stderr = cmd.get('stderr')
                timeout = cmd.get('timeout')
                append = cmd.get('append')

                for key in cmd:
                    if key not in cmd_exec_keys:
                        raise RuntimeError(
                            (
                                f'Unknown "exec" command argument "{key}" in '
                                f'step "{step_name}", expecting '
                                f'"{cmd_exec_keys}"'
                            )
                        )

                # Required arguments
                if not exec_cmd:
                    raise RuntimeError(
                        (
                            f'Command "exec" in step "{step_name}" '
                            f'is missing "cmd" argument'
                        )
                    )
                if isinstance(exec_cmd, list):
                    for arg in exec_cmd:
                        if not isinstance(arg, str):
                            raise RuntimeError(
                                (
                                    f'List entry "{arg}" in "cmd" argument '
                                    f'for command "exec" in step '
                                    f'"{step_name}" is not of type "str"'
                                )
                            )
                elif not isinstance(exec_cmd, str):
                    raise RuntimeError(
                        (
                            f'Command "exec" in step "{step_name}" '
                            f'is not of type "list" or "str"'
                        )
                    )

                # Optional arguments
                if output and not isinstance(output, str):
                    raise RuntimeError(
                        (
                            f'Argument "output" for command "exec" in '
                            f'step "{step_name}" is not of type "str"'
                        )
                    )
                if stderr and not isinstance(stderr, str):
                    raise RuntimeError(
                        (
                            f'Argument "stderr" for command "exec" '
                            f'in step "{step_name}" is not of type "str"'
                        )
                    )
                if timeout and not isinstance(timeout, int):
                    raise RuntimeError(
                        (
                            f'Argument "timeout" for command "exec" '
                            f'in step "{step_name}" is not of type "int"'
                        )
                    )
                if append and not isinstance(append, bool):
                    raise RuntimeError(
                        (
                            f'Argument "append" for command "exec" '
                            f'in step "{step_name}" is not of type "bool"'
                        )
                    )

            elif 'file' in cmd:
                cmd_file_keys = ['file', 'path', 'output']

                path = cmd.get('path')
                output = cmd.get('output')

                for key in cmd:
                    if key not in cmd_file_keys:
                        raise RuntimeError(
                            (
                                f'Unknown "file" command argument "{key}" in '
                                f'step "{step_name}", expecting '
                                f'"{cmd_file_keys}"'
                            )
                        )

                # Required arguments
                if not path:
                    raise RuntimeError(
                        (
                            f'Command "file" in step "{step_name}" '
                            f'is missing "path" argument'
                        )
                    )
                if not isinstance(path, str):
                    raise RuntimeError(
                        (
                            f'Argument "path" for command "file" in step '
                            f'"{step_name}" is not of type "str"'
                        )
                    )

                # Optional arguments
                if output and not isinstance(output, str):
                    raise RuntimeError(
                        (
                            f'Argument "output" for command "file" in step '
                            f' "{step_name}" is not of type "str"'
                        )
                    )

            elif 'env' in cmd:
                cmd_env_keys = ['env', 'vars', 'regex', 'output', 'append']

                variables = cmd.get('vars')
                regex = cmd.get('regex')
                output = cmd.get('output')
                append = cmd.get('append')

                for key in cmd:
                    if key not in cmd_env_keys:
                        raise RuntimeError(
                            (
                                f'Unknown "env" command argument "{key}" in '
                                f'step "{step_name}", expecting '
                                f'"{cmd_env_keys}"'
                            )
                        )

                # Required arguments
                if not variables and not regex:
                    raise RuntimeError(
                        (
                            f'Command "env" in step "{step_name}" is missing '
                            f'both "vars" and "regex" arguments'
                        )
                    )
                if variables:
                    if not isinstance(variables, list):
                        raise RuntimeError(
                            (
                                f'Argument "vars" for command "env" in step '
                                f'"{step_name}" is not of type "list"'
                            )
                        )
                    for var in variables:
                        if not isinstance(var, str):
                            raise RuntimeError(
                                (
                                    f'List entry "{var}" in "vars" argument '
                                    f'for command "env" in step "{step_name}" '
                                    f'is not of type "str"'
                                )
                            )
                if regex:
                    if not isinstance(regex, str):
                        raise RuntimeError(
                            (
                                f'Argument "regex" for command "env" in step '
                                f'"{step_name}" is not of type "str"'
                            )
                        )
                    try:
                        re.compile(regex)
                    except re.error as e:
                        raise RuntimeError(
                            (
                                f'Argument "regex" for command "env" in step '
                                f'"{step_name}" is not a valid regular '
                                f'expression: {e}'
                            )
                        )

                # Optional arguments
                if output and not isinstance(output, str):
                    raise RuntimeError(
                        (
                            f'Argument "output" for command "env" in step '
                            f'"{step_name}" is not of type "str"'
                        )
                    )
                if append and not isinstance(append, bool):
                    raise RuntimeError(
                        (
                            f'Argument "append" for command "env" in step '
                            f'"{step_name}" is not of type "bool"'
                        )
                    )

            elif 'glob' in cmd:
                cmd_glob_keys = [
                    'glob',
                    'pattern',
                    'path',
                    'regex',
                    'mtime',
                    'recursive',
                    'relative',
                    'output',
                ]

                pattern = cmd.get('pattern')
                path = cmd.get('path')
                regex = cmd.get('regex')
                mtime = cmd.get('mtime')
                recursive = cmd.get('recursive')
                relative = cmd.get('relative')
                output = cmd.get('output')

                for key in cmd:
                    if key not in cmd_glob_keys:
                        raise RuntimeError(
                            (
                                f'Unknown "glob" command argument "{key}" in '
                                f'step "{step_name}", expecting '
                                f'"{cmd_glob_keys}"'
                            )
                        )
                # Required arguments
                if not pattern:
                    raise RuntimeError(
                        (
                            f'Command "glob" in step "{step_name}" is '
                            f'missing "pattern" argument'
                        )
                    )
                if not isinstance(pattern, str):
                    raise RuntimeError(
                        (
                            f'Argument "pattern" for command "glob" in step '
                            f'"{step_name}" is not of type "str"'
                        )
                    )
                if not path:
                    raise RuntimeError(
                        (
                            f'Command "glob" in step "{step_name}" '
                            f'is missing "path" argument'
                        )
                    )
                if not isinstance(path, str):
                    raise RuntimeError(
                        (
                            f'Argument "path" for command "glob" in step '
                            f'"{step_name}" is not of type "str"'
                        )
                    )

                # Optional arguments
                if regex:
                    if not isinstance(regex, str):
                        raise RuntimeError(
                            (
                                f'Argument "regex" for command "glob" in step '
                                f'"{step_name}" is not of type "str"'
                            )
                        )
                    try:
                        re.compile(regex)
                    except re.error as e:
                        raise RuntimeError(
                            (
                                f'Argument "regex" for command "glob" in step '
                                f'"{step_name}" is not a valid regular '
                                f'expression: {e}'
                            )
                        )
                if mtime and not isinstance(mtime, bool):
                    raise RuntimeError(
                        (
                            f'Argument "mtime" for command "glob" in step '
                            f'"{step_name}" is not of type "bool"'
                        )
                    )
                if recursive and not isinstance(recursive, bool):
                    raise RuntimeError(
                        (
                            f'Argument "recursive" for command "glob" in '
                            f'step "{step_name}" is not of type "bool"'
                        )
                    )
                if relative and not isinstance(relative, bool):
                    raise RuntimeError(
                        (
                            f'Argument "relative" for command "glob" in step '
                            f'"{step_name}" is not of type "bool"'
                        )
                    )
                if output and not isinstance(output, str):
                    raise RuntimeError(
                        (
                            f'Argument "output" for command "glob" in step '
                            f'"{step_name}" is not of type "str"'
                        )
                    )

            else:
                raise RuntimeError(
                    f'Unknown command "{cmd}" in step "{step_name}"'
                )


def validate_purge(purge: Any) -> None:
    """Validate the loaded purge file. This is done manually to avoid any
    dependencies and to provide more informative error messages.
    """

    if not isinstance(purge, list):
        raise RuntimeError('Purge is not of type "list"')

    regex_keys = ['regex', 'repl']

    for entry in purge:
        if not isinstance(entry, dict):
            raise RuntimeError(f'Purge entry "{entry}" is not of type "dict"')

        if 'regex' in entry:
            for key in entry:
                if key not in regex_keys:
                    raise RuntimeError(
                        (
                            f'Unknown purge key "{key}" in "{entry}", '
                            f'expecting "{regex_keys}"'
                        )
                    )

            regex = entry.get('regex')
            repl = entry.get('repl')

            # Required arguments
            if not isinstance(regex, str):
                raise RuntimeError(
                    (
                        f'Argument "regex" for purge entry "{entry}" is '
                        f'not of type "str"'
                    )
                )
            try:
                re.compile(regex)
            except re.error as e:
                raise RuntimeError(
                    (
                        f'Argument "regex" for purge entry "{entry}" is not '
                        f'a valid regular expression: {e}'
                    )
                )

            if not repl:
                raise RuntimeError(
                    f'Purge entry "{entry}" is missing "repl" argument'
                )
            if not isinstance(repl, str):
                raise RuntimeError(
                    (
                        f'Argument "repl" for purge entry "{entry}" is not '
                        f'of type "str"'
                    )
                )

        else:
            raise RuntimeError(f'Unknown purge entry "{entry}"')


def get_output_path(
    src: Optional[str],
    dst: Optional[str],
    step: Dict,
    recipe: Dict,
    src_root: Optional[str] = None,
) -> Path:
    """Construct the output file path based on source, destination, and recipe
    output.

    Parameters:
    src (Optional[str]): The source file path. This can be None, for example,
                         when used in an exec command.
    dst (Optional[str]): The destination file path or directory. If it ends
                         with a '/' character, it is considered a directory,
                         and the src file name is appended to it. Otherwise
                         it is the file where the output should be saved. This
                         can also be None, in which case the src file name
                         is used as the output file name.
    step (Dict): The step this file belongs to, used to obtain the step'
                 global output directory.
    recipe (Dict): The recipe this file belongs to, used to obtain the recipe's
                   global output directory.
    src_root (Optional[str]): The src file directory, used to determine the
                              relative source file path for constructing the
                              relative destination path. For example, if src
                              is "/dir/dir2/dir3/file.txt" and src_root is
                              "/dir/" and dst is "/output/", the destination
                              file path will be "/output/dir2/dir3/file.txt".

    Returns:
    Path: The constructed output file path.
    """
    dst_path = TMP_DIR_REPORT_PATH
    # recipe global output directory
    recipe_root = recipe.get('output')
    # step global output directory
    step_root = step.get('output')

    if recipe_root:
        dst_path = dst_path / recipe_root

    if step_root:
        dst_path = dst_path / step_root

    if dst:
        dst_path = dst_path / dst
        if dst.endswith('/') and src:
            if src_root:
                src_rel_path = Path(src).relative_to(src_root)
                dst_path = dst_path / src_rel_path
            else:
                dst_path = dst_path / Path(src).name
    elif src:
        dst_path = dst_path / Path(src).name

    return dst_path


def cmd_file(args: Dict, step: Dict, recipe: Dict) -> None:
    """file command"""
    src = args['path']
    dst = args.get('output')

    dst_path = get_output_path(src, dst, step, recipe)

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst_path)
    except FileNotFoundError:
        warn(f'File "{src}" does not exist')
    except Exception:
        err(f'Cannot copy file "{src}"')


def cmd_exec(args: Dict, step: Dict, recipe: Dict) -> None:
    """exec command"""
    cmd = args['cmd']
    stdout = args.get('output')
    stderr = args.get('stderr')
    timeout = args.get('timeout')
    append = args.get('append', False)

    stdout_path = get_output_path(None, stdout, step, recipe)
    stderr_path = get_output_path(None, stderr, step, recipe)

    # If cmd is a string, execute it using the shell.
    if isinstance(cmd, list):
        shell = False
    else:
        shell = True

    try:
        p = run(
            cmd,
            shell=shell,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        warn(f'Exec command "{cmd}" failed')
        return

    if p.returncode:
        warn(f'Exec command "{cmd}" failed with exit code {p.returncode}')
        if p.stderr:
            dbg(f'stderr: "{p.stderr}"')

    if stdout and p.stdout:
        try:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, 'a' if append else 'w') as f:
                f.write(p.stdout)
        except Exception:
            err(f'Cannot write exec command "{cmd}" stdout to "{stdout}"')

    if stderr and p.stderr:
        try:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stderr_path, 'a' if append else 'w') as f:
                f.write(p.stderr)
        except Exception:
            err(f'Cannot write exec command "{cmd}" stderr to "{stderr}"')


def cmd_env(args: Dict, step: Dict, recipe: Dict) -> None:
    """env command"""
    variables = args.get('vars', [])
    regex_str = args.get('regex')
    output = args.get('output')
    append = args.get('append', False)
    regex = re.compile(regex_str) if regex_str else None

    output_path = get_output_path(None, output, step, recipe)
    found_list: List = []
    out_list: List = []

    for var, val in os.environ.items():
        if var in variables:
            found_list.append(var)
            continue

        if not regex:
            continue

        match = regex.match(var)
        if match:
            found_list.append(var)

    for var in found_list:
        val = os.environ[var]
        out_list.append(f'{var}={val}')

    if output:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'a' if append else 'w') as f:
                f.write('\n'.join(out_list))
        except Exception:
            err(f'Cannot write env command output to "{output}"')


def get_latest_modified_file(file_paths: List[Path]) -> Optional[Path]:
    """Return the most recently modified file from the file_paths list"""
    file_path = None
    file_mtime = 0.0

    for file in file_paths:
        mtime = file.stat().st_mtime
        if mtime < file_mtime:
            continue
        file_mtime = mtime
        file_path = file

    return file_path


def cmd_glob(args: Dict, step: Dict, recipe: Dict) -> None:
    """glob command"""
    pattern = args['pattern']
    dir_path = Path(args['path'])
    output = args.get('output')
    mtime = args.get('mtime', False)
    recursive = args.get('recursive', False)
    relative = args.get('relative', False)
    regex_str = args.get('regex')

    try:
        if recursive:
            file_paths = list(dir_path.rglob(pattern))
        else:
            file_paths = list(dir_path.glob(pattern))
    except Exception:
        err(f'Cannot glob "{pattern}" in "{dir_path}"')
        return

    file_paths = [file_path for file_path in file_paths if file_path.is_file()]
    if not file_paths:
        warn(f'No files matching glob "{pattern}" found in "{dir_path}"')
        return

    if regex_str:
        file_paths_match = []
        regex = re.compile(regex_str, flags=re.MULTILINE)
        for file_path in file_paths:
            try:
                with open(file_path, 'r') as f:
                    data = f.read()
                    match = regex.search(data)
                    if match:
                        file_paths_match.append(file_path)
            except Exception:
                err(
                    (
                        f'Failed to search for the regex "{regex_str}" '
                        f'in "{file_path}"'
                    )
                )

        if not file_paths_match:
            warn(
                (
                    f'No files with content matching regex "{regex_str}" '
                    f'found in "{dir_path}"'
                )
            )
            return
        file_paths = file_paths_match

    if mtime:
        last_modified_file = get_latest_modified_file(file_paths)
        if not last_modified_file:
            warn(
                (
                    f'No last modified file found for "{pattern}" '
                    f'found in "{dir_path}"'
                )
            )
            return
        file_paths = [last_modified_file]

    for file_path in file_paths:
        # If the relative flag is enabled, save the file in the output
        # directory while maintaining the same relative path as in the
        # source directory.
        dst_path = get_output_path(
            str(file_path),
            output,
            step,
            recipe,
            str(dir_path) if relative else None,
        )
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if dst_path.is_file():
                # A file already exists in the report directory. Attempt to
                # create a new name by appending numerical suffixes.
                cnt = 1
                while True:
                    new_dst_path = dst_path.with_name(
                        dst_path.name + f'.{cnt}'
                    )
                    if not new_dst_path.exists():
                        dbg(
                            (
                                f'File "{dst_path.name}" for "{file_path}" '
                                f'already exists. Using "{new_dst_path.name}"'
                            )
                        )
                        dst_path = new_dst_path
                        break
                    cnt += 1
            dbg(f'copy "{file_path}" to "{dst_path}"')
            shutil.copy(file_path, dst_path)
        except Exception:
            err(f'Cannot copy glob file "{file_path}"')


def process_recipe(recipe: Dict, args: Namespace) -> None:
    """execute commands for every stage in a recipe"""
    for step in recipe['steps']:
        step_name = step['name']
        step_system = step.get('system')
        step_port = step.get('port', False)

        if step_system and step_system != platform.system():
            dbg(f'Skipping step "{step_name}" for "{step_system}"')
            continue

        if step_port and not args.port:
            dbg(f'Skipping step "{step_name}" requires device serial port')
            continue

        dbg(f'Processing step "{step_name}"')
        oprint(f'* {step_name}')
        for cmd in step['cmds']:
            dbg(f'cmd: "{cmd}"')
            if 'file' in cmd:
                cmd_file(cmd, step, recipe)
            elif 'exec' in cmd:
                cmd_exec(cmd, step, recipe)
            elif 'env' in cmd:
                cmd_env(cmd, step, recipe)
            elif 'glob' in cmd:
                cmd_glob(cmd, step, recipe)
            else:
                err(f'Unknow command "{cmd}" in step "{step_name}"')


def get_purge(args: Namespace) -> list:
    """Load and return a dictionary for purge."""

    purge: list = []

    dbg(f'Purge file: {args.purge}')

    def get_username() -> str:
        username = ''
        try:
            username = getpass.getuser()
        except Exception:
            dbg('Unable to retrieve the username using getpass.getuser')

        return username

    variables = {
        'USERNAME': get_username(),
    }

    try:
        with open(args.purge, 'r') as f:
            data = f.read()
            formatted = Template(data).safe_substitute(**variables)
            purge = yaml.safe_load(formatted)
    except Exception:
        die(f'Cannot load purge file "{args.purge}"')

    return purge


def get_recipes(args: Namespace) -> Dict:
    """Load and return a dictionary of recipes.

    This function retrieves recipes based on the provided command line inputs
    and filters them using specified tags. It can also append additional
    recipes to a set of built-in recipes."""

    builtin_recipe_files: Dict = {}
    recipe_files: List = []
    recipes: Dict = {}

    for recipe_path in BUILTIN_RECIPES_PATH.glob('*.yml'):
        builtin_recipe_files[recipe_path.stem] = str(recipe_path.resolve())
    dbg(f'Builtin recipes "{builtin_recipe_files}"')

    if args.recipe:
        for recipe_file in args.recipe:
            recipe_path = Path(recipe_file).resolve()
            if recipe_path.is_file():
                recipe_files.append(str(recipe_path))
                continue

            if recipe_file in builtin_recipe_files:
                recipe_files.append(builtin_recipe_files[recipe_file])
                continue

            die(f'Cannot find recipe "{recipe_file}"')

        if args.append:
            recipe_files += list(builtin_recipe_files.values())
    else:
        recipe_files += list(builtin_recipe_files.values())

    recipe_files = list(set(recipe_files))
    recipe_files.sort()
    dbg(f'Recipe files to use "{recipe_files}"')

    project_dir = str(Path(args.project_dir).expanduser())
    build_dir = str(Path(args.build_dir).expanduser())

    if (
        not (Path(build_dir) / 'project_description.json').is_file()
        and args.func == cmd_create
    ):
        # Display a warning solely for the create command.
        warn(
            (
                f'Directory "{build_dir}" does not seem to be '
                f'an ESP-IDF project build directory.'
            )
        )
        hint('You can use the "--build-dir" option to set it.')

    # Set up variables that can be utilized in the recipe.
    variables = {
        'PROJECT_DIR': project_dir,
        'BUILD_DIR': build_dir,
        'IDF_PATH': os.environ['IDF_PATH'],
        'REPORT_DIR': str(TMP_DIR_REPORT_PATH),
    }
    if args.port:
        variables['PORT'] = args.port

    dbg(f'Recipe variables: {variables}')
    dbg(f'Project directory: {project_dir}')
    dbg(f'Build directory: {build_dir}')
    dbg(f'System: {platform.system()}')
    dbg(f'Port: {args.port}')

    # Load recipes
    for recipe_file in recipe_files:
        dbg(f'Loading recipe file "{recipe_file}"')
        try:
            with open(recipe_file, 'r') as f:
                data = f.read()
                formatted = Template(data).safe_substitute(**variables)
                recipe = yaml.safe_load(formatted)
                recipes[recipe_file] = recipe
        except Exception:
            die(f'Cannot load diagnostic recipe "{recipe_file}"')

    if args.tag:
        dbg('Filtering recipe file with tags "{}"'.format(', '.join(args.tag)))
        recipes_tagged: Dict = {}
        for recipe_file, recipe in recipes.items():
            recipe_tags = recipe.get('tags')

            if not recipe_tags:
                continue

            for cmdl_tag in args.tag:
                if cmdl_tag in recipe_tags:
                    recipes_tagged[recipe_file] = recipe
                    break

        recipes = recipes_tagged

    if not recipes:
        die('No recipes available')

    return recipes


def cmd_list(args: Namespace) -> int:
    """Display a list of available recipes along with their details"""
    try:
        # list command does not have port option
        args.port = None
        recipes = get_recipes(args)
    except Exception:
        die('Unable to create list of recipe files')

    rv = 0

    for recipe_file, recipe in recipes.items():
        builtin = BUILTIN_RECIPES_PATH == Path(recipe_file).parent

        try:
            validate_recipe(recipe)
            valid = True
        except Exception:
            valid = False
            rv = 1

        oprint(recipe_file)
        oprint('   description: {}'.format(recipe.get('description', '')))
        oprint(
            '   short name: {}'.format(
                Path(recipe_file).stem if builtin else ''
            )
        )
        oprint('   valid: {}'.format(valid))
        oprint('   builtin: {}'.format(builtin))
        oprint('   tags: {}'.format(', '.join(recipe.get('tags', ''))))

    return rv


def cmd_check(args: Namespace) -> int:
    """Verify recipes"""
    try:
        # check command does not have port option
        args.port = None
        recipes = get_recipes(args)
    except Exception:
        die('Unable to create list of recipe files')

    error = False
    for recipe_file, recipe in recipes.items():
        oprint(f'Checking recipe "{recipe_file}" ... ', end='')
        try:
            validate_recipe(recipe)
            oprint('[green]OK', no_prefix=True)
        except Exception:
            oprint('[red] Failed', no_prefix=True)
            err('validation failed')
            error = True

    if error:
        err('Recipes validation failed')
        return 1

    return 0


def cmd_zip(args: Namespace) -> int:
    """Compress the report directory into a zip file"""
    archive_dir_path = Path(args.directory).expanduser()
    archive_path = (
        Path(args.output or args.directory).with_suffix('.zip').expanduser()
    )

    info(f'Creating archive "{archive_path}"')

    if not archive_dir_path.exists() or not archive_dir_path.is_dir():
        die(
            (
                f'The path "{archive_dir_path}" either does not '
                f'exist or is not a directory.'
            )
        )

    if archive_path.exists():
        if not archive_path.is_file():
            die(
                (
                    f'Directory entry "{archive_path}" already exists and is '
                    f'not a regular file. Please use the --output option or '
                    f'remove "{archive_path}" manually.'
                )
            )
        if not args.force:
            die(
                (
                    f'Archive file "{archive_path}" already exists. '
                    f'Please use the --output option or --force option to '
                    f'overwrite the existing "{archive_path}" archive.'
                )
            )
    try:
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as f:
            for file in archive_dir_path.rglob('*'):
                oprint(f'adding: {file}')
                f.write(file, file.relative_to(archive_dir_path.parent))
    except Exception:
        die(f'Cannot create zip archive for "{args.directory}" directory.')

    info(
        (
            f'The archive "{archive_path}" is prepared and can be '
            f'included with your issue report.'
        )
    )

    return 0


def _detect_port() -> Optional[str]:
    port = None
    info('Searching for device serial port ...')
    try:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import esptool
        import serial.tools.list_ports

        ports = list(
            sorted(p.device for p in serial.tools.list_ports.comports())
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            esp = esptool.get_default_connected_device(
                serial_list=ports,
                port=None,
                connect_attempts=4,
                initial_baud=115200,
            )
        dbg(f'Port detection: {buffer.getvalue()}')
        if esp:
            port = esp.serial_port
            esp._port.close()
    except Exception:
        dbg('Port detection failed')

    return port


def cmd_create(args: Namespace) -> int:
    recipes: Dict = {}

    if not args.output:
        output_dir_path = Path('diag-{}'.format(uuid.uuid4())).expanduser()
    else:
        output_dir_path = Path(args.output).expanduser()

    info(f'Creating report in "{output_dir_path}" directory.')

    args.port = args.port or _detect_port()
    port_str = args.port or 'N/A'
    info(f'Serial port: {port_str}')
    if not args.port:
        warn(
            (
                'The device serial port is unavailable. '
                'Target information will not be gathered.'
            )
        )

    try:
        output_dir_path_exists = output_dir_path.exists()
    except Exception:
        die(f'Cannot get report directory "{output_dir_path}" status')

    if output_dir_path_exists:
        if not output_dir_path.is_dir():
            die(
                (
                    f'Directory entry "{output_dir_path}" already exists and '
                    f'is not a directory. Please select a directory that '
                    f'does not exist or remove "{output_dir_path}" '
                    f'manually.'
                )
            )
        if not args.force:
            die(
                (
                    f'Report directory "{output_dir_path}" already exists. '
                    f'Please select a directory that does not exist or use '
                    f'the "-f/--force" option to delete the existing '
                    f'"{output_dir_path}" directory.'
                )
            )
        try:
            dbg(f'Removing existing report "{output_dir_path}" directory')
            shutil.rmtree(output_dir_path)
        except Exception:
            die(f'Cannot remove existing "{output_dir_path}" directory')

    # Get recipe files
    try:
        recipes = get_recipes(args)
    except Exception:
        die('Unable to create list of recipe files')

    # Validate recipes
    try:
        for recipe_file, recipe in recipes.items():
            dbg(f'Validating recipe file "{recipe_file}"')
            validate_recipe(recipe)
    except Exception:
        die(f'File "{recipe_file}" is not a valid diagnostic file')

    # Get purge file
    try:
        purge = get_purge(args)
    except Exception:
        die(f'Unable to create purge for "{args.purge}"')

    # Validate purge file
    try:
        validate_purge(purge)
    except Exception:
        die(f'File "{args.purge}" is not a valid purge file')

    # Cook recipes
    try:
        for recipe_file, recipe in recipes.items():
            desc = recipe.get('description')
            dbg(f'Processing recipe "{desc}" file "{recipe_file}"')
            oprint(f'{desc}')
            process_recipe(recipe, args)
    except Exception:
        die(f'Cannot process diagnostic file "{recipe_file}"')

    dbg('Report is done.')

    try:
        shutil.copy(LOG_FILE_PATH, TMP_DIR_REPORT_PATH / 'diag.log')
    except Exception:
        err('Cannot copy the log file to the report directory')

    try:
        redact_files(TMP_DIR_REPORT_PATH, TMP_DIR_REPORT_REDACTED_PATH, purge)
    except Exception:
        err('The redaction was unsuccessful')

    try:
        shutil.move(str(TMP_DIR_REPORT_REDACTED_PATH), str(output_dir_path))
    except Exception:
        die(
            (
                f'Cannot move diagnostic report directory from '
                f'"{TMP_DIR_REPORT_REDACTED_PATH}" to "{output_dir_path}"'
            )
        )

    info(f'The report has been created in the "{output_dir_path}" directory.')
    hint(
        (
            f'Please make sure to thoroughly check it for any sensitive '
            f'information before sharing and remove files you do not want '
            f'to share. Kindly include any additional files you find '
            f'relevant that were not automatically added. Please archive '
            f'the contents of the final report directory using the command:\n'
            f'"esp-idf-diag zip {output_dir_path}".'
        )
    )

    return 0


def main() -> int:
    def add_common_arguments(parser) -> None:
        parser.add_argument(
            '-d',
            '--debug',
            action='store_true',
            help=('Print debug information, including exception tracebacks.'),
        )
        parser.add_argument(
            '--no-color',
            action='store_true',
            help=('Do not emit ANSI color codes.'),
        )
        parser.add_argument(
            '--force-terminal',
            action='store_true',
            default=None,
            help=(
                'Enable terminal control codes even if out '
                'is not attached to terminal.'
            ),
        )
        parser.add_argument(
            '--log-prefix',
            action='store_true',
            help=(
                'Add a severity character at the beginning of log messages.'
            ),
        )
        parser.add_argument(
            '--no-hints',
            action='store_true',
            help=('Disable hint log messages.'),
        )

    def add_recipe_arguments(parser) -> None:
        parser.add_argument(
            '-r',
            '--recipe',
            metavar='RECIPE',
            action='append',
            help=(
                f'Recipe to use. This option can be specified multiple times. '
                f'By default, all built-in recipes from '
                f'"{BUILTIN_RECIPES_PATH}" directory are used. RECIPE refers '
                f'to the recipe file path or the file name stem for built-in '
                f'recipes.'
            ),
        )
        parser.add_argument(
            '-t',
            '--tag',
            metavar='TAG',
            action='append',
            help=(
                'Consider only recipes containing TAG. This option can be '
                'specified multiple times. By default, all recipes are '
                'used. Use the list command to see recipe TAG information.'
            ),
        )
        parser.add_argument(
            '-a',
            '--append',
            action='store_true',
            help=(
                'Use recipes specified with the -r/--recipe option in '
                'combination with the built-in recipes.'
            ),
        )
        parser.add_argument(
            '-P',
            '--project-dir',
            metavar='PATH',
            default=str(Path.cwd()),
            help=('Project directory.'),
        )
        parser.add_argument(
            '-B',
            '--build-dir',
            metavar='PATH',
            default=str(Path.cwd() / 'build'),
            help=('Build directory.'),
        )

    parser = argparse.ArgumentParser(
        prog='esp-idf-diag', description='ESP-IDF diag tool'
    )
    subparsers = parser.add_subparsers(help='sub-command help')

    create_parser = subparsers.add_parser(
        'create', help=('Create diagnostic report.')
    )
    create_parser.add_argument(
        '-p',
        '--purge',
        metavar='PATH',
        default=str(BUILTIN_PURGE_PATH),
        help=(
            f'Purge file PATH containing a description of what information '
            f'should be redacted from the resulting report. '
            f'Default is "{BUILTIN_PURGE_PATH}"'
        ),
    )
    create_parser.add_argument(
        '-f',
        '--force',
        action='store_true',
        help=(
            'If the report directory already exists, remove it '
            'before creating a new one.'
        ),
    )
    create_parser.add_argument(
        '-o',
        '--output',
        metavar='PATH',
        help=(
            'Diagnostic report directory PATH. '
            'If not specified, the diag-UUID is used as the report directory.'
        ),
    )
    create_parser.add_argument(
        '--port',
        metavar='PORT',
        help=('Serial port device to be used by esptool tools.'),
    )
    create_parser.set_defaults(func=cmd_create)
    add_recipe_arguments(create_parser)
    add_common_arguments(create_parser)

    list_parser = subparsers.add_parser(
        'list', help=('Show information about available recipes.')
    )
    list_parser.set_defaults(func=cmd_list)
    add_recipe_arguments(list_parser)
    add_common_arguments(list_parser)

    check_parser = subparsers.add_parser('check', help=('Validate recipes.'))
    check_parser.set_defaults(func=cmd_check)
    add_recipe_arguments(check_parser)
    add_common_arguments(check_parser)

    zip_parser = subparsers.add_parser(
        'zip', help=('Create zip archive for diagnostic report in PATH.')
    )
    zip_parser.add_argument(
        'directory',
        metavar='PATH',
        help=('Directory PATH for which a zip archive should to be created.'),
    )
    zip_parser.add_argument(
        '-f',
        '--force',
        action='store_true',
        help=(
            'If the zip archive already exists, delete it before creating a '
            'new one.'
        ),
    )
    zip_parser.add_argument(
        '-o',
        '--output',
        metavar='PATH',
        help=(
            'Zip file archive PATH. If not specified, the report directory '
            'used as positional argument to the zip command with a zip '
            'extension is used for the zip file archive.'
        ),
    )

    zip_parser.set_defaults(func=cmd_zip)
    add_common_arguments(zip_parser)

    try:
        args = parser.parse_args()
        if 'func' not in args:
            parser.print_help(sys.stderr)
            sys.exit(1)

        set_logger(args)

        if not os.environ.get('IDF_PATH'):
            die(
                (
                    'IDF_PATH is not set. This command must be '
                    'initiated from within an activated ESP-IDF environment.'
                ),
                show_hint=False,
            )

        rv = args.func(args)
    except KeyboardInterrupt:
        die('Process interrupted by user.', show_hint=False)

    assert isinstance(rv, int)  # help mypy
    return rv


if __name__ == '__main__':
    sys.exit(main())
