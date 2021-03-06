# This file is part of the pretty-traceback project
# https://gitlab.com/mbarkhau/pretty-traceback
#
# Copyright (c) 2020 Manuel Barkhau (mbarkhau@gmail.com) - MIT License
# SPDX-License-Identifier: MIT

import os
import re
import sys
import types
import typing as typ
import traceback as tb
import subprocess as sp

import colorama

import pretty_traceback.common as com


def _get_terminal_width() -> int:
    try:
        columns = int(os.environ['COLUMNS'])
        # lines   = int(os.environ['LINES'  ])
        return columns
    except (KeyError, ValueError):
        pass

    if hasattr(os, 'get_terminal_size'):
        try:
            size = os.get_terminal_size(0)
            return size.columns
        except OSError:
            pass

    try:
        size_output = sp.check_output(['stty', 'size']).decode()
        _, columns = [int(val) for val in size_output.strip().split()]
        return columns
    except sp.CalledProcessError:
        pass
    except IOError:
        pass

    return 0


FMT_MODULE : str = colorama.Fore.CYAN    + colorama.Style.BRIGHT + "{0}" + colorama.Style.RESET_ALL
FMT_CALL   : str = colorama.Fore.YELLOW  + colorama.Style.BRIGHT + "{0}" + colorama.Style.RESET_ALL
FMT_LINENO : str = colorama.Fore.MAGENTA + colorama.Style.BRIGHT + "{0}" + colorama.Style.RESET_ALL
FMT_CONTEXT: str = "{0}"
FMT_ERROR  : str = colorama.Fore.RED + colorama.Style.BRIGHT + "{0}" + colorama.Style.RESET_ALL


class Row(typ.NamedTuple):

    alias       : str
    short_module: str
    full_module : str
    call        : str
    lineno      : str
    context     : str


class PaddedRow(typ.NamedTuple):

    alias  : str
    module : str
    call   : str
    lineno : str
    context: str


Alias  = str
Prefix = str

AliasPrefixes = typ.List[typ.Tuple[Alias, Prefix]]


class Context(typ.NamedTuple):

    rows   : typ.List[Row]
    aliases: AliasPrefixes

    term_width  : int
    is_wide_mode: bool

    # for paddings
    max_short_module_len: int
    max_full_module_len : int

    max_lineno_len : int
    max_call_len   : int
    max_context_len: int


def _iter_entry_paths(entries: com.Entries) -> typ.Iterable[str]:
    for entry in entries:
        module_abspath   = os.path.abspath(entry.module)
        is_valid_abspath = module_abspath != entry.module and os.path.exists(module_abspath)
        if is_valid_abspath:
            yield module_abspath
        else:
            yield entry.module


# used by unit tests to override paths
TEST_PATHS: typ.List[str] = []

PWD = os.getcwd()


def _py_paths() -> typ.List[str]:
    if TEST_PATHS:
        return TEST_PATHS

    # NOTE (mb 2020-08-16): We don't know which path entry
    #   was used to import a module. I guess we could figure it
    #   out, but the preference here is to make the shortest
    #   path possible.

    paths = list(sys.path)
    paths.sort(key=len)

    # pwd should be last (has preference)
    if PWD in paths:
        paths.remove(PWD)
    paths.append(PWD)
    return paths


def _init_aliases(entry_paths: typ.List[str]) -> AliasPrefixes:
    _uniq_entry_paths = set(entry_paths)
    paths             = _py_paths()

    alias_index = 0
    aliases: AliasPrefixes = []
    for path in reversed(paths):
        is_path_used = False
        for epath in _uniq_entry_paths:
            if epath.startswith(path):
                is_path_used = True
                _uniq_entry_paths.remove(epath)
                break

        if not is_path_used:
            continue

        # TODO (mb 2020-08-16): more better paths
        if path.endswith("site-packages"):
            alias = "<sitepkg>"
        elif path.endswith("dist-packages"):
            alias = "<distpkg>"
        elif path == PWD:
            alias = "<pwd>"
        elif re.search(r"lib/python\d.\d+$", path):
            alias = "<py>"
        elif re.search(r"lib/Python\d.\d+\\lib$", path):
            alias = "<py>"
        else:
            alias = f"<p{alias_index}>"
            alias_index += 1

        aliases.append((alias, path))

    return aliases


