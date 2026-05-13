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

from dataclasses import dataclass
from pathlib import Path

from antlr4 import InputStream, CommonTokenStream
from antlr4.error.ErrorStrategy import BailErrorStrategy

from hrw4u.hrw4uLexer import hrw4uLexer
from hrw4u.hrw4uParser import hrw4uParser
from hrw4u.errors import Hrw4uSyntaxError, ThrowingErrorListener, ErrorCollector
from hrw4u.symbols import SymbolResolver
from hrw4u.states import SectionType
from hrw4u.procedures import resolve_use_path
from hrw4u.ast_visitor import ASTVisitor
import hrw4u.types as types
import hrw4u.ast_nodes as nodes

_VAR_SECTION_SCOPE: dict[nodes.VarSectionKind, types.VarScope] = {
    nodes.VarSectionKind.TXN: types.VarScope.TXN,
    nodes.VarSectionKind.SESSION: types.VarScope.SESSION,
}


@dataclass(frozen=True, slots=True)
class ProcSig:
    qualified_name: str
    params: tuple[nodes.ProcParam, ...]
    body: tuple[nodes.BodyNode, ...]
    source_file: str


@dataclass(frozen=True)
class ResolvedAST:
    ast: nodes.HRW4UAST
    proc_registry: dict[str, ProcSig]
    symbol_resolver: SymbolResolver


def resolve(
        ast: nodes.HRW4UAST,
        filename: str,
        error_collector: ErrorCollector,
        proc_search_paths: list[Path] | None = None,
        debug: bool = False) -> ResolvedAST:
    search_paths: list[Path] = list(proc_search_paths) if proc_search_paths else []
    proc_registry: dict[str, ProcSig] = {}
    proc_loaded: set[str] = set()
    symbol_resolver = SymbolResolver(debug)
    seen_sections = False

    for node in ast.body:
        if isinstance(node, nodes.UseDirective):
            if seen_sections:
                error_collector.add_error(
                    Hrw4uSyntaxError(filename, node.line, 0, "'use' directives must appear before any section blocks", ""))
                continue
            _resolve_use_directive(node, filename, search_paths, proc_registry, proc_loaded, error_collector)

        elif isinstance(node, nodes.ProcedureDecl):
            if seen_sections:
                error_collector.add_error(
                    Hrw4uSyntaxError(filename, node.line, 0,
                                     "'procedure' declarations must appear before any section blocks", ""))
                continue
            _register_procedure_decl(node, filename, proc_registry, error_collector)

        elif isinstance(node, nodes.VarSection):
            if seen_sections:
                error_collector.add_error(
                    Hrw4uSyntaxError(filename, node.line, 0, "Variable section must be first in a section", ""))
                continue
            scope = _VAR_SECTION_SCOPE[node.scope]
            for decl in node.declarations:
                _register_var_decl(decl, scope, filename, symbol_resolver, error_collector)

        elif isinstance(node, nodes.Section):
            seen_sections = True
            try:
                SectionType(node.type)
            except ValueError:
                valid_sections = [s.value for s in SectionType]
                error_collector.add_error(
                    Hrw4uSyntaxError(
                        filename, node.line, 0,
                        f"Invalid section name: '{node.type}'. Valid sections: {', '.join(valid_sections)}", ""))

    return ResolvedAST(ast=ast, proc_registry=proc_registry, symbol_resolver=symbol_resolver)


def _resolve_use_directive(
        node: nodes.UseDirective,
        filename: str,
        search_paths: list[Path],
        proc_registry: dict[str, ProcSig],
        proc_loaded: set[str],
        error_collector: ErrorCollector) -> None:
    if not search_paths:
        error_collector.add_error(
            Hrw4uSyntaxError(filename, node.line, 0, "use directive requires --procedures-path to be set", ""))
        return
    path = resolve_use_path(node.spec, search_paths)
    if path is None:
        error_collector.add_error(
            Hrw4uSyntaxError(filename, node.line, 0, f"use '{node.spec}': file not found in procedures path", ""))
        return
    try:
        _load_and_resolve_proc_file(path, [], search_paths, proc_registry, proc_loaded, use_spec=node.spec)
    except Hrw4uSyntaxError as e:
        error_collector.add_error(e)
    except Exception as e:
        error_collector.add_error(Hrw4uSyntaxError(filename, node.line, 0, str(e), ""))


