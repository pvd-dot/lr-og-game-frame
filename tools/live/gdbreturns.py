"""Track x86-64 native returns across tail calls at explicit all-thread stops.

Inside GDB Python: tracker = ReturnTracker(gdb, dispatcher); in a dispatched
entry callback use tracker.watch(return_pc, entry_sp, callback, *args). The saved
8-byte little-endian return slot must match return_pc. The callback runs outside
Breakpoint.stop(), on the original PTID at return_pc with SP == entry_sp + 8.
The returned handle supports is_valid() and enabled=False for cancellation.
Shared return sites and recursive frames share one breakpoint per address.

Requires gdbstops.Dispatcher with pc/sp registers, native x86-64 downward stacks
and ordinary near returns. Each explicit stop rechecks thread membership, the
saved slot, and stack bounds. Lost frames, exits and rewritten slots fail closed;
callers must terminate capture after any error. Signals on alternate stacks,
stack switching, nonlocal exits and unusual return conventions are unsupported.
This does not prove SIMD state, atomic capture, code-pointer provenance, stack
ownership, or complete application domain coverage. Inputs are caller-pinned
code addresses, not symbols inferred by this helper. No game material.
Run with no arguments to print this help; import only inside a stopped GDB run.
"""
from gdbstops import StopError


class ReturnHandle:
    """Single-use return observation. A disabled handle cannot be re-enabled."""
    def __init__(self, owner, ptid, pc, sp, callback, args):
        self.owner, self.ptid, self.pc, self.sp = owner, ptid, pc, sp
        self.callback, self.args, self.active = callback, args, True

    def is_valid(self):
        return self.active

    @property
    def enabled(self):
        return self.active

    @enabled.setter
    def enabled(self, value):
        if value:
            if not self.active:
                raise StopError('cannot re-enable consumed return handle')
        else:
            self.owner.cancel(self)


class ReturnTracker:
    def __init__(self, gdb, dispatcher):
        if not {'pc', 'sp'} <= set(dispatcher.registers):
            raise StopError('return tracker requires pc/sp snapshots')
        self.gdb, self.dispatcher = gdb, dispatcher
        self.identity = None
        self.handles, self.sites = [], {}
        self.failed = False
        dispatcher.add_validator(self.validate)

    def _fail(self, message):
        self.failed = True
        raise StopError(message)

    def _slot(self, sp):
        try:
            return int.from_bytes(bytes(self.gdb.selected_inferior().read_memory(sp, 8)), 'little')
        except Exception as error:
            self._fail('saved return slot unreadable: '+str(error))

    def watch(self, return_pc, entry_sp, callback, *args):
        """Arm only in a current validated dispatch, never in a GDB stop method."""
        snapshot = self.dispatcher.last_snapshot
        if self.failed or snapshot is None:
            self._fail('return watch requires a current validated stop')
        inferior, thread = self.gdb.selected_inferior(), self.gdb.selected_thread()
        identity = (inferior.num, inferior.pid)
        ptid = tuple(thread.ptid)
        if identity != (snapshot['inferior'], snapshot['pid']) or ptid != snapshot['selected_ptid']:
            self._fail('return watch selected identity changed')
        frame = self.gdb.newest_frame()
        if frame.architecture().name() != 'i386:x86-64':
            self._fail('return tracker supports native x86-64 only')
        rows = {r['ptid']: r['registers'] for r in snapshot['threads']}
        if (type(return_pc) is not int or type(entry_sp) is not int or
                not 0 < return_pc < 2**64 or not 0 < entry_sp < 2**64-8 or
                entry_sp % 8 or rows[ptid]['sp'] != entry_sp or
                int(frame.read_register('sp')) != entry_sp or not callable(callback)):
            self._fail('invalid return address, entry SP or callback')
        if self.identity is not None and identity != self.identity:
            self._fail('return tracker inferior changed')
        if any(h.ptid == ptid and h.sp == entry_sp for h in self.handles):
            self._fail('duplicate live return frame')
        if self._slot(entry_sp) != return_pc:
            self._fail('saved return slot mismatch')
        self.identity = identity
        handle = ReturnHandle(self, ptid, return_pc, entry_sp, callback, args)
        if return_pc not in self.sites:
            tracker = self
            gdb = self.gdb
            dispatcher = self.dispatcher
            class Site(gdb.Breakpoint):
                def stop(self):
                    return dispatcher.defer(tracker._returned, return_pc)
            self.sites[return_pc] = Site('*'+hex(return_pc), internal=True)
        self.handles.append(handle)
        return handle

    def validate(self, snapshot):
        """Check all pending frames at every dispatched stop, before callbacks."""
        if self.failed:
            raise StopError('return tracker already failed')
        if not self.handles:
            return
        if (snapshot['inferior'], snapshot['pid']) != self.identity:
            self._fail('tracked inferior changed')
        rows = {r['ptid']: r['registers'] for r in snapshot['threads']}
        for handle in self.handles:
            row = rows.get(handle.ptid)
            if row is None:
                self._fail('tracked thread disappeared')
            sp, pc = row['sp'], row['pc']
            if sp <= handle.sp:
                if self._slot(handle.sp) != handle.pc:
                    self._fail('saved return slot changed')
            elif (sp, pc) != (handle.sp+8, handle.pc):
                self._fail('tracked return escaped stack bounds')

    def _returned(self, pc):
        snapshot = self.dispatcher.last_snapshot
        ptid = snapshot['selected_ptid']
        row = next(r['registers'] for r in snapshot['threads'] if r['ptid'] == ptid)
        matches = [h for h in self.handles if h.ptid == ptid and
                   h.pc == pc == row['pc'] and h.sp+8 == row['sp']]
        if len(matches) > 1:
            self._fail('ambiguous return frame')
        if not matches:
            return None  # Another thread or deeper invocation shares this site.
        handle = matches[0]
        self.cancel(handle)
        return handle.callback(*handle.args)

    def cancel(self, handle):
        """Cancel outside stop(); remove shared breakpoint only after last user."""
        if handle.owner is not self:
            raise StopError('foreign return handle')
        if not handle.active:
            return
        self.handles.remove(handle)
        handle.active = False
        if not any(h.pc == handle.pc for h in self.handles):
            self.sites.pop(handle.pc).delete()

    def close(self):
        for handle in tuple(self.handles):
            self.cancel(handle)


if __name__ == '__main__':
    print(__doc__)
