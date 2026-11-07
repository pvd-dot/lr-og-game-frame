"""Native GDB owned-fixture integration for x86-64 return tracking."""
import json
import os
from pathlib import Path
import platform
import shutil
import unittest
from test_native_stops import run

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT/'examples/sample-game/build/native-returns'

@unittest.skipUnless(platform.system() == 'Linux' and platform.machine() == 'x86_64',
                     'native integration requires Linux x86-64; not validated here')
class NativeReturns(unittest.TestCase):
    def test_returns_and_refusal(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        compiler, debugger = shutil.which('cc'), shutil.which('gdb')
        self.assertIsNotNone(compiler)
        self.assertIsNotNone(debugger)
        binary = BUILD/'fixture'
        run([compiler, '-O2', '-g', '-pthread', '-fno-inline', '-o', str(binary),
             str(ROOT/'examples/sample-game/native_returns.c')], BUILD/'compile.log')
        for mode in ['normal', 'duplicate', 'mismatch', 'cancel', 'thread_missing', 'nonlocal', 'exit']:
            result = BUILD/f'{mode}.json'
            env = dict(os.environ, NATIVE_RETURNS_ROOT=str(ROOT),
                       NATIVE_RETURNS_RESULT=str(result), NATIVE_RETURNS_MODE=mode)
            script = repr(str(ROOT/'tests/fixtures/native_returns_gdb.py'))
            argv = [debugger, '-q', '-nx', '-batch', '-ex',
                    f'python exec(compile(open({script}).read(), {script}, "exec"))',
                    '--args', str(binary)]
            if mode == 'nonlocal':
                argv.append('escape')
            elif mode == 'exit':
                argv.extend(['exit', 'now'])
            run(argv, BUILD/f'{mode}.log', env)
            report = json.loads(result.read_text())
            self.assertIsNone(report['failure'], report)
            self.assertEqual(report['pid_after'], 0)

if __name__ == '__main__':
    unittest.main()
