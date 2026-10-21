import socket
import time
from pathlib import Path

import pytest

from flakepilot.integrations.application.ports import ParsedTestCase, ReportRejected
from flakepilot.integrations.infrastructure import junit
from flakepilot.integrations.infrastructure.junit import JUnitParser

PARSE = JUnitParser().parse


def test_single_suite_outcomes_and_precedence() -> None:
    report = b"""<?xml version="1.0" encoding="UTF-8"?>
    <testsuite name="s" tests="5">
      <testcase classname=" a.B " name=" passes "/>
      <testcase classname="a.B" name="fails"><failure message="x">trace</failure></testcase>
      <testcase classname="a.B" name="errors"><error/></testcase>
      <testcase classname="a.B" name="both"><failure/><error/></testcase>
      <testcase classname="a.B" name="skips"><skipped/></testcase>
      <testcase name="no-class"><system-out>text</system-out></testcase>
    </testsuite>"""
    assert PARSE(report) == [
        ParsedTestCase("a.B", "passes", ("passed",)),
        ParsedTestCase("a.B", "fails", ("failed",)),
        ParsedTestCase("a.B", "errors", ("errored",)),
        ParsedTestCase("a.B", "both", ("errored",)),
        ParsedTestCase("a.B", "skips", ("skipped",)),
        ParsedTestCase("", "no-class", ("passed",)),
    ]


def test_nested_suites_and_surefire_reruns_become_attempts() -> None:
    report = b"""<testsuites><testsuite name="outer"><testsuite name="inner">
      <testcase classname="c" name="flaky">
        <flakyFailure message="m"/><flakyError/>
      </testcase>
      <testcase classname="c" name="broken">
        <failure/><rerunFailure/><rerunError/>
      </testcase>
    </testsuite></testsuite></testsuites>"""
    assert PARSE(report) == [
        ParsedTestCase("c", "flaky", ("failed", "errored", "passed")),
        ParsedTestCase("c", "broken", ("failed", "failed", "errored")),
    ]


def test_parsing_is_deterministic() -> None:
    report = b'<testsuite><testcase classname="c" name="t"/></testsuite>'
    assert PARSE(report) == PARSE(report)


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
<!ENTITY lol9 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">]>
<testsuite><testcase classname="c" name="&lol9;"/></testsuite>"""


@pytest.mark.parametrize(
    "report",
    [
        BILLION_LAUGHS,
        b'<!DOCTYPE t SYSTEM "file:///etc/passwd"><testsuite/>',
        (
            b'<!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b'<testsuite><testcase name="&x;"/></testsuite>'
        ),
        (
            b'<!DOCTYPE t [<!ENTITY x SYSTEM "http://127.0.0.1:9/x">]>'
            b'<testsuite><testcase name="&x;"/></testsuite>'
        ),
        b'<!DOCTYPE t [<!ENTITY % p SYSTEM "http://127.0.0.1:9/p"> %p;]><testsuite/>',
        b"<!DOCTYPE testsuite><testsuite/>",
    ],
    ids=[
        "billion-laughs",
        "external-dtd",
        "file-entity",
        "http-entity",
        "param-entity",
        "plain-doctype",
    ],
)
def test_doctypes_and_entities_are_refused_without_expansion_or_fetching(
    report: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parser opened a connection")

    def no_files(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parser opened a file")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(Path, "open", no_files)
    started = time.monotonic()
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(report)
    assert excinfo.value.reason == "forbidden_dtd"
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (b"<testsuite><testcase name='t'>", "malformed_xml"),
        (b"not xml at all", "malformed_xml"),
        (b"<testsuite><testcase name='&undefined;'/></testsuite>", "malformed_xml"),
        (b"<html><body/></html>", "not_junit"),
        (b"<testsuite><testcase classname='c'/></testsuite>", "missing_test_name"),
        (b"<testsuite><testcase name=' '/></testsuite>", "missing_test_name"),
        (
            b"<testsuite><testcase name='a'><testcase name='b'/></testcase></testsuite>",
            "not_junit",
        ),
        (
            b"<testsuite><testcase name='" + b"n" * 1001 + b"'/></testsuite>",
            "value_too_long",
        ),
        (b"<testsuite>" * 33 + b"</testsuite>" * 33, "too_deep"),
        (
            b"<testsuite><testcase name='t'><failure/>"
            + b"<rerunFailure/>" * junit.MAX_ATTEMPTS
            + b"</testcase></testsuite>",
            "too_many_attempts",
        ),
    ],
)
def test_invalid_reports_are_refused_with_a_reason(report: bytes, reason: str) -> None:
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(report)
    assert excinfo.value.reason == reason
    assert "SENTINEL" not in str(excinfo.value)


@pytest.mark.parametrize(
    "encoding",
    ["UTF-32", "UTF-7", "Shift_JIS", "Big5", "GB2312", "base64", "rot13", "x-nope"],
)
def test_undecodable_declared_encodings_are_malformed(encoding: str) -> None:
    report = f'<?xml version="1.0" encoding="{encoding}"?><testsuite/>'.encode()
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(report)
    assert excinfo.value.reason == "malformed_xml"


@pytest.mark.parametrize(
    "character",
    ["\u0085", "\u009f", "\u200e", "\u202a", "\u202e", "\u2066", "\u2069", "\ufeff"],
)
@pytest.mark.parametrize("attribute", ["classname", "name"])
def test_control_and_format_characters_in_names_are_refused(
    character: str, attribute: str
) -> None:
    other = "name" if attribute == "classname" else "classname"
    report = (
        f'<testsuite><testcase {other}="ok" {attribute}="a{character}b"/></testsuite>'
    ).encode()
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(report)
    assert excinfo.value.reason == "invalid_characters"


def test_tab_character_references_are_refused_but_plain_unicode_is_kept() -> None:
    with pytest.raises(ReportRejected):
        PARSE(b'<testsuite><testcase name="a&#9;b"/></testsuite>')
    [case] = PARSE('<testsuite><testcase name="tést ✓ 测试"/></testsuite>'.encode())
    assert case.name == "tést ✓ 测试"


def test_counts_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(junit, "MAX_CASES", 3)
    cases = b"".join(b"<testcase name='t%d'/>" % i for i in range(4))
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(b"<testsuite>" + cases + b"</testsuite>")
    assert excinfo.value.reason == "too_many_cases"
    monkeypatch.setattr(junit, "MAX_ELEMENTS", 3)
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(b"<testsuite><a/><b/><c/></testsuite>")
    assert excinfo.value.reason == "too_many_elements"


def test_oversized_input_is_refused_before_parsing() -> None:
    with pytest.raises(ReportRejected) as excinfo:
        PARSE(b" " * (junit.MAX_BYTES + 1))
    assert excinfo.value.reason == "too_large"
