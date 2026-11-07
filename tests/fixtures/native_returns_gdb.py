"""Real GDB fixture for saved-return tracking; owned C target only."""
import json
import os
from pathlib import Path
import sys
import gdb

root = Path(os.environ['NATIVE_RETURNS_ROOT'])
sys.path.insert(0, str(root/'tools/live'))
from gdbstops import Dispatcher, StopError
from gdbreturns import ReturnTracker
mode = os.environ['NATIVE_RETURNS_MODE']
report = {'mode': mode, 'returns': [], 'checkpoints': 0, 'lost_finish': 0, 'rejection': None, 'failure': None}

def returned(kind, sp, pc):
    assert int(gdb.parse_and_eval('$rsp')) == sp+8
    assert int(gdb.parse_and_eval('$pc')) == pc
    report['returns'].append([kind, int(gdb.parse_and_eval('$rax'))])

class FrameWitness(gdb.FinishBreakpoint):
    def stop(self):
        return False
    def out_of_scope(self):
        report['lost_finish'] += 1

def entry(kind):
    sp = int(gdb.parse_and_eval('$rsp'))
    pc = int.from_bytes(bytes(gdb.selected_inferior().read_memory(sp, 8)), 'little')
    handle = tracker.watch(pc, sp, returned, kind, sp, pc)
    if mode == 'normal' and kind == 'tail':
        FrameWitness(gdb.newest_frame(), internal=True)
    if mode == 'duplicate':
        tracker.watch(pc, sp, returned, kind, sp, pc)
    elif mode == 'mismatch':
        gdb.selected_inferior().write_memory(sp, (pc+1).to_bytes(8, 'little'))
    elif mode == 'cancel':
        handle.enabled = False
        assert not handle.is_valid() and not handle.enabled
    elif mode == 'thread_missing':
        # Simulate missing membership in a current validated snapshot.
        snapshot = dict(dispatcher.last_snapshot, threads=[])
        tracker.validate(snapshot)

def checkpoint():
    report['checkpoints'] += 1

class Probe(gdb.Breakpoint):
    def __init__(self, name, callback, *args):
        self.callback, self.args = callback, args
        super().__init__('*'+name, internal=True)
    def stop(self):
        return dispatcher.defer(self.callback, *self.args)

try:
    gdb.execute('set pagination off')
    gdb.execute('set confirm off')
    gdb.execute('set non-stop off')
    gdb.execute('start')
    dispatcher = Dispatcher(gdb)
    tracker = ReturnTracker(gdb, dispatcher)
    Probe('escape_frame' if mode == 'nonlocal' else 'tail', entry, 'tail')
    if mode == 'normal':
        Probe('recurse', entry, 'recurse')
    Probe('checkpoint', checkpoint)
    for _ in range(40):
        try:
            dispatcher.continue_once()
        except StopError as error:
            report['rejection'] = str(error)
            break
    else:
        raise AssertionError('fixture never terminated')
    if mode == 'normal':
        assert len(report['returns']) == 6, report
        assert report['lost_finish'] >= 1, report
        assert not tracker.handles and not tracker.sites
        assert sorted(v for k, v in report['returns'] if k == 'recurse') == [1, 2, 3, 4]
        assert sorted(v for k, v in report['returns'] if k == 'tail') == [8, 10]
        assert 'inferior exit' in report['rejection']
    elif mode == 'cancel':
        assert not report['returns'] and not tracker.handles and not tracker.sites
    elif mode == 'exit':
        assert 'inferior exit' in report['rejection']
        assert tracker.handles and not report['returns']
    elif mode == 'nonlocal':
        assert report['rejection'] in ('saved return slot changed', 'tracked return escaped stack bounds')
        assert not report['returns']
    else:
        expected = {'duplicate': 'duplicate live', 'mismatch': 'slot changed',
                    'nonlocal': 'escaped stack', 'thread_missing': 'thread disappeared'}[mode]
        assert expected in report['rejection'], report
        assert not report['returns']
except Exception as error:
    report['failure'] = repr(error)
finally:
    if 'tracker' in globals():
        tracker.close()
    if gdb.selected_inferior().pid:
        gdb.execute('kill')
    report['pid_after'] = gdb.selected_inferior().pid
    report['gdb'] = gdb.VERSION
    Path(os.environ['NATIVE_RETURNS_RESULT']).write_text(json.dumps(report, indent=2)+'\n')
