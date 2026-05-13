#
#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from antlr4 import InputStream, CommonTokenStream
from antlr4.error.ErrorStrategy import BailErrorStrategy

from hrw4u.hrw4uLexer import hrw4uLexer
from hrw4u.hrw4uParser import hrw4uParser
from hrw4u.errors import Hrw4uSyntaxError, ThrowingErrorListener, ErrorCollector, SymbolResolutionError
from hrw4u.symbols import SymbolResolver
from hrw4u.states import SectionType
from hrw4u.common import RegexPatterns
from hrw4u.validation import Validator
from hrw4u.procedures import resolve_use_path
import hrw4u.types as types
from hrw4u.ast_nodes import (
    HRW4UAST,
    UseDirective,
    VarSection,
    VarDecl,
    ProcedureDecl,
    Section,
    Assignment,
    FunctionCall,
    IfBlock,
    Break,
    Comparison,
    LogicalOp,
    NotOp,
    BoolLiteral,
    IdentCondition,
    LiteralStringValue,
    IdentValue,
    IPValue,
    ParamRef,
    RegexValue,
    ProcParam,
)

_SUBSTITUTE_PATTERN = RegexPatterns.SUBSTITUTE_PATTERN
_PARAM_REF_PATTERN = re.compile(r'\$([a-zA-Z_][a-zA-Z0-9_-]*)')
_regex_validator = Validator.regex_pattern()


@dataclass(slots=True)
class ProcSig:
    qualified_name: str
    params: list[ProcParam]
    body: tuple[Any, ...]
    source_file: str


def validate(
        ast: HRW4UAST,
        filename: str,
        error_collector: ErrorCollector,
        proc_search_paths: list[Path] | None = None,
        debug: bool = False) -> None:
    ctx = _ValidationContext(
        filename=filename,
        error_collector=error_collector,
        proc_search_paths=list(proc_search_paths) if proc_search_paths else [],
        debug=debug)
    _pass1_declarations(ast, ctx)
    _pass2_semantics(ast, ctx)


@dataclass
class _ValidationContext:
    filename: str
    error_collector: ErrorCollector
    proc_search_paths: list[Path]
    debug: bool
    symbol_resolver: SymbolResolver = field(init=False)
    proc_registry: dict[str, ProcSig] = field(default_factory=dict)
    proc_loaded: set[str] = field(default_factory=set)
    proc_call_stack: list[str] = field(default_factory=list)
    proc_bindings: dict[str, str] = field(default_factory=dict)
    current_section: SectionType | None = None

    def __post_init__(self):
        self.symbol_resolver = SymbolResolver(self.debug)

    def error(self, line: int, column: int, message: str, source_line: str = "") -> None:
        self.error_collector.add_error(Hrw4uSyntaxError(self.filename, line, column, message, source_line))

    def error_from_exc(self, line: int, column: int, exc: Exception, source_line: str = "") -> None:
        if isinstance(exc, Hrw4uSyntaxError):
            self.error_collector.add_error(exc)
        else:
            self.error_collector.add_error(Hrw4uSyntaxError(self.filename, line, column, str(exc), source_line))


def _pass1_declarations(ast: HRW4UAST, ctx: _ValidationContext) -> None:
    seen_sections = False

    for node in ast.body:
        if isinstance(node, UseDirective):
            if seen_sections:
                ctx.error(node.line, 0, "'use' directives must appear before any section blocks")
                continue
            _validate_use_directive(node, ctx, load_stack=[])

        elif isinstance(node, ProcedureDecl):
            if seen_sections:
                ctx.error(node.line, 0, "'procedure' declarations must appear before any section blocks")
                continue
            _validate_procedure_decl(node, ctx)

        elif isinstance(node, VarSection):
            if seen_sections:
                ctx.error(node.line, 0, "Variable section must be first in a section")
                continue
            _validate_var_section(node, ctx)

        elif isinstance(node, Section):
            seen_sections = True
            _validate_section_type(node, ctx)


