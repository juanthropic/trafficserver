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

import pytest

from hrw4u.cachekey import is_cachekey_statement, merge, parse_statement, render
from hrw4u.errors import SymbolResolutionError


def flags(*calls: tuple[str, list[str]]) -> list[str]:
    return merge(parse_statement(name, args) for name, args in calls)


class TestIsCachekeyStatement:

    @pytest.mark.parametrize(
        "name", [
            "cache.url.pristine",
            "cache.url.query.remove",
            "cache.url.query.keep",
            "cache.url.query.sort",
            "cache.key.add_headers",
            "cache.key.add_cookies",
        ])
    def test_known(self, name: str) -> None:
        assert is_cachekey_statement(name)

    @pytest.mark.parametrize("name", ["cache", "cache.bypass", "cache.url", "run-plugin", "cache.key.require_headers"])
    def test_unknown(self, name: str) -> None:
        assert not is_cachekey_statement(name)


class TestMerge:

    def test_no_statements_still_has_canonical_prefix(self) -> None:
        assert merge([]) == ["--canonical-prefix=true"]

    @pytest.mark.parametrize(
        "name,args,flag", [
            ("cache.url.pristine", [], "--uri-type=pristine"),
            ("cache.url.query.remove", ['"a,b"'], "--exclude-params=a,b"),
            ("cache.url.query.keep", ['"a,b"'], "--include-params=a,b"),
            ("cache.url.query.sort", [], "--sort-params=true"),
            ("cache.key.add_headers", ['"H1,@H2"'], "--include-headers=H1,@H2"),
            ("cache.key.add_cookies", ['"c1,c2"'], "--include-cookies=c1,c2"),
        ])
    def test_one_statement(self, name: str, args: list[str], flag: str) -> None:
        assert flags((name, args)) == ["--canonical-prefix=true", flag]

    def test_repeats_merge_dedupe_and_keep_first_seen_order(self) -> None:
        assert flags(
            ("cache.key.add_headers", ['"B,A"']),
            ("cache.key.add_headers", ['"A,C"']),
        ) == ["--canonical-prefix=true", "--include-headers=B,A,C"]

    def test_repeated_flag_statement_is_a_no_op(self) -> None:
        assert flags(("cache.url.pristine", []), ("cache.url.pristine", [])) == ["--canonical-prefix=true", "--uri-type=pristine"]

    def test_table_order_not_source_order(self) -> None:
        expected = [
            "--canonical-prefix=true",
            "--uri-type=pristine",
            "--exclude-params=q",
            "--include-params=k",
            "--sort-params=true",
            "--include-headers=H",
            "--include-cookies=c",
        ]
        assert flags(
            ("cache.key.add_cookies", ['"c"']),
            ("cache.key.add_headers", ['"H"']),
            ("cache.url.query.sort", []),
            ("cache.url.query.keep", ['"k"']),
            ("cache.url.query.remove", ['"q"']),
            ("cache.url.pristine", []),
        ) == expected

    def test_client_api(self) -> None:
        assert flags(
            ("cache.url.pristine", []),
            (
                "cache.url.query.remove",
                ['"ign-sf,guid,ign-lr,ign-mpt,ign-msr,x-itunes-client-guid,ign-refClientId,X-JS-SP-TOKEN,X-JS-TIMESTAMP"']),
            ("cache.key.add_headers", ['"X-Apple-Store-Front,@Prism-Country"']),
            ("cache.key.add_cookies", ['"itst,itre,itfe,itrv"']),
        ) == [
            "--canonical-prefix=true",
            "--uri-type=pristine",
            "--exclude-params=ign-sf,guid,ign-lr,ign-mpt,ign-msr,x-itunes-client-guid,ign-refClientId,X-JS-SP-TOKEN,X-JS-TIMESTAMP",
            "--include-headers=X-Apple-Store-Front,@Prism-Country",
            "--include-cookies=itst,itre,itfe,itrv",
        ]


class TestRender:

    def test_quotes_each_flag(self) -> None:
        assert render(["--canonical-prefix=true", "--uri-type=pristine"]
                     ) == 'run-plugin "cachekey.so" "--canonical-prefix=true" "--uri-type=pristine"'


class TestParseStatementErrors:

    @pytest.mark.parametrize(
        "name,args,message", [
            ("cache.url.bogus", [], "Unknown cache key statement"),
            ("cache.url.pristine", ['"x"'], "takes no arguments"),
            ("cache.url.query.sort", ['"x"'], "takes no arguments"),
            ("cache.key.add_headers", [], "takes one string argument"),
            ("cache.key.add_headers", ['"a"', '"b"'], "takes one string argument"),
            ("cache.key.add_headers", ["X-Foo"], "must be a quoted string"),
            ("cache.key.add_headers", ["42"], "must be a quoted string"),
            ("cache.key.add_headers", ['"{inbound.req.X}"'], "static"),
            ("cache.key.add_headers", ['"%{CLIENT-HEADER:X}"'], "static"),
            ("cache.key.add_headers", ['""'], "empty name"),
            ("cache.key.add_headers", ['"a,,b"'], "empty name"),
            ("cache.key.add_headers", ['"a,"'], "empty name"),
            ("cache.key.add_cookies", ['"a, b"'], "whitespace"),
            ("cache.url.query.remove", ['"a b"'], "whitespace"),
            ("cache.key.add_headers", ['"a\\"b"'], "quote or backslash"),
            ("cache.key.add_headers", ['"a\\nb"'], "quote or backslash"),
        ])
    def test_rejects(self, name: str, args: list[str], message: str) -> None:
        with pytest.raises(SymbolResolutionError, match=message):
            parse_statement(name, args)
