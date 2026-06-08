# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
"""Logging for esp-idf-diag.

This module provides ``DiagLog``, an :class:`esp_pylib.logger.EspLogBase`
implementation that keeps esp-idf-diag's tool-specific logging behaviour while
plugging into the shared esp-pylib logging interface:

* every message is also written to a ``diag.log`` capture file that becomes
  part of the diagnostic report (no color, with a severity-character prefix);
* the message of the exception currently being handled (and, at debug level,
  its traceback) is appended automatically to log messages;
* informational and debug messages go to *stderr* while report data
  (``oprint``) and hints go to *stdout*;
* fatal errors exit with status code 128.

The logger is installed as the global esp-pylib singleton via
:func:`setup_logger`, so the rest of the tool simply uses
``from esp_pylib.logger import log`` and calls ``log.info(...)`` etc.
"""

import atexit
import shutil
import sys
import textwrap
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional

from esp_pylib.logger import EspLog, EspLogBase
from rich.console import Console

# A temporary directory is used to store the report. Once it is completely
# generated, it is moved to its final location.
TMP_DIR = TemporaryDirectory()
TMP_DIR_PATH = Path(TMP_DIR.name)
TMP_DIR_REPORT_PATH = TMP_DIR_PATH / 'report'
TMP_DIR_REPORT_REDACTED_PATH = TMP_DIR_PATH / 'redacted'

# The full debug log will be stored in the report directory alongside other
# collected files.
LOG_FILE_PATH = TMP_DIR_PATH / 'diag.log'


def cleanup() -> None:
    """Perform cleanup operations in case of unexpected termination."""
    try:
        inst = EspLog.instance
        if isinstance(inst, DiagLog) and inst._log_file:
            inst._log_file.close()
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