def _register_procedure_decl(
        node: nodes.ProcedureDecl,
        filename: str,
        proc_registry: dict[str, ProcSig],
        error_collector: ErrorCollector) -> None:
    if '::' not in node.name:
        error_collector.add_error(
            Hrw4uSyntaxError(filename, node.line, 0,
                             f"procedure name '{node.name}' must be qualified (e.g. 'ns::name')", ""))
        return
    if node.name in proc_registry:
        existing = proc_registry[node.name]
        error_collector.add_error(
            Hrw4uSyntaxError(filename, node.line, 0,
                             f"procedure '{node.name}' already declared in {existing.source_file}", ""))
        return

    seen_default = False
    for p in node.params:
        if p.default is None and seen_default:
            error_collector.add_error(
                Hrw4uSyntaxError(
                    filename, node.line, 0,
                    f"procedure '{node.name}': required parameter '${p.name}' must not follow an optional parameter", ""))
            return
        if p.default is not None:
            seen_default = True

    proc_registry[node.name] = ProcSig(
        qualified_name=node.name,
        params=node.params,
        body=node.body,
        source_file=filename)


def _register_var_decl(
        decl: nodes.VarDecl,
        scope: types.VarScope,
        filename: str,
        symbol_resolver: SymbolResolver,
        error_collector: ErrorCollector) -> None:
    if '.' in decl.name or ':' in decl.name:
        error_collector.add_error(
            Hrw4uSyntaxError(filename, decl.line, 0,
                             f"Variable name '{decl.name}' cannot contain '.' or ':' characters", ""))
        return
    try:
        symbol_resolver.declare_variable(decl.name, decl.type_name, decl.slot, scope)
    except Exception as e:
        if isinstance(e, Hrw4uSyntaxError):
            error_collector.add_error(e)
        else:
            error_collector.add_error(Hrw4uSyntaxError(filename, decl.line, 0, str(e), ""))


def _load_and_resolve_proc_file(
        path: Path,
        load_stack: list[str],
        search_paths: list[Path],
        proc_registry: dict[str, ProcSig],
        proc_loaded: set[str],
        use_spec: str | None = None) -> None:
    abs_path = str(path.resolve())
    if abs_path in proc_loaded:
        return
    if abs_path in load_stack:
        cycle = ' -> '.join([*load_stack, abs_path])
        raise Hrw4uSyntaxError(str(path), 1, 0, f"circular use dependency: {cycle}", "")

    expected_ns: str | None = None
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

    file_ast: nodes.HRW4UAST = ASTVisitor().visit(tree)

    new_stack = [*load_stack, abs_path]
    found_proc = False

    for item in file_ast.body:
        if isinstance(item, nodes.UseDirective):
            sub_path = resolve_use_path(item.spec, search_paths)
            if sub_path is None:
                raise Hrw4uSyntaxError(
                    str(path), item.line, 0, f"use '{item.spec}': file not found in procedures path", "")
            _load_and_resolve_proc_file(sub_path, new_stack, search_paths, proc_registry, proc_loaded, use_spec=item.spec)
            found_proc = True

        elif isinstance(item, nodes.ProcedureDecl):
            name = item.name
            if '::' not in name:
                raise Hrw4uSyntaxError(
                    str(path), item.line, 0, f"procedure name '{name}' must be qualified (e.g. 'ns::name')", "")
            if expected_ns and not name.startswith(expected_ns):
                raise Hrw4uSyntaxError(
                    str(path), item.line, 0,
                    f"procedure '{name}' does not match namespace '{expected_ns[:-2]}' (expected from 'use {use_spec}')", "")
            if name in proc_registry:
                existing = proc_registry[name]
                raise Hrw4uSyntaxError(
                    str(path), item.line, 0, f"procedure '{name}' already declared in {existing.source_file}", "")

            proc_registry[name] = ProcSig(
                qualified_name=name,
                params=item.params,
                body=item.body,
                source_file=str(path))
            found_proc = True

    if not found_proc:
        raise Hrw4uSyntaxError(str(path), 1, 0, f"no 'procedure' declarations found in {path.name}", "")

    proc_loaded.add(abs_path)
