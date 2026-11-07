"""Dispatch native GDB capture work after an explicit all-thread stop.

Inside GDB Python, create Dispatcher(gdb, registers=('pc', 'sp')). A breakpoint's
stop() returns dispatcher.defer(callback, *args); outside stop(), repeatedly call
continue_once(). This returns the callback's result after verifying the triggering
thread and readable scalar registers on every current thread. Callback code may
then inspect frames or create finish breakpoints. It must not resume the inferior.
Optional expected_ptids freezes a caller-owned thread membership contract.

The helper does not attach, install breakpoints, resume after errors, save memory,
prove atomicity or timing neutrality, or capture SIMD/vector registers. It requires
one native inferior and all-stop mode. Current membership is checked twice; that
is not proof of an application's complete thread/domain coverage. No game inputs.
"""

class StopError(RuntimeError):
    pass


def stopped_threads(gdb, registers=('pc', 'sp'), expected_ptids=None):
    """Validate current threads; restore selected thread even after failed reads."""
    registers = tuple(registers)
    if not registers or len(set(registers)) != len(registers):
        raise StopError('require distinct scalar register names')
    inferiors = gdb.inferiors()
    if gdb.parameter('non-stop') or len(inferiors) != 1:
        raise StopError('requires one inferior in all-stop mode')
    inferior = gdb.selected_inferior()
    selected = gdb.selected_thread()
    if not inferior.pid or selected is None or not selected.is_valid():
        raise StopError('no live selected thread')
    threads = tuple(inferior.threads())
    membership = {tuple(t.ptid) for t in threads}
    if not threads or len(membership) != len(threads):
        raise StopError('empty or ambiguous thread membership')
    if expected_ptids is not None and membership != set(expected_ptids):
        raise StopError('unexpected thread membership')
    rows = []
    try:
        for thread in threads:
            if (not thread.is_valid() or thread.is_exited() or
                    not thread.is_stopped() or thread.is_running()):
                raise StopError('thread is not stopped')
            thread.switch()
            frame = gdb.newest_frame()
            values = {name: int(frame.read_register(name)) for name in registers}
            rows.append({'ptid': tuple(thread.ptid), 'global_num': thread.global_num,
                         'registers': values})
        after = tuple(inferior.threads())
        if ({tuple(t.ptid) for t in after} != membership or len(after) != len(threads)
                or any(not t.is_valid() or t.is_exited() or not t.is_stopped()
                       or t.is_running() for t in after)):
            raise StopError('thread membership or stop state changed during reads')
    except StopError:
        raise
    except Exception as error:
        raise StopError('required thread registers unavailable: '+str(error)) from error
    finally:
        if selected.is_valid():
            selected.switch()
    return {'inferior': inferior.num, 'pid': inferior.pid,
            'selected_ptid': tuple(selected.ptid), 'threads': rows}


class Dispatcher:
    """One queued stop callback per continue; every unexpected stop is an error."""
    def __init__(self, gdb, registers=('pc', 'sp'), expected_ptids=None):
        self.gdb = gdb
        self.registers = tuple(registers)
        self.expected_ptids = (None if expected_ptids is None else
                               frozenset(tuple(p) for p in expected_ptids))
        self.pending = None
        self.queue_error = None
        self.last_snapshot = None

    def defer(self, callback, *args):
        """Call only in stop(); record identity without switching frames/threads."""
        if self.pending is not None:
            self.queue_error = 'multiple callbacks queued for one stop'
        else:
            inferior = self.gdb.selected_inferior()
            thread = self.gdb.selected_thread()
            self.pending = (inferior.num, inferior.pid, tuple(thread.ptid), callback, args)
        return True

    def continue_once(self):
        """Resume once, then dispatch only a queued, verified explicit stop."""
        if self.pending is not None or self.queue_error is not None:
            raise StopError('unconsumed stop before continue')
        self.gdb.execute('continue')
        return self.dispatch_pending()

    def dispatch_pending(self):
        """Use after an outer continue returns; never call inside stop()."""
        if self.queue_error is not None:
            raise StopError(self.queue_error)
        if self.pending is None:
            raise StopError('unexpected stop or inferior exit; no callback queued')
        inferior_num, pid, ptid, callback, args = self.pending
        self.pending = None
        snapshot = stopped_threads(self.gdb, self.registers, self.expected_ptids)
        if (snapshot['inferior'], snapshot['pid'], snapshot['selected_ptid']) != (
                inferior_num, pid, ptid):
            raise StopError('triggering inferior or selected thread changed')
        self.last_snapshot = snapshot
        return callback(*args)


if __name__ == '__main__':
    print(__doc__)