def _iter_entry_rows(
    aliases: AliasPrefixes, entry_paths: typ.List[str], entries: com.Entries
) -> typ.Iterable[Row]:
    for module, entry in zip(entry_paths, entries):
        _module, call, lineno, context = entry
        # NOTE (mb 2020-08-18): _module may not be an absolute path,
        #   but it's not shortened using an alias yet either.
        assert module.endswith(_module)

        used_alias   = ""
        module_full  = module
        module_short = module
        for alias, path in aliases:
            if module.startswith(path):
                used_alias   = alias
                module_short = module[len(path) :]
                break

        yield Row(used_alias, module_short, module_full, call, lineno, context)


def _init_entry_context(entries: com.Entries) -> Context:
    entry_paths = list(_iter_entry_paths(entries))
    aliases     = _init_aliases(entry_paths)

    rows = list(_iter_entry_rows(aliases, entry_paths, entries))

    if rows:
        max_short_module_len = max(len(row.alias      ) + len(row.short_module) for row in rows)
        max_full_module_len  = max(len(row.full_module) for row in rows)

        max_lineno_len  = max(len(row.lineno ) for row in rows)
        max_call_len    = max(len(row.call   ) for row in rows)
        max_context_len = max(len(row.context) for row in rows)
    else:
        max_short_module_len = 0
        max_full_module_len  = 0

        max_lineno_len  = 0
        max_call_len    = 0
        max_context_len = 0

    term_width    = _get_terminal_width()
    max_total_len = max_full_module_len + max_lineno_len + max_call_len + max_context_len
    is_wide_mode  = max_total_len < term_width

    return Context(
        rows,
        aliases,
        term_width,
        is_wide_mode,
        max_short_module_len,
        max_full_module_len,
        max_lineno_len,
        max_call_len,
        max_context_len,
    )


def _padded_rows(ctx: Context) -> typ.Iterable[PaddedRow]:
    # Expand padding from left to right as much as can fit the terminal.
    # This will mutate rows (updating strings with added padding)

    for row in ctx.rows:
        alias, module_short, module_full, call, lineno, context = row
        len_module = len(alias) + len(module_short)
        line_len   = 2 + len_module + 2 + len(call) + 2 + len(lineno) + 2 + len(context)

        padding_available = ctx.term_width - line_len

        if ctx.is_wide_mode:
            alias         = ""
            padded_module = module_full.ljust(ctx.max_full_module_len)
        elif padding_available > 0:
            padding_desired  = ctx.max_short_module_len - len_module
            padding_consumed = min(padding_available, padding_desired)
            padded_module    = module_short + " " * padding_consumed
            padding_available -= padding_consumed
        else:
            padded_module = module_short

        if ctx.is_wide_mode:
            padded_call = call.ljust(ctx.max_call_len)
        elif padding_available > 0:
            padding_desired  = ctx.max_call_len - len(call)
            padding_consumed = min(padding_available, padding_desired)
            padded_call      = call.ljust(len(call) + padding_consumed)
            padding_available -= padding_consumed
        else:
            padded_call = call

        if ctx.is_wide_mode:
            padded_lineno = lineno.rjust(ctx.max_lineno_len)
        elif padding_available > 0:
            padding_desired  = ctx.max_lineno_len - len(lineno)
            padding_consumed = min(padding_available, padding_desired)
            padded_lineno    = lineno.rjust(len(lineno) + padding_consumed)
        else:
            padded_lineno = lineno

        yield PaddedRow(alias, padded_module, padded_call, padded_lineno, context)