class DiagLog(EspLogBase):
    """esp-idf-diag logger implementing the esp-pylib ``EspLogBase`` interface.

    Output methods accept variadic positional arguments (rendered together like
    :func:`print`) to match the ``EspLogBase`` interface; esp-idf-diag always
    calls them with a single message string.
    """

    # Severity levels. stderr: FATAL..DEBUG, stdout: STDOUT and HINT. The
    # numeric ordering drives routing in ``_log`` exactly as before.
    FATAL = 1
    ERROR = 2
    WARNING = 3
    INFO = 4
    DEBUG = 5
    STDOUT = 6
    HINT = 7

    _COLOR = {
        FATAL: '[red1]',
        ERROR: '[bold #CC3311]',
        WARNING: '[bold yellow]',
        INFO: '[#0077BB]',
        DEBUG: '[dim]',
        HINT: '[#00A0A0]',
        STDOUT: '',
    }

    _PREFIX = {
        FATAL: 'F',
        ERROR: 'E',
        WARNING: 'W',
        INFO: 'I',
        DEBUG: 'D',
        STDOUT: 'O',
        HINT: 'H',
    }

    def __init__(
        self,
        *,
        debug: bool = False,
        no_color: bool = False,
        force_terminal: Optional[bool] = None,
        log_prefix: bool = False,
        no_hints: bool = False,
    ) -> None:
        # debug raises the stderr verbosity to DEBUG; --no-hints keeps the
        # default INFO level and only suppresses hint messages.
        self._level = self.DEBUG if debug else self.INFO
        self._log_prefix = log_prefix
        self._hints = not no_hints

        self._stdout = Console(
            no_color=no_color,
            force_terminal=force_terminal,
            soft_wrap=True,
        )
        self._stderr = Console(
            stderr=True,
            no_color=no_color,
            force_terminal=force_terminal,
            soft_wrap=True,
        )

        self._log_file: Optional[Any] = None
        try:
            LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(LOG_FILE_PATH, 'w')
        except Exception:
            self.err(
                (
                    f'Cannot open log file "{LOG_FILE_PATH}". '
                    f'Log file will not be generated.'
                )
            )

    @staticmethod
    def _join(args: tuple) -> str:
        return ' '.join(str(a) for a in args)

    def _log(
        self,
        level: int,
        msg: str,
        prefix: str,
        no_prefix: bool = False,
        **kwargs: Any,
    ) -> None:
        """Output a message to the console and the report log file.

        Messages are written to the log file regardless of the level, with a
        severity prefix and without color formatting. Console output is routed
        to stdout for ``level >= STDOUT`` and to stderr for messages within the
        configured verbosity. ``no_prefix`` suppresses the console severity
        prefix (used for line continuation) and is never forwarded to rich.
        """
        if self._log_prefix and not no_prefix:
            console_prefix = f'{prefix} '
        else:
            console_prefix = ''

        if self._log_file:
            try:
                log_msg = textwrap.indent(msg, prefix=f'{prefix} ')
                self._log_file.write(log_msg + '\n')
                self._log_file.flush()
            except Exception:
                self._log_file.close()
                self._log_file = None
                self.err(
                    (
                        f'Cannot write to log file "{LOG_FILE_PATH}". '
                        f'Logging to file is turned off.'
                    )
                )

        indented = textwrap.indent(msg, prefix=console_prefix)
        out = f'{self._COLOR[level]}{indented}'

        if level >= self.STDOUT:
            self._stdout.print(out, **kwargs)
            sys.stdout.flush()
        elif level <= self._level:
            self._stderr.print(out, **kwargs)
            sys.stderr.flush()

    def _log_with_exception(self, level: int, msg: str, prefix: str) -> None:
        ex_msg = exception_msg()
        if ex_msg:
            msg += f': {ex_msg}'
        self._log(level, msg, prefix)
        ex_tb = exception_tb()
        if ex_tb:
            self._log(self.DEBUG, ex_tb, 'D')

    # --- esp-idf-diag severities (auto-append the active exception) ---
    def fatal(self, *args: Any) -> None:
        self._log_with_exception(self.FATAL, 'fatal: ' + self._join(args), 'F')

    def err(self, *args: Any, suggestion: Optional[str] = None) -> None:
        self._log_with_exception(self.ERROR, 'error: ' + self._join(args), 'E')

    def warn(self, *args: Any, suggestion: Optional[str] = None) -> None:
        self._log_with_exception(
            self.WARNING, 'warning: ' + self._join(args), 'W'
        )

    def info(self, *args: Any) -> None:
        self._log_with_exception(self.INFO, self._join(args), 'I')

    def dbg(self, *args: Any) -> None:
        self._log_with_exception(self.DEBUG, self._join(args), 'D')

    def debug(self, *args: Any) -> None:
        # EspLogBase interface name; esp-idf-diag uses ``dbg``.
        self.dbg(*args)

    def oprint(self, *args: Any, **kwargs: Any) -> None:
        """Print report/output data to stdout (no severity, no exception)."""
        self._log(self.STDOUT, self._join(args), 'O', **kwargs)

    def print(self, *args: Any, **kwargs: Any) -> None:
        # EspLogBase interface name; esp-idf-diag uses ``oprint``.
        self.oprint(*args, **kwargs)

    def hint(self, *args: Any) -> None:
        if self._hints:
            self._log(self.HINT, self._join(args), 'H')

    def note(self, *args: Any) -> None:
        # EspLogBase requires ``note``; esp-idf-diag has no separate notion of
        # it, so route to plain stdout output.
        self.oprint(*args)

    def die(
        self,
        *args: Any,
        exit_code: int = 128,
        show_hint: bool = True,
        suggestion: Optional[str] = None,
    ) -> Any:
        """Irrecoverable fatal error."""
        self.fatal(*args)
        # Avoid calling fatal, as it may print the exception again if present.
        self._log(self.FATAL, 'ESP-IDF diagnostic command failed.', 'F')
        if show_hint and self._level != self.DEBUG:
            # If the stderr log level is not set to debug, suggest it.
            self.hint(
                'Using the "-d/--debug" option may provide more information.'
            )
        sys.exit(exit_code)

    def set_verbosity(self, mode: Any) -> None:
        # esp-idf-diag manages verbosity through its own CLI flags.
        pass

    def progress_bar(
        self,
        cur_iter: int,
        total_iters: int,
        prefix: str = '',
        suffix: str = '',
        bar_length: int = 30,
    ) -> None:
        # esp-idf-diag does not render progress bars.
        pass


def setup_logger(args: Any) -> None:
    """Install the esp-idf-diag logger as the esp-pylib singleton."""
    EspLog.set_logger(
        DiagLog(
            debug=args['debug'],
            no_color=args['no_color'],
            force_terminal=args['force_terminal'],
            log_prefix=args['log_prefix'],
            no_hints=args['no_hints'],
        )
    )
