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

from pathlib import Path

import pytest
import utils
from hrw4u.ast_visitor import ASTVisitor
from hrw4u.ast_validator import validate
from hrw4u.errors import ErrorCollector


def _validate_text(text: str, filename: str = "<test>", proc_search_paths: list[Path] | None = None) -> ErrorCollector:
    _, tree = utils.parse_input_text(text)
    ast = ASTVisitor().visit(tree)
    ec = ErrorCollector()
    validate(ast, filename, ec, proc_search_paths=proc_search_paths)
    return ec


def _procs_dir(input_file: Path) -> Path:
    return input_file.parent / 'procs'


class TestFailingVars:

    @pytest.mark.parametrize("input_file", utils.collect_failing_inputs("vars"))
    def test_catches_error(self, input_file: Path) -> None:
        text = input_file.read_text()
        error_file = input_file.with_name(input_file.name.replace(".input.txt", ".error.txt"))
        assert error_file.exists(), f"Missing error file: {error_file}"
        expected_error = error_file.read_text().strip()

        ec = _validate_text(text, filename=str(input_file))
        assert ec.has_errors(), f"Expected errors for {input_file.name} but got none"

        actual_summary = ec.get_error_summary()
        first_line = expected_error.split('\n')[0]

        # Extract the error message from structured format
        import re
        match = re.match(r'^.+:\d+:\d+:\s*error:\s*(.+)$', first_line)
        if match:
            expected_message = match.group(1).strip()
            assert expected_message in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_message!r}\n"
                f"Actual summary:\n{actual_summary}")
        else:
            assert expected_error in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_error!r}\n"
                f"Actual summary:\n{actual_summary}")


class TestFailingHooks:

    @pytest.mark.parametrize("input_file", utils.collect_failing_inputs("hooks"))
    def test_catches_error(self, input_file: Path) -> None:
        text = input_file.read_text()
        error_file = input_file.with_name(input_file.name.replace(".input.txt", ".error.txt"))
        assert error_file.exists(), f"Missing error file: {error_file}"
        expected_error = error_file.read_text().strip()

        ec = _validate_text(text, filename=str(input_file))
        assert ec.has_errors(), f"Expected errors for {input_file.name} but got none"

        actual_summary = ec.get_error_summary()
        first_line = expected_error.split('\n')[0]

        import re
        match = re.match(r'^.+:\d+:\d+:\s*error:\s*(.+)$', first_line)
        if match:
            expected_message = match.group(1).strip()
            assert expected_message in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_message!r}\n"
                f"Actual summary:\n{actual_summary}")
        else:
            assert expected_error in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_error!r}\n"
                f"Actual summary:\n{actual_summary}")


class TestFailingConds:

    @pytest.mark.parametrize("input_file", utils.collect_failing_inputs("conds"))
    def test_catches_error(self, input_file: Path) -> None:
        text = input_file.read_text()
        error_file = input_file.with_name(input_file.name.replace(".input.txt", ".error.txt"))
        assert error_file.exists(), f"Missing error file: {error_file}"
        expected_error = error_file.read_text().strip()

        ec = _validate_text(text, filename=str(input_file))
        assert ec.has_errors(), f"Expected errors for {input_file.name} but got none"

        actual_summary = ec.get_error_summary()
        first_line = expected_error.split('\n')[0]

        import re
        match = re.match(r'^.+:\d+:\d+:\s*error:\s*(.+)$', first_line)
        if match:
            expected_message = match.group(1).strip()
            assert expected_message in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_message!r}\n"
                f"Actual summary:\n{actual_summary}")
        else:
            assert expected_error in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_error!r}\n"
                f"Actual summary:\n{actual_summary}")