def _validate_use_directive(node: UseDirective, ctx: _ValidationContext, load_stack: list[str]) -> None:
    if not ctx.proc_search_paths:
        ctx.error(node.line, 0, "use directive requires --procedures-path to be set")
        return
    path = resolve_use_path(node.spec, ctx.proc_search_paths)
    if path is None:
        ctx.error(node.line, 0, f"use '{node.spec}': file not found in procedures path")
        return
    try:
        _load_proc_file(path, load_stack, ctx, use_spec=node.spec)
    except (Hrw4uSyntaxError, Exception) as e:
        ctx.error_from_exc(node.line, 0, e)


def _load_proc_file(path: Path, load_stack: list[str], ctx: _ValidationContext, use_spec: str | None = None) -> None:
    abs_path = str(path.resolve())
    if abs_path in ctx.proc_loaded:
        return
    if abs_path in load_stack:
        cycle = ' -> '.join([*load_stack, abs_path])
        raise Hrw4uSyntaxError(str(path), 1, 0, f"circular use dependency: {cycle}", "")

    expected_ns = None
    if use_spec and '::' in use_spec:
        expected_ns = use_spec[:use_spec.rindex('::') + 2]

    text = path.read_text(encoding='utf-8')
    listener = ThrowingErrorListener(filename=str(path))

    lexer = hrw4uLexer(InputStream(text))
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)

    stream = CommonTokenStream(lexer)
    parser = hrw4uParser(stream)
    parser.removeErrorListeners()
    parser.addErrorListener(listener)
    parser.errorHandler = BailErrorStrategy()
    tree = parser.program()

    new_stack = [*load_stack, abs_path]
    found_proc = False

    for item in tree.programItem():
        if item.useDirective():
            spec = item.useDirective().QUALIFIED_IDENT().getText()
            sub_path = resolve_use_path(spec, ctx.proc_search_paths)
            if sub_path is None:
                raise Hrw4uSyntaxError(
                    str(path),
                    item.useDirective().start.line, 0, f"use '{spec}': file not found in procedures path", "")
            _load_proc_file(sub_path, new_stack, ctx, use_spec=spec)
            found_proc = True
        elif item.procedureDecl():
            proc_ctx = item.procedureDecl()
            name = proc_ctx.QUALIFIED_IDENT().getText()
            if '::' not in name:
                raise Hrw4uSyntaxError(
                    str(path), proc_ctx.start.line, proc_ctx.start.column,
                    f"procedure name '{name}' must be qualified (e.g. 'ns::name')", "")
            if expected_ns and not name.startswith(expected_ns):
                raise Hrw4uSyntaxError(
                    str(path), proc_ctx.start.line, proc_ctx.start.column,
                    f"procedure '{name}' does not match namespace '{expected_ns[:-2]}' (expected from 'use {use_spec}')", "")
            if name in ctx.proc_registry:
                existing = ctx.proc_registry[name]
                raise Hrw4uSyntaxError(
                    str(path), proc_ctx.start.line, 0, f"procedure '{name}' already declared in {existing.source_file}", "")

            param_list = proc_ctx.paramList()
            params: list[ProcParam] = []
            if param_list:
                for p in param_list.param():
                    default_val = None
                    if p.value():
                        default_val = _extract_default_value(p.value())
                    params.append(ProcParam(line=proc_ctx.start.line, name=p.IDENT().getText(), default=default_val))

            ctx.proc_registry[name] = ProcSig(qualified_name=name, params=params, body=(), source_file=str(path))
            found_proc = True

    if not found_proc:
        raise Hrw4uSyntaxError(str(path), 1, 0, f"no 'procedure' declarations found in {path.name}", "")

    ctx.proc_loaded.add(abs_path)


