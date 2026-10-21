"""Classify probe stdout with the product rule.

This runs on the validator, not in the sandbox. A result here is a software
decision about bytes. It does not attest that isolation was enforced.
"""

import sys

from flakepilot.execution.domain.reproduction import classify

SELECTOR = "tests.login.TestLogin.test_ok"


def main() -> None:
    data = sys.stdin.buffer.read()
    decision = classify("finished", "probe", data, selector=SELECTOR)
    sys.stdout.write(f"{decision.outcome} {decision.reason}\n")


if __name__ == "__main__":
    main()