class TestFailingProcedures:

    @pytest.mark.parametrize("input_file", utils.collect_failing_inputs("procedures"))
    def test_catches_error(self, input_file: Path) -> None:
        text = input_file.read_text()
        error_file = input_file.with_name(input_file.name.replace(".fail.input.txt", ".fail.error.txt"))
        if not error_file.exists():
            error_file = input_file.with_name(input_file.name.replace(".input.txt", ".error.txt"))
        assert error_file.exists(), f"Missing error file: {error_file}"
        expected_error = error_file.read_text().strip()

        procs_dir = _procs_dir(input_file)
        proc_paths = [procs_dir] if procs_dir.exists() else None

        ec = _validate_text(text, filename=str(input_file), proc_search_paths=proc_paths)
        assert ec.has_errors(), f"Expected errors for {input_file.name} but got none"

        actual_summary = ec.get_error_summary()
        first_line = expected_error.split('\n')[0]

        import re
        match = re.match(r'^.+:\d+:\d+:\s*error:\s*(.+)$', first_line)
        if match:
            expected_message = match.group(1).strip()
            assert expected_message in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_message!r}\n"
                f"Actual summary:\n{actual_summary}")
        else:
            assert expected_error in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_error!r}\n"
                f"Actual summary:\n{actual_summary}")


class TestFailingOps:

    @pytest.mark.parametrize("input_file", utils.collect_failing_inputs("ops"))
    def test_catches_error(self, input_file: Path) -> None:
        text = input_file.read_text()
        error_file = input_file.with_name(input_file.name.replace(".input.txt", ".error.txt"))
        assert error_file.exists(), f"Missing error file: {error_file}"
        expected_error = error_file.read_text().strip()

        ec = _validate_text(text, filename=str(input_file))
        assert ec.has_errors(), f"Expected errors for {input_file.name} but got none"

        actual_summary = ec.get_error_summary()
        first_line = expected_error.split('\n')[0]

        import re
        match = re.match(r'^.+:\d+:\d+:\s*error:\s*(.+)$', first_line)
        if match:
            expected_message = match.group(1).strip()
            assert expected_message in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_message!r}\n"
                f"Actual summary:\n{actual_summary}")
        else:
            assert expected_error in actual_summary, (
                f"Error mismatch for {input_file.name}\n"
                f"Expected (substring): {expected_error!r}\n"
                f"Actual summary:\n{actual_summary}")


class TestNoFalsePositives:

    @pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files("vars", "hrw4u"))
    def test_valid_vars(self, input_file: Path, output_file: Path) -> None:
        text = input_file.read_text()
        ec = _validate_text(text)
        assert not ec.has_errors(), (f"Unexpected errors for valid input {input_file.name}:\n"
                                     f"{ec.get_error_summary()}")

    @pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files("hooks", "hrw4u"))
    def test_valid_hooks(self, input_file: Path, output_file: Path) -> None:
        text = input_file.read_text()
        ec = _validate_text(text)
        assert not ec.has_errors(), (f"Unexpected errors for valid input {input_file.name}:\n"
                                     f"{ec.get_error_summary()}")

    @pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files("conds", "hrw4u"))
    def test_valid_conds(self, input_file: Path, output_file: Path) -> None:
        text = input_file.read_text()
        ec = _validate_text(text)
        assert not ec.has_errors(), (f"Unexpected errors for valid input {input_file.name}:\n"
                                     f"{ec.get_error_summary()}")

    @pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files("ops", "hrw4u"))
    def test_valid_ops(self, input_file: Path, output_file: Path) -> None:
        text = input_file.read_text()
        ec = _validate_text(text)
        assert not ec.has_errors(), (f"Unexpected errors for valid input {input_file.name}:\n"
                                     f"{ec.get_error_summary()}")

    @pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files("procedures", "hrw4u"))
    def test_valid_procedures(self, input_file: Path, output_file: Path) -> None:
        text = input_file.read_text()
        procs_dir = input_file.parent / 'procs'
        proc_paths = [procs_dir] if procs_dir.exists() else None
        ec = _validate_text(text, filename=str(input_file), proc_search_paths=proc_paths)
        assert not ec.has_errors(), (f"Unexpected errors for valid input {input_file.name}:\n"
                                     f"{ec.get_error_summary()}")
