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
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import utils

GROUP = "cache_symbol"


@pytest.mark.cache
@pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files(GROUP, "hrw4u"))
def test_output_matches(input_file: Path, output_file: Path) -> None:
    """Test that cache.* statements lower to the expected cachekey.so rule."""
    utils.run_output_test(input_file, output_file)


@pytest.mark.cache
@pytest.mark.ast
@pytest.mark.parametrize("input_file,ast_file", utils.collect_ast_test_files(GROUP))
def test_ast_matches(input_file: Path, ast_file: Path) -> None:
    """Test that AST structure matches expected AST for cache.* test cases."""
    utils.run_ast_test(input_file, ast_file)


@pytest.mark.cache
@pytest.mark.invalid
@pytest.mark.parametrize("input_file", utils.collect_failing_inputs(GROUP))
def test_invalid_inputs_fail(input_file: Path) -> None:
    """Test that misplaced or conflicting cache.* statements produce expected errors."""
    utils.run_failing_test(input_file)


@pytest.mark.cache
@pytest.mark.parametrize("input_file,output_file", utils.collect_output_test_files(GROUP, "hrw4u"))
def test_valid_input_writes_nothing_to_stderr(input_file: Path, output_file: Path) -> None:
    """voluspa fails a render on any hrw4u stderr output, so valid input must stay silent."""
    script = Path("scripts/hrw4u").resolve()
    result = subprocess.run([sys.executable, str(script), str(input_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.strip() == output_file.read_text().strip()
