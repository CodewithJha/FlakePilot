"""JUnit XML report parser (ADR-009: hardened parsing; ADR-013) on the stdlib expat parser.

The report is untrusted. Limits (provisional, ADR-013) are checked before and during
parsing; any DOCTYPE, entity declaration or external entity is refused outright, before
anything is declared or expanded, so entity-expansion and external-entity payloads never
run. Text content is never read or kept; only testcase names and outcome elements are.

Outcomes: `<error>` → errored, `<failure>` → failed, `<skipped>` → skipped, none → passed
(precedence in that order). Maven Surefire rerun elements express retries within a run:
`flakyFailure`/`flakyError` are failed attempts before a final pass; `rerunFailure`/
`rerunError` are further failed attempts after the first failure.
"""

from xml.parsers import expat

from flakepilot.integrations.application.ports import ParsedTestCase, ReportRejected

MAX_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 200_000
MAX_CASES = 20_000
MAX_ATTEMPTS = 50  # per test case in one report
MAX_NAME_CHARS = 1_000

_ROOTS = {"testsuites", "testsuite"}
_MAIN = {"error": "errored", "failure": "failed", "skipped": "skipped"}
_BEFORE_PASS = {"flakyFailure": "failed", "flakyError": "errored"}
_AFTER_FAILURE = {"rerunFailure": "failed", "rerunError": "errored"}


def _refuse(*_: object) -> int:
    raise ReportRejected("forbidden_dtd")


class _Case:
    def __init__(self, classname: str, name: str, depth: int) -> None:
        self.classname, self.name, self.depth = classname, name, depth
        self.main: set[str] = set()
        self.before_pass: list[str] = []
        self.after_failure: list[str] = []

    def attempts(self) -> tuple[str, ...]:
        final = next(
            (_MAIN[tag] for tag in ("error", "failure", "skipped") if tag in self.main),
            "passed",
        )
        if final in ("failed", "errored"):
            attempts = [final, *self.after_failure]
        elif final == "passed":
            attempts = [*self.before_pass, final]
        else:
            attempts = [final]
        if len(attempts) > MAX_ATTEMPTS:
            raise ReportRejected("too_many_attempts")
        return tuple(attempts)


class _Handler:
    def __init__(self) -> None:
        self.depth = 0
        self.elements = 0
        self.case: _Case | None = None
        self.cases: list[ParsedTestCase] = []

    def start(self, tag: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_DEPTH:
            raise ReportRejected("too_deep")
        if self.elements > MAX_ELEMENTS:
            raise ReportRejected("too_many_elements")
        if self.depth == 1 and tag not in _ROOTS:
            raise ReportRejected("not_junit")
        if tag == "testcase":
            if self.case is not None:
                raise ReportRejected("not_junit")
            self.case = _Case(
                _value(attributes, "classname"), _value(attributes, "name"), self.depth
            )
            if not self.case.name:
                raise ReportRejected("missing_test_name")
        elif self.case is not None and self.depth == self.case.depth + 1:
            if tag in _MAIN:
                self.case.main.add(tag)
            elif tag in _BEFORE_PASS:
                self.case.before_pass.append(_BEFORE_PASS[tag])
            elif tag in _AFTER_FAILURE:
                self.case.after_failure.append(_AFTER_FAILURE[tag])

    def end(self, tag: str) -> None:
        if self.case is not None and self.depth == self.case.depth:
            case = self.case
            self.cases.append(
                ParsedTestCase(case.classname, case.name, case.attempts())
            )
            if len(self.cases) > MAX_CASES:
                raise ReportRejected("too_many_cases")
            self.case = None
        self.depth -= 1


def _value(attributes: dict[str, str], name: str) -> str:
    value = attributes.get(name, "").strip()
    if len(value) > MAX_NAME_CHARS:
        raise ReportRejected("value_too_long")
    return value


class JUnitParser:
    def parse(self, data: bytes) -> list[ParsedTestCase]:
        if len(data) > MAX_BYTES:
            raise ReportRejected("too_large")
        handler = _Handler()
        parser = expat.ParserCreate()
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.StartDoctypeDeclHandler = _refuse
        parser.EntityDeclHandler = _refuse
        parser.UnparsedEntityDeclHandler = _refuse
        parser.NotationDeclHandler = _refuse
        parser.ExternalEntityRefHandler = _refuse
        parser.StartElementHandler = handler.start
        parser.EndElementHandler = handler.end
        try:
            parser.Parse(data, True)
        except expat.ExpatError:
            raise ReportRejected("malformed_xml") from None
        return handler.cases
