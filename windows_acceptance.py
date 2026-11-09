"""Run isolated Windows fixture checks and write a machine-readable report.

Does not start production DSH, modify DSH homes or certify native DSH shutdown.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import sys
import unittest

ROOT = Path(__file__).resolve().parent


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--node', type=Path)
    args = parser.parse_args()
    if args.node:
        os.environ['DSH_GUARD_TEST_NODE'] = str(args.node.resolve())
    # -I isolates startup; explicitly load only the package's reviewed test code.
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in
                               ('test_windows', 'test_console', 'test_launcher.HeartbeatTests'))
    result = unittest.TextTestRunner(verbosity=2, resultclass=Results).run(suite)
    native = [name for name in result.passed if '.NativeWindowsTests.' in name]
    accepted = os.name == 'nt' and len(native) == 7 and result.wasSuccessful() and not result.skipped
    report = {
        'schema': 1, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(),
        'tests_run': result.testsRun, 'passed': result.passed,
        'skipped': [{'test': test.id(), 'reason': reason} for test, reason in result.skipped],
        'failures': [{'test': test.id(), 'traceback': text} for test, text in result.failures + result.errors],
        'native_windows_fixture_checks_passed': accepted,
        'native_dsh_accepted': False, 'candidate_isolation_accepted': False,
        'automatic_recovery_enabled': False,
        'remaining': ['Native DSH graceful shutdown under the installed background task',
                      'Real DSH readiness/browser initialization and external business calls',
                      'Windows candidate containment and recovery parity'],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(args.output), 'native_windows_fixture_checks_passed': accepted,
                      'native_dsh_accepted': False}))
    return 0 if accepted else 1 if not result.wasSuccessful() else 2


if __name__ == '__main__':
    raise SystemExit(main())