def _extract_default_value(val_ctx) -> LiteralStringValue | IdentValue | int | bool:
    text = val_ctx.getText()
    if text.startswith('"') and text.endswith('"'):
        return LiteralStringValue(raw=text)
    if text == 'true':
        return True
    if text == 'false':
        return False
    try:
        return int(text)
    except ValueError:
        return IdentValue(raw=text)


def _validate_procedure_decl(node: ProcedureDecl, ctx: _ValidationContext) -> None:
    if '::' not in node.name:
        ctx.error(node.line, 0, f"procedure name '{node.name}' must be qualified (e.g. 'ns::name')")
        return
    if node.name in ctx.proc_registry:
        existing = ctx.proc_registry[node.name]
        ctx.error(node.line, 0, f"procedure '{node.name}' already declared in {existing.source_file}")
        return

    seen_default = False
    for p in node.params:
        if p.default is None and seen_default:
            ctx.error(
                node.line, 0, f"procedure '{node.name}': required parameter '${p.name}' must not follow an optional parameter")
            return
        if p.default is not None:
            seen_default = True

    ctx.proc_registry[node.name] = ProcSig(
        qualified_name=node.name, params=list(node.params), body=node.body, source_file=ctx.filename)


def _validate_var_section(node: VarSection, ctx: _ValidationContext) -> None:
    for decl in node.declarations:
        _validate_var_decl(decl, node.scope, ctx)


def _validate_var_decl(decl: VarDecl, scope: types.VarScope, ctx: _ValidationContext) -> None:
    if '.' in decl.name or ':' in decl.name:
        ctx.error(decl.line, 0, f"Variable name '{decl.name}' cannot contain '.' or ':' characters")
        return
    try:
        ctx.symbol_resolver.declare_variable(decl.name, decl.type_name, decl.slot, scope)
    except (SymbolResolutionError, Exception) as e:
        ctx.error_from_exc(decl.line, 0, e)


def _validate_section_type(node: Section, ctx: _ValidationContext) -> None:
    try:
        SectionType(node.type)
    except ValueError:
        valid_sections = [s.value for s in SectionType]
        ctx.error(node.line, 0, f"Invalid section name: '{node.type}'. Valid sections: {', '.join(valid_sections)}")


# ---------------------------------------------------------------------------
# Pass 2: Semantic validation (section bodies)
# ---------------------------------------------------------------------------


def _pass2_semantics(ast: HRW4UAST, ctx: _ValidationContext) -> None:
    for node in ast.body:
        if isinstance(node, Section):
            try:
                ctx.current_section = SectionType(node.type)
            except ValueError:
                continue
            _validate_body(node.body, ctx)


def _validate_body(body: tuple, ctx: _ValidationContext) -> None:
    for node in body:
        if isinstance(node, Assignment):
            _validate_assignment(node, ctx)
        elif isinstance(node, FunctionCall):
            _validate_statement_function(node, ctx)
        elif isinstance(node, IfBlock):
            _validate_if_block(node, ctx)
        elif isinstance(node, Break):
            pass


def _validate_assignment(node: Assignment, ctx: _ValidationContext) -> None:
    lhs = _target_to_str(node.target)
    rhs = _value_to_str(node.value, node.line, ctx)
    if rhs is None:
        return

    try:
        if node.operator == "=":
            ctx.symbol_resolver.resolve_assignment(lhs, rhs, ctx.current_section)
        else:
            ctx.symbol_resolver.resolve_add_assignment(lhs, rhs, ctx.current_section)
    except (SymbolResolutionError, Exception) as e:
        ctx.error_from_exc(node.line, 0, e)


def _validate_statement_function(node: FunctionCall, ctx: _ValidationContext) -> None:
    name = node.name
    if name in ctx.proc_registry:
        _validate_proc_call(node, ctx)
        return
    if '::' in name:
        ctx.error(node.line, 0, f"unknown procedure '{name}': not loaded via 'use'")
        return
    args = [_value_to_str(a, node.line, ctx) or "" for a in node.args]
    try:
        ctx.symbol_resolver.resolve_statement_func(name, args, ctx.current_section)
    except (SymbolResolutionError, Exception) as e:
        ctx.error_from_exc(node.line, 0, e)


