# SPDX-FileCopyrightText: 2024-2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
import difflib
import getpass
import json
import os
import platform
import re
import shutil
import sys
import uuid
import zipfile
from pathlib import Path
from string import Template
from subprocess import run
from typing import Any, Dict, List, Optional

import rich_click as click
import yaml
from esp_pylib.cli_types import SerialPortType
from esp_pylib.constants import ESP_ROM_BAUD
from esp_pylib.logger import log

from esp_idf_diag.log import (
    LOG_FILE_PATH,
    TMP_DIR_REPORT_PATH,
    TMP_DIR_REPORT_REDACTED_PATH,
    setup_logger,
)

# Fixed path for the built-in recipes
BUILTIN_RECIPES_PATH = (Path(__file__).parent / 'data' / 'recipes').resolve()

# Fixed path for the built-in purge
BUILTIN_PURGE_PATH = (
    Path(__file__).parent / 'data' / 'purge' / 'purge.yml'
).resolve()


def diff_dirs(dir1: Path, dir2: Path) -> None:
    """Show differences in files between two directories."""
    dir1_root_path = Path(dir1).resolve()
    dir2_root_path = Path(dir2).resolve()
    log.dbg(f'diff "{dir1_root_path}" to "{dir2_root_path}"')
    for dir1_file_path in dir1_root_path.rglob('*'):
        if not dir1_file_path.is_file():
            continue
        dir2_file_path = dir2_root_path / dir1_file_path.relative_to(
            dir1_root_path
        )

        with open(dir1_file_path, 'r') as f1, open(dir2_file_path, 'r') as f2:
            try:
                f1_lines = f1.readlines()
            except UnicodeDecodeError:
                log.dbg(f'skipping redaction diff for {dir1_file_path}')
            else:
                diff = difflib.unified_diff(
                    f1_lines,
                    f2.readlines(),
                    fromfile=str(
                        dir1_file_path.relative_to(dir1_root_path.parent)
                    ),
                    tofile=str(
                        dir2_file_path.relative_to(dir2_root_path.parent)
                    ),
                    n=0,
                )
                for line in diff:
                    log.dbg(line.strip())


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
    log.dbg(f'redacting files in "{dir1_root_path}" into "{dir2_root_path}"')
    for dir1_file_path in dir1_root_path.rglob('*'):
        if not dir1_file_path.is_file():
            continue
        dir2_file_path = dir2_root_path / dir1_file_path.relative_to(
            dir1_root_path
        )
        dir2_file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dir1_file_path, 'r') as f1, open(dir2_file_path, 'w') as f2:
            try:
                data = f1.read()
            except UnicodeDecodeError:
                shutil.copy(dir1_file_path, dir2_file_path)
                log.warn(f'skipping redaction for {dir1_file_path}')
            else:
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
    src = str(Path(args['path']).expanduser())
    dst = args.get('output')

    dst_path = get_output_path(src, dst, step, recipe)

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst_path)
    except FileNotFoundError:
        log.warn(f'File "{src}" does not exist')
    except Exception:
        log.err(f'Cannot copy file "{src}"')


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
        log.warn(f'Exec command "{cmd}" failed')
        return

    if p.returncode:
        log.warn(f'Exec command "{cmd}" failed with exit code {p.returncode}')
        if p.stderr:
            log.dbg(f'stderr: "{p.stderr}"')

    if stdout and p.stdout:
        try:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, 'a' if append else 'w') as f:
                f.write(p.stdout)
        except Exception:
            log.err(f'Cannot write exec command "{cmd}" stdout to "{stdout}"')

    if stderr and p.stderr:
        try:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stderr_path, 'a' if append else 'w') as f:
                f.write(p.stderr)
        except Exception:
            log.err(f'Cannot write exec command "{cmd}" stderr to "{stderr}"')


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
            log.err(f'Cannot write env command output to "{output}"')


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
    dir_path = Path(args['path']).expanduser()
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
        log.err(f'Cannot glob "{pattern}" in "{dir_path}"')
        return

    file_paths = [file_path for file_path in file_paths if file_path.is_file()]
    if not file_paths:
        log.warn(f'No files matching glob "{pattern}" found in "{dir_path}"')
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
                log.err(
                    (
                        f'Failed to search for the regex "{regex_str}" '
                        f'in "{file_path}"'
                    )
                )

        if not file_paths_match:
            log.warn(
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
            log.warn(
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
                        log.dbg(
                            (
                                f'File "{dst_path.name}" for "{file_path}" '
                                f'already exists. Using "{new_dst_path.name}"'
                            )
                        )
                        dst_path = new_dst_path
                        break
                    cnt += 1
            log.dbg(f'copy "{file_path}" to "{dst_path}"')
            shutil.copy(file_path, dst_path)
        except Exception:
            log.err(f'Cannot copy glob file "{file_path}"')


def process_recipe(recipe: Dict, args: Dict) -> None:
    """execute commands for every stage in a recipe"""
    for step in recipe['steps']:
        step_name = step['name']
        step_system = step.get('system')
        step_port = step.get('port', False)

        if step_system and step_system != platform.system():
            log.dbg(f'Skipping step "{step_name}" for "{step_system}"')
            continue

        if step_port and not args.get('port'):
            log.dbg(f'Skipping step "{step_name}" requires device serial port')
            continue

        log.dbg(f'Processing step "{step_name}"')
        log.oprint(f'* {step_name}')
        for cmd in step['cmds']:
            log.dbg(f'cmd: "{cmd}"')
            if 'file' in cmd:
                cmd_file(cmd, step, recipe)
            elif 'exec' in cmd:
                cmd_exec(cmd, step, recipe)
            elif 'env' in cmd:
                cmd_env(cmd, step, recipe)
            elif 'glob' in cmd:
                cmd_glob(cmd, step, recipe)
            else:
                log.err(f'Unknow command "{cmd}" in step "{step_name}"')


def get_purge(args: Dict) -> list:
    """Load and return a dictionary for purge."""

    purge: list = []
    purge_path = args['purge']

    log.dbg(f'Purge file: {purge_path}')

    def get_username() -> str:
        username = ''
        try:
            username = getpass.getuser()
        except Exception:
            log.dbg('Unable to retrieve the username using getpass.getuser')

        return username

    variables = {
        'USERNAME': get_username(),
    }

    try:
        with open(purge_path, 'r') as f:
            data = f.read()
            formatted = Template(data).safe_substitute(**variables)
            purge = yaml.safe_load(formatted)
    except Exception:
        log.die(f'Cannot load purge file "{purge_path}"')

    return purge


def get_recipes(args: Dict) -> Dict:
    """Load and return a dictionary of recipes.

    This function retrieves recipes based on the provided command line inputs
    and filters them using specified tags. It can also append additional
    recipes to a set of built-in recipes."""

    builtin_recipe_files: Dict = {}
    recipe_files: List = []
    recipes: Dict = {}

    for recipe_path in BUILTIN_RECIPES_PATH.glob('*.yml'):
        builtin_recipe_files[recipe_path.stem] = str(recipe_path.resolve())
    log.dbg(f'Builtin recipes "{builtin_recipe_files}"')

    if args['recipe']:
        for recipe_file in args['recipe']:
            recipe_path = Path(recipe_file).resolve()
            if recipe_path.is_file():
                recipe_files.append(str(recipe_path))
                continue

            if recipe_file in builtin_recipe_files:
                recipe_files.append(builtin_recipe_files[recipe_file])
                continue

            log.die(f'Cannot find recipe "{recipe_file}"')

        if args['append']:
            recipe_files += list(builtin_recipe_files.values())
    else:
        recipe_files += list(builtin_recipe_files.values())

    recipe_files = list(set(recipe_files))
    recipe_files.sort()
    log.dbg(f'Recipe files to use "{recipe_files}"')

    project_dir = str(Path(args['project_dir']).expanduser())
    build_dir = str(Path(args['build_dir']).expanduser())
    sdkconfig_file = os.path.join(project_dir, 'sdkconfig')

    if (
        not (Path(build_dir) / 'project_description.json').is_file()
        and args['func'] == cmd_create
    ):
        # Display a warning solely for the create command.
        log.warn(
            (
                f'Directory "{build_dir}" does not seem to be '
                f'an ESP-IDF project build directory.'
            )
        )
        log.hint('You can use the "--build-dir" option to set it.')
    else:
        try:
            with open(Path(build_dir) / 'project_description.json') as f:
                project_description = json.load(f)
                sdkconfig_file = project_description.get(
                    'config_file', sdkconfig_file
                )
        except Exception:
            log.warn(
                (
                    'Obtaining SDKCONFIG file from project description failed.'
                    f' Using default SDKCONFIG file path: "{sdkconfig_file}".'
                )
            )

    # Set up variables that can be utilized in the recipe.
    variables = {
        'PROJECT_DIR': project_dir,
        'BUILD_DIR': build_dir,
        'IDF_PATH': os.environ['IDF_PATH'],
        'REPORT_DIR': str(TMP_DIR_REPORT_PATH),
        'SDKCONFIG_FILE': sdkconfig_file,
    }
    port = args.get('port')
    if port:
        variables['PORT'] = port

    log.dbg(f'Recipe variables: {variables}')
    log.dbg(f'Project directory: {project_dir}')
    log.dbg(f'Build directory: {build_dir}')
    log.dbg(f'System: {platform.system()}')
    log.dbg(f'Port: {port}')

    # Load recipes
    for recipe_file in recipe_files:
        log.dbg(f'Loading recipe file "{recipe_file}"')
        try:
            with open(recipe_file, 'r') as f:
                data = f.read()
                formatted = Template(data).safe_substitute(**variables)
                recipe = yaml.safe_load(formatted)
                recipes[recipe_file] = recipe
        except Exception:
            log.die(f'Cannot load diagnostic recipe "{recipe_file}"')

    if args['tag']:
        tags = ', '.join(args['tag'])
        log.dbg(f'Filtering recipe file with tags "{tags}"')
        recipes_tagged: Dict = {}
        for recipe_file, recipe in recipes.items():
            recipe_tags = recipe.get('tags')

            if not recipe_tags:
                continue

            for cmdl_tag in args['tag']:
                if cmdl_tag in recipe_tags:
                    recipes_tagged[recipe_file] = recipe
                    break

        recipes = recipes_tagged

    if not recipes:
        log.die('No recipes available')

    return recipes


def cmd_list(args: Dict) -> int:
    """Display a list of available recipes along with their details"""
    try:
        recipes = get_recipes(args)
    except Exception:
        log.die('Unable to create list of recipe files')

    rv = 0

    for recipe_file, recipe in recipes.items():
        builtin = BUILTIN_RECIPES_PATH == Path(recipe_file).parent

        try:
            validate_recipe(recipe)
            valid = True
        except Exception:
            valid = False
            rv = 1

        log.oprint(recipe_file)
        log.oprint('   description: {}'.format(recipe.get('description', '')))
        log.oprint(
            '   short name: {}'.format(
                Path(recipe_file).stem if builtin else ''
            )
        )
        log.oprint('   valid: {}'.format(valid))
        log.oprint('   builtin: {}'.format(builtin))
        log.oprint('   tags: {}'.format(', '.join(recipe.get('tags', ''))))

    return rv


def cmd_check(args: Dict) -> int:
    """Verify recipes"""
    try:
        recipes = get_recipes(args)
    except Exception:
        log.die('Unable to create list of recipe files')

    error = False
    for recipe_file, recipe in recipes.items():
        log.oprint(f'Checking recipe "{recipe_file}" ... ', end='')
        try:
            validate_recipe(recipe)
            log.oprint('[green]OK', no_prefix=True)
        except Exception:
            log.oprint('[red] Failed', no_prefix=True)
            log.err('validation failed')
            error = True

    if error:
        log.err('Recipes validation failed')
        return 1

    return 0


def cmd_zip(args: Dict) -> int:
    """Compress the report directory into a zip file"""
    directory = args['directory']
    output = args['output']
    archive_dir_path = Path(directory).expanduser()
    archive_path = Path(output or directory).with_suffix('.zip').expanduser()

    log.info(f'Creating archive "{archive_path}"')

    if not archive_dir_path.exists() or not archive_dir_path.is_dir():
        log.die(
            (
                f'The path "{archive_dir_path}" either does not '
                f'exist or is not a directory.'
            )
        )

    if archive_path.exists():
        if not archive_path.is_file():
            log.die(
                (
                    f'Directory entry "{archive_path}" already exists and is '
                    f'not a regular file. Please use the --output option or '
                    f'remove "{archive_path}" manually.'
                )
            )
        if not args['force']:
            log.die(
                (
                    f'Archive file "{archive_path}" already exists. '
                    f'Please use the --output option or --force option to '
                    f'overwrite the existing "{archive_path}" archive.'
                )
            )
    try:
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as f:
            for file in archive_dir_path.rglob('*'):
                log.oprint(f'adding: {file}')
                f.write(file, file.relative_to(archive_dir_path.parent))
    except Exception:
        log.die(f'Cannot create zip archive for "{directory}" directory.')

    log.info(
        (
            f'The archive "{archive_path}" is prepared and can be '
            f'included with your issue report.'
        )
    )

    return 0


def _detect_port() -> Optional[str]:
    port = None
    log.info('Searching for device serial port ...')
    try:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import esptool
        from esp_pylib.serial_ports import get_port_names

        ports = get_port_names()
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            esp = esptool.get_default_connected_device(
                serial_list=ports,
                port=None,
                connect_attempts=4,
                initial_baud=ESP_ROM_BAUD,
            )
        log.dbg(f'Port detection: {buffer.getvalue()}')
        if esp:
            port = esp.serial_port
            esp._port.close()
    except Exception:
        log.dbg('Port detection failed')

    return port


def cmd_create(args: Dict) -> int:
    recipes: Dict = {}

    if not args['output']:
        output_dir_path = Path('diag-{}'.format(uuid.uuid4())).expanduser()
    else:
        output_dir_path = Path(args['output']).expanduser()

    log.info(f'Creating report in "{output_dir_path}" directory.')

    args['port'] = args['port'] or _detect_port()
    port_str = args['port'] or 'N/A'
    log.info(f'Serial port: {port_str}')
    if not args['port']:
        log.warn(
            (
                'The device serial port is unavailable. '
                'Target information will not be gathered.'
            )
        )

    try:
        output_dir_path_exists = output_dir_path.exists()
    except Exception:
        log.die(f'Cannot get report directory "{output_dir_path}" status')

    if output_dir_path_exists:
        if not output_dir_path.is_dir():
            log.die(
                (
                    f'Directory entry "{output_dir_path}" already exists and '
                    f'is not a directory. Please select a directory that '
                    f'does not exist or remove "{output_dir_path}" '
                    f'manually.'
                )
            )
        if not args['force']:
            log.die(
                (
                    f'Report directory "{output_dir_path}" already exists. '
                    f'Please select a directory that does not exist or use '
                    f'the "-f/--force" option to delete the existing '
                    f'"{output_dir_path}" directory.'
                )
            )
        try:
            log.dbg(f'Removing existing report "{output_dir_path}" directory')
            shutil.rmtree(output_dir_path)
        except Exception:
            log.die(f'Cannot remove existing "{output_dir_path}" directory')

    # Get recipe files
    try:
        recipes = get_recipes(args)
    except Exception:
        log.die('Unable to create list of recipe files')

    # Validate recipes
    try:
        for recipe_file, recipe in recipes.items():
            log.dbg(f'Validating recipe file "{recipe_file}"')
            validate_recipe(recipe)
    except Exception:
        log.die(f'File "{recipe_file}" is not a valid diagnostic file')

    # Get purge file
    purge_path = args['purge']
    try:
        purge = get_purge(args)
    except Exception:
        log.die(f'Unable to create purge for "{purge_path}"')

    # Validate purge file
    try:
        validate_purge(purge)
    except Exception:
        log.die(f'File "{purge_path}" is not a valid purge file')

    # Cook recipes
    try:
        for recipe_file, recipe in recipes.items():
            desc = recipe.get('description')
            log.dbg(f'Processing recipe "{desc}" file "{recipe_file}"')
            log.oprint(f'{desc}')
            process_recipe(recipe, args)
    except Exception:
        log.die(f'Cannot process diagnostic file "{recipe_file}"')

    log.dbg('Report is done.')

    try:
        TMP_DIR_REPORT_PATH.mkdir(parents=True, exist_ok=True)
        shutil.copy(LOG_FILE_PATH, TMP_DIR_REPORT_PATH / 'diag.log')
    except Exception:
        log.err('Cannot copy the log file to the report directory')

    try:
        redact_files(TMP_DIR_REPORT_PATH, TMP_DIR_REPORT_REDACTED_PATH, purge)
    except Exception:
        log.err('The redaction was unsuccessful')

    try:
        shutil.move(str(TMP_DIR_REPORT_REDACTED_PATH), str(output_dir_path))
    except Exception:
        log.die(
            (
                f'Cannot move diagnostic report directory from '
                f'"{TMP_DIR_REPORT_REDACTED_PATH}" to "{output_dir_path}"'
            )
        )

    log.info(
        f'The report has been created in the "{output_dir_path}" directory.'
    )
    log.hint(
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


def common_options(func):
    """Attach the options shared by every subcommand."""
    func = click.option(
        '--no-hints',
        is_flag=True,
        help='Disable hint log messages.',
    )(func)
    func = click.option(
        '--log-prefix',
        is_flag=True,
        help='Add a severity character at the beginning of log messages.',
    )(func)
    func = click.option(
        '--force-terminal',
        is_flag=True,
        default=None,
        help=(
            'Enable terminal control codes even if out '
            'is not attached to terminal.'
        ),
    )(func)
    func = click.option(
        '--no-color',
        is_flag=True,
        help='Do not emit ANSI color codes.',
    )(func)
    func = click.option(
        '-d',
        '--debug',
        is_flag=True,
        help='Print debug information, including exception tracebacks.',
    )(func)
    return func


def _to_list(ctx, param, value):
    """Click callback: normalize a ``multiple=True`` tuple to a list.

    Keeps the value type identical to the old argparse ``append`` action that
    the ``cmd_*`` functions were written against.
    """
    return list(value)


def recipe_options(func):
    """Attach the recipe-selection options (create/list/check)."""
    func = click.option(
        '-B',
        '--build-dir',
        metavar='PATH',
        default=lambda: str(Path.cwd() / 'build'),
        help='Build directory.',
    )(func)
    func = click.option(
        '-P',
        '--project-dir',
        metavar='PATH',
        default=lambda: str(Path.cwd()),
        help='Project directory.',
    )(func)
    func = click.option(
        '-a',
        '--append',
        is_flag=True,
        help=(
            'Use recipes specified with the -r/--recipe option in '
            'combination with the built-in recipes.'
        ),
    )(func)
    func = click.option(
        '-t',
        '--tag',
        metavar='TAG',
        multiple=True,
        callback=_to_list,
        help=(
            'Consider only recipes containing TAG. This option can be '
            'specified multiple times. By default, all recipes are '
            'used. Use the list command to see recipe TAG information.'
        ),
    )(func)
    func = click.option(
        '-r',
        '--recipe',
        metavar='RECIPE',
        multiple=True,
        callback=_to_list,
        help=(
            f'Recipe to use. This option can be specified multiple times. '
            f'By default, all built-in recipes from '
            f'"{BUILTIN_RECIPES_PATH}" directory are used. RECIPE refers '
            f'to the recipe file path or the file name stem for built-in '
            f'recipes.'
        ),
    )(func)
    return func


def _run(func, **kwargs):
    """Build the options dict and dispatch to a command function.

    The ``cmd_*`` functions receive the collected rich_click options as a dict
    carrying a ``func`` discriminator. (``--recipe`` / ``--tag`` already
    normalize their ``multiple=True`` tuples to lists via the ``_to_list``
    callback.)
    """
    args = {'func': func, **kwargs}
    setup_logger(args)

    if not os.environ.get('IDF_PATH'):
        log.die(
            (
                'IDF_PATH is not set. This command must be '
                'initiated from within an activated ESP-IDF environment.'
            ),
            show_hint=False,
        )

    return func(args)


@click.group(
    invoke_without_command=True,
    context_settings=dict(help_option_names=['-h', '--help']),
)
@click.pass_context
def cli(ctx):
    """ESP-IDF diag tool"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help(), err=True)
        ctx.exit(1)


@cli.command('create')
@click.option(
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
@click.option(
    '-f',
    '--force',
    is_flag=True,
    help=(
        'If the report directory already exists, remove it '
        'before creating a new one.'
    ),
)
@click.option(
    '-o',
    '--output',
    metavar='PATH',
    help=(
        'Diagnostic report directory PATH. '
        'If not specified, the diag-UUID is used as the report directory.'
    ),
)
@click.option(
    '--port',
    type=SerialPortType(),
    metavar='PORT',
    help='Serial port device to be used by esptool tools.',
)
@recipe_options
@common_options
def create(**kwargs):
    """Create diagnostic report."""
    return _run(cmd_create, **kwargs)


@cli.command('list')
@recipe_options
@common_options
def list_(**kwargs):
    """Show information about available recipes."""
    return _run(cmd_list, **kwargs)


@cli.command('check')
@recipe_options
@common_options
def check(**kwargs):
    """Validate recipes."""
    return _run(cmd_check, **kwargs)


@cli.command('zip')
@click.argument('directory', metavar='PATH')
@click.option(
    '-f',
    '--force',
    is_flag=True,
    help=(
        'If the zip archive already exists, delete it before creating a '
        'new one.'
    ),
)
@click.option(
    '-o',
    '--output',
    metavar='PATH',
    help=(
        'Zip file archive PATH. If not specified, the report directory '
        'used as positional argument to the zip command with a zip '
        'extension is used for the zip file archive.'
    ),
)
@common_options
def zip_(**kwargs):
    """Create zip archive for diagnostic report in PATH."""
    return _run(cmd_zip, **kwargs)


def main() -> int:
    try:
        rv = cli.main(prog_name='esp-idf-diag', standalone_mode=False)
    except (KeyboardInterrupt, click.Abort):
        log.die('Process interrupted by user.', show_hint=False)
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)

    assert isinstance(rv, int)  # help mypy
    return rv


if __name__ == '__main__':
    sys.exit(main())
