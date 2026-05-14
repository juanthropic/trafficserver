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

from hrw4u.errors import Hrw4uSyntaxError, ErrorCollector
from hrw4u.symbols import SymbolResolver
from hrw4u.states import SectionType
from hrw4u.procedures import resolve_use_path
from hrw4u.ast_visitor import parse_to_ast
import hrw4u.types as types
import hrw4u.ast_nodes as nodes

_VAR_SECTION_SCOPE: dict[nodes.VarSectionKind, types.VarScope] = {
    nodes.VarSectionKind.TXN: types.VarScope.TXN,
    nodes.VarSectionKind.SESSION: types.VarScope.SESSION,
}


@dataclass(frozen=True, slots=True)
class ProcSig:
    """Resolved signature of a declared procedure.

    `body` contains the full AST of the procedure body. External procedures
    loaded via `use` directives have real body nodes, not an empty tuple.
    `source_file` is the absolute path for external procedures; the input
    filename for inline declarations.
    """

    qualified_name: str
    params: tuple[nodes.ProcParam, ...]
    body: tuple[nodes.BodyNode, ...]
    source_file: str


@dataclass(frozen=True)
class ResolvedAST:
    """Output of the resolution pass: the original AST plus its resolved state.

    `frozen=True` prevents field reassignment, but `proc_registry` and
    `symbol_resolver` are mutable objects. Callers must not mutate them.

    Always returned by `resolve()` even when errors were collected; check
    `error_collector.has_errors()` before passing this to `validate()`.
    """

    ast: nodes.HRW4UAST
    proc_registry: dict[str, ProcSig]
    symbol_resolver: SymbolResolver


def resolve(
        ast: nodes.HRW4UAST,
        filename: str,
        error_collector: ErrorCollector,
        proc_search_paths: list[Path] | None = None,
        debug: bool = False) -> ResolvedAST:
    """Walk `ast` and produce a fully-resolved state for the validator.

    `ast` must have been produced by `ASTVisitor` from a parsed program.
    `error_collector` is shared with the caller and may already hold errors;
    this function appends to it rather than raising.

    `proc_search_paths` must be provided when the program contains `use`
    directives; without it, every `use` produces an error.

    Resolution proceeds past errors to collect as many as possible. The
    returned `ResolvedAST` always holds the original `ast` reference unchanged.
    """
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
        _load_and_resolve_proc_file(
            path, [], search_paths, proc_registry, proc_loaded, error_collector, use_spec=node.spec)
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
    except Hrw4uSyntaxError as e:
        error_collector.add_error(e)
    except Exception as e:
        error_collector.add_error(Hrw4uSyntaxError(filename, decl.line, 0, str(e), ""))


def _load_and_resolve_proc_file(
        path: Path,
        load_stack: list[str],
        search_paths: list[Path],
        proc_registry: dict[str, ProcSig],
        proc_loaded: set[str],
        error_collector: ErrorCollector,
        use_spec: str | None = None) -> None:
    """Parse an external .hrw4u file and register its procedures.

    Structural errors (unqualified names, namespace mismatches, duplicate
    procs, missing nested `use` targets, files with no proc content) are
    appended to `error_collector` and the loop continues, so one bad
    procedure does not hide the next. Lex/parse errors from the file are
    also collected via `parse_to_ast`; when parsing fails, the file is
    marked loaded and no procedures from it are registered, since a
    partially-recovered tree would not faithfully represent the source.

    `load_stack` holds the absolute paths currently being loaded on the
    recursion chain; it is used only for cycle detection. `proc_loaded`
    is the set of paths already fully processed and is consulted to skip
    diamond re-loads. A detected cycle records an error and returns
    without recursing to avoid infinite loops; the file is still marked
    loaded at the end so retries do not re-emit the same errors.

    `use_spec` is the spec string from the `use` directive that triggered
    this load (or None at the top level). If it contains `::`, every
    procedure in the file must live under that namespace; otherwise the
    namespace check is skipped.

    Mutates `proc_registry`, `proc_loaded`, and `error_collector` in place.
    """
    abs_path = str(path.resolve())
    if abs_path in proc_loaded:
        return
    if abs_path in load_stack:
        cycle = ' -> '.join([*load_stack, abs_path])
        error_collector.add_error(Hrw4uSyntaxError(str(path), 1, 0, f"circular use dependency: {cycle}", ""))
        return

    expected_ns: str | None = None
    if use_spec and '::' in use_spec:
        expected_ns = use_spec[:use_spec.rindex('::') + 2]

    text = path.read_text(encoding='utf-8')
    file_ast = parse_to_ast(text, str(path), error_collector)
    if file_ast is None:
        proc_loaded.add(abs_path)
        return

    new_stack = [*load_stack, abs_path]
    saw_proc_content = False

    for item in file_ast.body:
        if isinstance(item, nodes.UseDirective):
            saw_proc_content = True
            sub_path = resolve_use_path(item.spec, search_paths)
            if sub_path is None:
                error_collector.add_error(
                    Hrw4uSyntaxError(
                        str(path), item.line, 0, f"use '{item.spec}': file not found in procedures path", ""))
                continue
            _load_and_resolve_proc_file(
                sub_path, new_stack, search_paths, proc_registry, proc_loaded, error_collector, use_spec=item.spec)

        elif isinstance(item, nodes.ProcedureDecl):
            saw_proc_content = True
            name = item.name
            if '::' not in name:
                error_collector.add_error(
                    Hrw4uSyntaxError(
                        str(path), item.line, 0, f"procedure name '{name}' must be qualified (e.g. 'ns::name')", ""))
                continue
            if expected_ns and not name.startswith(expected_ns):
                error_collector.add_error(
                    Hrw4uSyntaxError(
                        str(path), item.line, 0,
                        f"procedure '{name}' does not match namespace '{expected_ns[:-2]}' (expected from 'use {use_spec}')",
                        ""))
                continue
            if name in proc_registry:
                existing = proc_registry[name]
                error_collector.add_error(
                    Hrw4uSyntaxError(
                        str(path), item.line, 0, f"procedure '{name}' already declared in {existing.source_file}", ""))
                continue

            proc_registry[name] = ProcSig(
                qualified_name=name,
                params=item.params,
                body=item.body,
                source_file=str(path))

    if not saw_proc_content:
        error_collector.add_error(
            Hrw4uSyntaxError(str(path), 1, 0, f"no 'procedure' declarations found in {path.name}", ""))

    proc_loaded.add(abs_path)
