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
    LiteralStringValue,
    IdentValue,
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
                node.line, 0,
                f"procedure '{node.name}': required parameter '${p.name}' must not follow an optional parameter")
            return
        if p.default is not None:
            seen_default = True

    ctx.proc_registry[node.name] = ProcSig(
        qualified_name=node.name, params=list(node.params), body=node.body, source_file=ctx.filename)


def _validate_var_section(node: VarSection, ctx: _ValidationContext) -> None:
    scope = types.VarScope.SESSION if node.scope == "SESSION_VARS" else types.VarScope.TXN
    for decl in node.declarations:
        _validate_var_decl(decl, scope, ctx)


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