def _aliases_to_lines(ctx: Context, color: bool = False) -> typ.Iterable[str]:
    fmt_module = FMT_MODULE if color else "{0}"
    if ctx.aliases:
        alias_padding = max(len(alias) for alias, _ in ctx.aliases)
        for alias, path in ctx.aliases:
            yield "    " + alias.ljust(alias_padding) + ": " + fmt_module.format(path)


def _rows_to_lines(rows: typ.List[PaddedRow], color: bool = False) -> typ.Iterable[str]:
    # apply colors and additional separators/ spacing
    fmt_module  = FMT_MODULE if color else "{0}"
    fmt_call    = FMT_CALL if color else "{0}"
    fmt_lineno  = FMT_LINENO if color else "{0}"
    fmt_context = FMT_CONTEXT if color else "{0}"

    for alias, module, call, lineno, context in rows:
        parts = (
            "    ",
            alias,
            fmt_module.format(module),
            "  ",
            fmt_call.format(call),
            "  ",
            fmt_lineno.format(lineno),
            ": ",
            fmt_context.format(context),
        )
        yield "".join(parts)


def _traceback_to_entries(traceback: types.TracebackType) -> typ.Iterable[com.Entry]:
    summary = tb.extract_tb(traceback)
    for entry in summary:
        module  = entry[0]
        call    = entry[2]
        lineno  = str(entry[1])
        context = entry[3]
        yield com.Entry(module, call, lineno, context)


def format_traceback(traceback: com.Traceback, color: bool = False) -> str:
    fmt_error  = FMT_ERROR if color else "{0}"
    error_line = fmt_error.format(traceback.exc_name)
    if traceback.exc_msg:
        error_line += ": " + traceback.exc_msg

    ctx         = _init_entry_context(traceback.entries)
    padded_rows = list(_padded_rows(ctx))

    lines = []
    if not ctx.is_wide_mode:
        lines.append(com.ALIASES_HEAD)
        lines.extend(_aliases_to_lines(ctx, color))

    lines.append(com.TRACEBACK_HEAD)
    lines.extend(_rows_to_lines(padded_rows, color))
    lines.append(error_line)
    return os.linesep.join(lines) + os.linesep


def format_tracebacks(tracebacks: typ.List[com.Traceback], color: bool = False) -> str:
    traceback_strs: typ.List[str] = []

    for tb_tup in tracebacks:
        if tb_tup.is_caused:
            # traceback_strs.append("vvv caused by ^^^ - ")
            traceback_strs.append(com.CAUSE_HEAD + os.linesep)
        elif tb_tup.is_context:
            # traceback_strs.append("vvv happend after ^^^ - ")
            traceback_strs.append(com.CONTEXT_HEAD + os.linesep)

        traceback_str = format_traceback(tb_tup, color)
        traceback_strs.append(traceback_str)

    return os.linesep.join(traceback_strs).strip()


def exc_to_traceback_str(
    exc_value: BaseException, traceback: types.TracebackType, color: bool = False,
) -> str:
    # NOTE (mb 2020-08-13): wrt. cause vs context see
    #   https://www.python.org/dev/peps/pep-3134/#enhanced-reporting
    #   https://stackoverflow.com/questions/11235932/
    tracebacks: typ.List[com.Traceback] = []

    cur_exc_value: BaseException       = exc_value
    cur_traceback: types.TracebackType = traceback

    while cur_exc_value:
        next_cause   = getattr(cur_exc_value, '__cause__'  , None)
        next_context = getattr(cur_exc_value, '__context__', None)

        tb_tup = com.Traceback(
            exc_name=type(cur_exc_value).__name__,
            exc_msg=str(cur_exc_value),
            entries=list(_traceback_to_entries(cur_traceback)),
            is_caused=bool(next_cause),
            is_context=bool(next_context),
        )

        tracebacks.append(tb_tup)

        if next_cause:
            cur_exc_value = next_cause
            cur_traceback = getattr(next_cause, '__traceback__', None)
        elif next_context:
            cur_exc_value = next_context
            cur_traceback = getattr(next_context, '__traceback__', None)
        else:
            break

    tracebacks = list(reversed(tracebacks))

    return format_tracebacks(tracebacks, color)
