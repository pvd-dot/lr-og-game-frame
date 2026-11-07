"""Executed inside native GDB against our own concurrent sample."""
import json
import os
from pathlib import Path
import sys
import time

import gdb

root = Path(os.environ['NATIVE_STOPS_ROOT'])
sys.path.insert(0, str(root/'tools/live'))
from gdbstops import Dispatcher, StopError, stopped_threads

records = []
errors = []
finishes = []
watch_values = []
mode = os.environ.get("NATIVE_STOPS_MODE", "normal")

def rejected(action):
    try:
        action()
    except StopError as error:
        errors.append(str(error))
    else:
        raise AssertionError('incomplete state accepted')

def progress():
    address = int(gdb.parse_and_eval('&pulses'))
    data = bytes(gdb.selected_inferior().read_memory(address, 16))
    return [int.from_bytes(data[i:i+8], 'little') for i in (0, 8)]

class Finish(gdb.FinishBreakpoint):
    def __init__(self, value):
        self.value = value
        super().__init__(gdb.newest_frame(), internal=True)
    def stop(self):
        return dispatcher.defer(finished, self.value)
    def out_of_scope(self):
        errors.append('unexpected lost finish')

class Probe(gdb.Breakpoint):
    def stop(self):
        return dispatcher.defer(captured)

class Watch(gdb.Breakpoint):
    def __init__(self):
        super().__init__("*(unsigned int *)&watched", type=gdb.BP_WATCHPOINT,
                         wp_class=gdb.WP_WRITE, internal=True)
    def stop(self):
        return dispatcher.defer(watched)

def watched():
    before = progress()
    time.sleep(.05)
    assert progress() == before
    assert len(dispatcher.last_snapshot["threads"]) == 4
    watch_values.append(int(gdb.parse_and_eval("watched")))

def captured():
    snapshot = dispatcher.last_snapshot
    assert len(snapshot['threads']) == 4
    assert len({t['ptid'] for t in snapshot['threads']}) == 4
    before = progress()
    time.sleep(.05)
    assert progress() == before
    # Frame and register access plus new finish breakpoint happen after continue.
    value = int(gdb.parse_and_eval('value'))
    Finish(value)
    current = frozenset(t['ptid'] for t in snapshot['threads'])
    if dispatcher.expected_ptids is None:
        dispatcher.expected_ptids = current
    assert current == dispatcher.expected_ptids
    rejected(lambda: stopped_threads(gdb, expected_ptids=set()))
    rejected(lambda: stopped_threads(gdb, registers=('not_a_register_087',)))
    assert tuple(gdb.selected_thread().ptid) == snapshot['selected_ptid']
    records.append({'value': value, 'snapshot': snapshot, 'progress': before})

def finished(value):
    assert int(gdb.parse_and_eval('$rax')) == value+7
    finishes.append(value)

failure = None
try:
    gdb.execute('set pagination off')
    gdb.execute('set confirm off')
    gdb.execute('set non-stop off')
    gdb.execute('start')
    gdb.execute('set scheduler-locking off')
    dispatcher = Dispatcher(gdb)
    rejected(dispatcher.dispatch_pending)
    probe = Probe('step', internal=True)
    if mode == 'duplicate':
        second = Probe('step', internal=True)
        rejected(dispatcher.continue_once)
        assert 'multiple callbacks' in errors[-1]
        assert not records
    elif mode == 'changed_trigger':
        def change_trigger(event):
            selected = gdb.selected_thread()
            next(t for t in gdb.selected_inferior().threads() if t != selected).switch()
        gdb.events.stop.connect(change_trigger)
        rejected(dispatcher.continue_once)
        assert 'selected thread changed' in errors[-1]
        assert not records
    else:
        watch = Watch()
        for _ in range(9):
            dispatcher.continue_once()
        assert [r['value'] for r in records] == [1, 2, 3]
        assert finishes == [1, 2, 3]
        assert watch_values == [1, 2, 3]
        probe.delete()
        watch.delete()
        rejected(dispatcher.continue_once)  # inferior exit is not accepted
        assert not gdb.selected_inferior().pid
except Exception as error:
    failure = str(error)
finally:
    if gdb.selected_inferior().pid:
        gdb.execute('kill')
    Path(os.environ['NATIVE_STOPS_RESULT']).write_text(json.dumps({
        'records': records, 'finishes': finishes, 'watches': watch_values,
        'mode': mode, 'rejections': errors,
        'failure': failure, 'pid_after': gdb.selected_inferior().pid,
        'gdb': gdb.VERSION}, indent=2)+'\n')
