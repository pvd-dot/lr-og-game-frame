"""Native Linux x86-64 GDB integration against the owned sample game fixture."""
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT/'examples/sample-game/build/native-stops'


def run(argv, log, env=None):
    with log.open('w') as stream:
        child = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True)
        try:
            result = child.wait(timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
    if result:
        raise AssertionError(log.read_text())


@unittest.skipUnless(platform.system() == 'Linux' and platform.machine() == 'x86_64',
                     'native integration requires Linux x86-64; not validated here')
class NativeStops(unittest.TestCase):
    def test_concurrent_stops_finish_and_refusal(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        compiler, debugger = shutil.which('cc'), shutil.which('gdb')
        self.assertIsNotNone(compiler, 'cc required for native GDB integration')
        self.assertIsNotNone(debugger, 'gdb required for native integration')
        binary = BUILD/'fixture'
        run([compiler, '-O0', '-g', '-pthread', '-o', str(binary),
             str(ROOT/'examples/sample-game/native_threads.c')], BUILD/'compile.log')
        for mode, repeat in [('normal', i) for i in range(3)] + [('duplicate', 0), ('changed_trigger', 0)]:
            result = BUILD/f'result-{mode}-{repeat}.json'
            env = dict(os.environ, NATIVE_STOPS_ROOT=str(ROOT), NATIVE_STOPS_RESULT=str(result), NATIVE_STOPS_MODE=mode)
            script = repr(str(ROOT/'tests/fixtures/native_stops_gdb.py'))
            run([debugger, '-q', '-nx', '-batch', '-ex',
                 f'python exec(compile(open({script}).read(), {script}, "exec"))',
                 '--args', str(binary)], BUILD/f'gdb-{mode}-{repeat}.log', env)
            report = json.loads(result.read_text())
            self.assertIsNone(report['failure'], report)
            if mode == 'normal':
                self.assertEqual(report['finishes'], [1, 2, 3])
                self.assertEqual(report['watches'], [1, 2, 3])
                self.assertEqual(len(report['records']), 3)
                self.assertEqual(len(report['rejections']), 8)
            else:
                self.assertEqual(len(report['rejections']), 2)
            self.assertEqual(report['pid_after'], 0)


if __name__ == '__main__':
    unittest.main()