def _validate_proc_call(node: FunctionCall, ctx: _ValidationContext) -> None:
    sig = ctx.proc_registry[node.name]

    if node.name in ctx.proc_call_stack:
        cycle = ' -> '.join([*ctx.proc_call_stack, node.name])
        ctx.error(node.line, 0, f"circular procedure call: {cycle}")
        return

    required = sum(1 for p in sig.params if p.default is None)
    n_args = len(node.args)
    if not (required <= n_args <= len(sig.params)):
        expected = f"{required}-{len(sig.params)}" if required < len(sig.params) else str(len(sig.params))
        ctx.error(node.line, 0, f"procedure '{sig.qualified_name}': expected {expected} arg(s), got {n_args}")
        return

    saved_stack = ctx.proc_call_stack
    saved_bindings = ctx.proc_bindings
    ctx.proc_call_stack = [*saved_stack, node.name]

    bindings: dict[str, str] = {}
    for i, param in enumerate(sig.params):
        if i < n_args:
            bindings[param.name] = _value_to_str(node.args[i], node.line, ctx) or ""
        elif param.default is not None:
            bindings[param.name] = _value_to_str(param.default, node.line, ctx) or ""

    ctx.proc_bindings = bindings
    _validate_body(sig.body, ctx)
    ctx.proc_call_stack = saved_stack
    ctx.proc_bindings = saved_bindings


def _validate_if_block(node: IfBlock, ctx: _ValidationContext) -> None:
    _validate_condition(node.condition, ctx)
    _validate_body(node.body, ctx)
    for branch in node.elif_branches:
        _validate_condition(branch.condition, ctx)
        _validate_body(branch.body, ctx)
    _validate_body(node.else_body, ctx)


def _validate_condition(cond, ctx: _ValidationContext) -> None:
    if isinstance(cond, Comparison):
        _validate_comparison(cond, ctx)
    elif isinstance(cond, LogicalOp):
        _validate_condition(cond.left, ctx)
        _validate_condition(cond.right, ctx)
    elif isinstance(cond, NotOp):
        _validate_condition(cond.operand, ctx)
    elif isinstance(cond, BoolLiteral):
        pass
    elif isinstance(cond, IdentCondition):
        _validate_ident_condition(cond, ctx)
    elif isinstance(cond, FunctionCall):
        _validate_condition_function(cond, ctx)


def _validate_comparison(node: Comparison, ctx: _ValidationContext) -> None:
    if isinstance(node.left, IdentValue):
        _resolve_identifier(node.left.raw, node.line, ctx)
    elif isinstance(node.left, FunctionCall):
        _validate_condition_function(node.left, ctx)

    if isinstance(node.right, RegexValue):
        try:
            _regex_validator(node.right.raw)
        except Exception as e:
            ctx.error_from_exc(node.line, 0, e)
    elif isinstance(node.right, LiteralStringValue):
        _validate_string_interpolation(node.right.raw, node.line, ctx)
    elif isinstance(node.right, tuple):
        for item in node.right:
            if isinstance(item, LiteralStringValue):
                _validate_string_interpolation(item.raw, node.line, ctx)


def _validate_ident_condition(node: IdentCondition, ctx: _ValidationContext) -> None:
    _resolve_identifier(node.name, node.line, ctx)


def _resolve_identifier(name: str, line: int, ctx: _ValidationContext) -> None:
    if not name:
        return

    if ctx.symbol_resolver.symbol_for(name):
        return

    if '.' not in name and ':' not in name:
        error = SymbolResolutionError("identifier", f"Undefined variable: '{name}'. Variables must be declared in a VARS section.")
        suggestions = ctx.symbol_resolver.get_variable_suggestions(name, ctx.current_section)
        if suggestions:
            error.add_symbol_suggestion(suggestions)
        ctx.error_from_exc(line, 0, error)
        return

    try:
        ctx.symbol_resolver.resolve_condition(name, ctx.current_section)
    except SymbolResolutionError as e:
        ctx.error_from_exc(line, 0, e)


def _validate_condition_function(node: FunctionCall, ctx: _ValidationContext) -> None:
    args = [_value_to_str(a, node.line, ctx) or "" for a in node.args]
    try:
        ctx.symbol_resolver.resolve_function(node.name, args, strip_quotes=True)
    except (SymbolResolutionError, Exception) as e:
        ctx.error_from_exc(node.line, 0, e)


def _validate_string_interpolation(s: str, line: int, ctx: _ValidationContext) -> None:
    if '{' not in s:
        return

    inner = s
    if ctx.proc_bindings:
        inner = _PARAM_REF_PATTERN.sub(lambda m: ctx.proc_bindings.get(m.group(1), m.group(0)), inner)

    for m in _SUBSTITUTE_PATTERN.finditer(inner):
        try:
            if m.group("escaped"):
                continue
            if m.group("func"):
                func_name = m.group("func").strip()
                arg_str = m.group("args").strip()
                args = _parse_function_args(arg_str) if arg_str else []
                ctx.symbol_resolver.resolve_function(func_name, args, strip_quotes=False)
            elif m.group("var"):
                var_name = m.group("var").strip()
                ctx.symbol_resolver.resolve_condition(var_name, ctx.current_section)
        except Exception as e:
            ctx.error(line, 0, f"symbol error in {{}}: {e}")


def _validate_param_ref(node: ParamRef, line: int, ctx: _ValidationContext) -> None:
    if node.raw not in ctx.proc_bindings:
        ctx.error(line, 0, f"'${node.raw}' used outside procedure context")


def _target_to_str(target) -> str:
    if target.namespace:
        return f"{target.namespace}.{target.field}"
    return target.field


def _value_to_str(value, line: int, ctx: _ValidationContext) -> str | None:
    if isinstance(value, LiteralStringValue):
        s = value.raw
        if '{' in s:
            _validate_string_interpolation(s, line, ctx)
            if ctx.proc_bindings:
                s = _PARAM_REF_PATTERN.sub(lambda m: ctx.proc_bindings.get(m.group(1), m.group(0)), s)
        return f'"{s}"'
    elif isinstance(value, IdentValue):
        return value.raw
    elif isinstance(value, IPValue):
        return value.raw
    elif isinstance(value, ParamRef):
        _validate_param_ref(value, line, ctx)
        if value.raw in ctx.proc_bindings:
            return ctx.proc_bindings[value.raw]
        return None
    elif isinstance(value, bool):  # bool before int: bool is a subclass of int
        return "true" if value else "false"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, tuple):
        parts = []
        for item in value:
            r = _value_to_str(item, line, ctx)
            if r:
                parts.append(r)
        return "{" + ",".join(parts) + "}"
    return str(value) if value is not None else None


def _parse_function_args(arg_str: str) -> list[str]:
    if not arg_str.strip():
        return []

    args = []
    current_arg: list[str] = []
    paren_depth = 0
    in_quotes = False
    quote_char = None
    i = 0

    while i < len(arg_str):
        char = arg_str[i]

        if not in_quotes:
            if char in ('"', "'"):
                in_quotes = True
                quote_char = char
                current_arg.append(char)
            elif char == '(':
                paren_depth += 1
                current_arg.append(char)
            elif char == ')':
                paren_depth -= 1
                current_arg.append(char)
            elif char == ',' and paren_depth == 0:
                args.append(''.join(current_arg).strip())
                current_arg = []
            else:
                current_arg.append(char)
        else:
            current_arg.append(char)
            if char == quote_char:
                if i == 0 or arg_str[i - 1] != '\\':
                    in_quotes = False
                    quote_char = None
        i += 1

    if current_arg:
        args.append(''.join(current_arg).strip())

    return args
