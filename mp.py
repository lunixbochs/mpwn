## this is mp.py from mpwn, a single file framework which lives at:
## https://github.com/lunxbochs/mpwn
#
#########
#
# Copyright (c) 2019 Ryan Hileman
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from typing import Any, Callable, Optional, Iterator, List, Tuple, Sequence
from typing import cast
import argparse
import asyncio
import atexit
import contextlib
import inspect
import io
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import weakref

### Streams ###

try:
    import termios
    import tty
    has_tty = True

    @contextlib.contextmanager
    def tty_interact(fd: int=0) -> Iterator[None]:
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            yield
        except KeyboardInterrupt:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

except ImportError:
    has_tty = False

    # windows doesn't get raw terminals (yet?)
    @contextlib.contextmanager
    def tty_interact() -> Iterator[None]:
        try:
            yield
        except KeyboardInterrupt:
            pass

class Timeout:
    def __init__(self, timeout: float=None):
        self.never = True
        self.expires = 0.0
        if timeout is not None:
            self.never = False
            self.expires = time.monotonic() + timeout
        self.timeout = timeout

    def check(self) -> bool:
        return self.never or time.monotonic() < self.expires

    def assert_ok(self) -> bool:
        if time.monotonic() < self.expires:
            raise TimeoutError('{:.3f}s timeout reached'.format(self.timeout))
        return True

    def time_left(self) -> Optional[float]:
        if self.never:
            return None
        diff = self.expires - time.monotonic()
        if diff < 0:
            raise TimeoutError('{:.3f}s timeout reached'.format(self.timeout))
        return diff

class Scanf:
    """
    (%d int) (%f float) (%o octal) (%x hex) (%s string / bytes)
    %5d %4s, etc all limit the match length to N instead of breaking on whitespace (length is ignored on %f float)

    For example: scanf("%d hello %4s %s", "4 hello blah anything") would return (4, "blah", "anything")
    """
    ESCAPES = re.compile(r'(%%|%(?P<len>\d*)(?P<fmtc>[dfoxs])|(?P<text>.+?))')
    TABLE = {'d': '\d', 'f': '\d+(\.\d+)?', 'o': '[0-7]', 'x': '[0-9a-fA-F]', 's': '\S'}
    CASTS = {'d': int, 'f': float, 'o': lambda x: int(x, 8), 'x': lambda x: int(x, 16), 's': lambda x: x}
    def __init__(self, fmt: str):
        self.casts = [] # type: List[Callable]
        patterns = [] # type: List[str]
        i = 0
        for match in self.ESCAPES.finditer(fmt):
            d = match.groupdict()
            if d['fmtc']:
                if d['len']:
                    length = '{1,%d}' % int(d['len'])
                else:
                    length = '+'
                if d['fmtc'] == 'f': # float can't be repeated
                    length = ''
                reg = '(?P<g{}>{}{})'.format(i, self.TABLE[d['fmtc']], length)
                i += 1
                patterns.append(reg)
                self.casts.append(self.CASTS[d['fmtc']])
            else:
                if d['text'] == ' ':
                    patterns.append(r'\s+')
                else:
                    patterns.append(re.escape(d['text']))
        patterns = [r'^\s*'] + patterns
        self.regex = re.compile(''.join(patterns))

    def match(self, s: str) -> Sequence[Any]:
        if isinstance(s, bytes):
            s = s.decode('utf8', 'replace')
        match = self.regex.match(s)
        return [self.casts[int(k[1:])](v) for k, v in match.groupdict().items()]

    # print(Scanf('%s test %f test %o test %x test %s test %4d%4d').match(' blah test 0.1 test 0777 test 0bfe test qqqqqq test 12345678 '))

class ByteFIFO:
    def __init__(self):
        self.buffer = io.BytesIO()
        self.cond = threading.Condition()
        self.read_pos = 0
        self.write_pos = 0
        self.closed = False

    def getbuffer(self):
        b = self.buffer.getbuffer()
        # little dance to make sure parent buffer is released even in non-refcount envs like pypy
        view = b[self.read_pos:self.write_pos]
        b.release()
        return view

    def peek(self, size: int=-1) -> bytes:
        with self.cond, self.getbuffer() as b:
            return bytes(b[:size])

    def find(self, needle: bytes) -> int:
        with self.cond, self.getbuffer() as b:
            return bytes(b).find(needle)

    def flush(self, size: int=-1) -> bytes:
        size = int(size)
        with self.cond:
            self.buffer.seek(self.read_pos, io.SEEK_SET)
            if size < 0:
                size = len(self)
            data = self.buffer.read(size)
            self.read_pos += len(data)
            # collapse buffer if size > 2MB and read_pos > 1MB
            if len(self) > 0x2000000 and self.read_pos > 0x1000000:
                newbuf = io.BytesIO(self.buffer.getbuffer()[self.read_pos:self.write_pos])
                self.buffer = newbuf
                self.write_pos -= self.read_pos
                self.read_pos = 0
            # reset to head of buffer if the read head reaches the write head
            elif self.read_pos == self.write_pos:
                self.read_pos = self.write_pos = 0
            assert(self.read_pos <= self.write_pos)
            return data

    def write(self, data: bytes):
        if self.closed:
            raise ValueError('I/O operation on closed buffer.')
        with self.cond:
            self.buffer.seek(self.write_pos, io.SEEK_SET)
            self.buffer.write(data)
            self.write_pos += len(data)
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def condwait(self, timeout: float) -> None:
        try:
            self.cond.wait(timeout=timeout)
        except KeyboardInterrupt:
            sys.exit(0)

    def wait(self, timeout: float=None) -> None:
        tout = Timeout(timeout)
        with self.cond:
            if self.closed and not len(self):
                raise ValueError('I/O operation on closed buffer.')
            while self.read_pos == self.write_pos and not self.closed:
                self.condwait(timeout=tout.time_left())

    def readany(self, size: int=-1, timeout: float=None) -> bytes:
        tout = Timeout(timeout)
        with self.cond:
            if self.closed and not len(self):
                raise ValueError('I/O operation on closed buffer.')
            while self.write_pos == self.read_pos and not self.closed:
                self.condwait(timeout=tout.time_left())
            return self.flush(size)

    def readall(self, size: int=-1, timeout: float=None) -> bytes:
        tout = Timeout(timeout)
        with self.cond:
            if self.closed and not len(self):
                raise ValueError('I/O operation on closed buffer.')
            while (size < 0 or self.write_pos - self.read_pos < size) and not self.closed:
                self.condwait(timeout=tout.time_left())
            return self.flush(size)

    def __len__(self):
        with self.cond:
            return max(self.write_pos - self.read_pos, 0)

def _queue_read_loop(self: 'Reader'):
    try:
        while not self.done:
            data = self._read(0x1000)
            if not data:
                print(' <- received EOF')
                break
            self.buf.write(data)

            with self.lock:
                sinks = self.sinks.copy()
            for ref in sinks:
                sink = ref()
                if sink:
                    sink(data)
    except ReferenceError: # will be triggered when Reader falls out of scope
        pass
    try:
        self.close()
    except ReferenceError:
        pass

byte_src_fn = Callable[[int], bytes]
byte_sink_fn = Callable[[bytes], int]

def weak_callable(cb: Callable) -> 'weakref.ReferenceType[Callable]':
    if inspect.ismethod(cb):
        ref = weakref.WeakMethod(cb)
    else:
        ref = weakref.ref(cb)
    return ref

class QueueReader:
    def __init__(self, _read: byte_src_fn):
        self._read = _read
        self.started = False
        self.done = False
        self.buf = ByteFIFO()
        self.lock = threading.RLock()
        self.start()
        self.sinks = [] # type: List[weakref.ReferenceType[byte_sink_fn]]

    def sink(self, cb: byte_sink_fn) -> None:
        ref = weak_callable(cb)
        with self.lock:
            self.sinks.append(ref)

    def unsink(self, cb: byte_sink_fn) -> None:
        ref = weak_callable(cb)
        with self.lock:
            try: self.sinks.remove(ref)
            except ValueError: pass

    def close(self):
        self.done = True
        self.buf.close()

    def close_exit(self):
        self.done = True
        try:
            self.wait(0.1)
        except (TimeoutError, ValueError):
            pass
        self.buf.close()

    def start(self) -> None:
        atexit.register(self.close_exit)
        with self.lock:
            if not self.started:
                self.started = True
                # weakref loop keeps thread from keeping Reader alive
                threading.Thread(target=_queue_read_loop, args=(weakref.proxy(self),), daemon=True).start()

    def peek(self, size: int=-1) -> bytes:
        return self.buf.peek(size)

    def find(self, needle: bytes) -> int:
        return self.buf.find(needle)

    def read(self, size: int=-1, timeout: float=None) -> bytes:
        return self.buf.readany(size, timeout)

    def readall(self, size: int=-1, timeout: float=None) -> bytes:
        return self.buf.readall(size, timeout)

    def wait(self, timeout: float=None) -> None:
        self.buf.wait(timeout)

    def flush(self, size: int=-1) -> bytes:
        return self.buf.flush(size)


notify_fn = Callable[['Stream', bytes, bool, str], None]

class Stream:
    reader = None # type: QueueReader
    def __init__(self, codec: 'Codec'=None, manager: 'Manager'=None):
        if codec is None:
            codec = Codec()
        if manager is None:
            manager = global_manager
        self.codec = codec
        self.interacting = False
        self._data = None # type: Optional[bytes]
        self.sinks = [] # type: List[weakref.ReferenceType[notify_fn]]
        self.lock = threading.RLock()
        manager.register(self)

    def sink(self, cb: notify_fn) -> None:
        ref = weak_callable(cb)
        with self.lock:
            self.sinks.append(ref)

    def unsink(self, cb: notify_fn) -> None:
        ref = weak_callable(cb)
        with self.lock:
            try: self.sinks.remove(ref)
            except ValueError: pass

    def on_recv(self, data: str, name: str='') -> None:
        with self.lock:
            sinks = self.sinks
        for ref in sinks:
            sink = ref()
            if sink:
                sink(self, data, False, name=name)

    def on_send(self, data: str, name: str='') -> None:
        with self.lock:
            sinks = self.sinks.copy()
        for ref in sinks:
            sink = ref()
            if sink:
                sink(self, data, True, name=name)

    @property
    def data(self) -> bytes:
        if self._data is None:
            raise ValueError('no expect data available, remember to save p.data immediately after the p.expect() or >> if you want to keep it')
        data = self._data
        self._data = None
        return data

    def read(self, size: int=-1, timeout: float=None) -> bytes:
        return self.reader.read(size, timeout)

    def readall(self, size: int=-1, timeout: float=None) -> bytes:
        return self.reader.readall(size, timeout)

    # TODO: timeout?
    def write(self, data: Any) -> 'Stream':
        if isinstance(data, int):
            data = str(data)
        data = self.codec.tobytes(data)
        self.on_send(data)
        view = memoryview(data)
        while len(view) > 0:
            n = self._write(view)
            if n <= 0:
                raise EOFError()
            view = view[n:]
        return self

    def _write(self, data: bytes) -> int:
        raise NotImplementedError

    ## helpers

    def interact(self) -> None:
        try:
            self.interacting = True
            self.reader.sink(self._interact_output)
            sys.stdout.write('\n$ ')
            with tty_interact():
                c = b'\x00'
                while c:
                    c = sys.stdin.read(1)
                    self.send(c)
                    if has_tty:
                        sys.stdout.write(c)
                        sys.stdout.flush()
        finally:
            self.interacting = False
            self.reader.unsink(self._interact_output)
    interactive = interact # for pwntools users

    def _interact_output(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def finish(self, timeout: float=None) -> None:
        tout = Timeout(timeout)
        while not self.reader.done:
            data = self.read(timeout=tout.time_left())
            if data:
                print(repr(data))

    def expect(self, target: Any, timeout: float=None) -> 'Stream':
        target = self.codec.tobytes(target)
        self._data = b''
        if not target:
            return self
        tout = Timeout(timeout)
        while not self.reader.done:
            pos = self.reader.find(target)
            if pos >= 0:
                self._data = self.reader.read(pos + len(target))
                return self
            self.reader.wait(tout.time_left())
        raise EOFError('reader closed before expect() found')

    def scan(self, fmt: str) -> Sequence[Any]:
        return Scanf(fmt).match(self.data)

    def recvline(self, timeout: float=None) -> bytes:
        self.expect(b'\n', timeout=timeout)
        return self.data

    def sendline(self, data: Any=b'') -> 'Stream':
        data = self.codec.tobytes(data)
        self.write(data + b'\n')
        return self

    send = write
    recv = read
    recvall = readall

    def __lshift__(self, other: Any):
        self.send(other)
        return self

    def __rshift__(self, other: Any):
        self.expect(other)
        return self

class Socket(Stream):
    def __init__(self, host: str, port: int, timeout: float=None, manager: 'Manager'=None):
        super().__init__(manager=manager)
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.reader = QueueReader(self.s.recv)
        self.reader.sink(self.on_recv)

    def eof(self) -> None:
        self.s.shutdown(socket.SHUT_WR)

    def _write(self, b: bytes) -> int:
        return self.s.send(b)

    def close(self) -> None:
        try: self.s.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: self.s.close()
        except Exception: pass

class Process(Stream):
    def __init__(self, *args, **kwargs):
        gdbinit = kwargs.pop('gdb', '')
        super().__init__(manager=kwargs.pop('manager', None))
        for name in ('stdin', 'stdout', 'stderr', 'bufsize'):
            kwargs.pop(name, None)
        self.p = subprocess.Popen(args,
                                  bufsize=0,
                                  stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  **kwargs)

        script = sys.argv[0]
        pidp     = '.{}.pid'.format(script)
        gdbinitp = '.{}.gdbinit'.format(script)
        with open(gdbinitp, 'w') as f:
            f.write('{}\n'.format(gdbinit.strip()))
        with open(pidp, 'w') as f:
            f.write('{}\n'.format(self.p.pid))

        # TODO: wrap read() with a LoggingReader('process-stdout', args[0], ...?)
        # instead of logging from QueueReader
        self.stdout = QueueReader(self.p.stdout.read)
        self.stderr = QueueReader(self.p.stderr.read)
        self.reader = self.stdout

        # need to keep references here because the target uses weakrefs
        self.on_stdout = lambda data: self.on_recv(data, name='stdout')
        self.on_stderr = lambda data: self.on_recv(data, name='stderr')
        self.stdout.sink(self.on_stdout)
        self.stderr.sink(self.on_stderr)

    def eof(self) -> None:
        self.p.stdin.close()

    def read(self, size=-1, timeout=None):
        return self.stdout.read(size, timeout)

    def _write(self, data: bytes) -> int:
        try:
            return self.p.stdin.write(data)
        except BrokenPipeError:
            print(' -> pipe closed')
            return 0

    def close(self) -> None:
        self.p.kill()
        del self.reader

### End Streams ###

### Data Objects ###

from typing import Any, Union, List
import io
import struct

class Codec:
    def __init__(self, order='@', bits=0):
        if order not in '@=<>!':
            raise RuntimeError('unsupported byte order {}'.format(order))

        if   bits <= 0:  sz = 'n'
        elif bits == 8:  sz = 'b'
        elif bits == 16: sz = 'h'
        elif bits == 32: sz = 'i'
        elif bits == 64: sz = 'q'
        else: raise RuntimeError('unsupported bit size {}'.format(bits))
        self.order = order
        self.bits = bits

        self.ptr = struct.Struct(order + sz.upper())
        self.s8  = struct.Struct(order + 'B')
        self.s16 = struct.Struct(order + 'H')
        self.s32 = struct.Struct(order + 'I')
        self.s64 = struct.Struct(order + 'Q')
        self.psz = len(self.ptr.pack(0))
        self.bits = self.psz * 8
        self.pmask = (1 << self.bits) - 1

    def tobytes(self, value: Any) -> bytes:
        if isinstance(value, Payload):
            return value.buf.getvalue()
        elif isinstance(value, int):
            return self.p(value)
        elif isinstance(value, str):
            return value.encode('utf8')
        elif isinstance(value, bytes):
            return value
        return str(value).encode('utf8')

    def   p(self, val: int) -> bytes: return self.ptr.pack(val & self.pmask)
    def  p8(self, val: int) -> bytes: return self. s8.pack(val & 0xff)
    def p16(self, val: int) -> bytes: return self.s16.pack(val & 0xffff)
    def p32(self, val: int) -> bytes: return self.s32.pack(val & 0xffffffff)
    def p64(self, val: int) -> bytes: return self.s64.pack(val & 0xffffffffffffffff)

    def   u(self, buf: bytes) -> int: return self.ptr.unpack(buf)[0]
    def  u8(self, buf: bytes) -> int: return self.s8.unpack(buf)[0]
    def u16(self, buf: bytes) -> int: return self.s16.unpack(buf)[0]
    def u32(self, buf: bytes) -> int: return self.s32.unpack(buf)[0]
    def u64(self, buf: bytes) -> int: return self.s64.unpack(buf)[0]

    def __repr__(self) -> str:
        return 'Codec(order={}, bits={})'.format(self.order, self.bits)

class Payload:
    def __init__(self, codec: Codec=None):
        if codec is not None:
            self.codec = codec
        else:
            self.codec = Codec()
        self.buf = io.BytesIO()

    def copy(self) -> 'Payload':
        new = Payload(self.codec)
        new.buf.write(self.buf.getvalue())
        return new

    def write(self, b: bytes) -> 'Payload':
        self.buf.write(b)
        return self

    def __iadd__(self, other: Any) -> None:
        self.write(self.codec.tobytes(other))
        return self

    def __add__(self, other: Any) -> 'Payload':
        new = self.copy()
        new += other
        return new

    def __lshift__(self, other: Any) -> 'Payload':
        self.write(self.codec.tobytes(other))
        return self

    def bytes(self) -> bytes:
        return self.buf.getvalue()

    def __repr__(self):
        return repr(self.buf.getvalue())

    def __str__(self):
        return hexstr(self.buf.getvalue())

def hex_clean(block: bytes) -> str:
    return ''.join([chr(b) if 0x20 <= b < 0x7f else '.'
                    for b in block])

def hexdump(*args, **kwargs):
    title = kwargs.pop('title', None)
    if title is None:
        stack = traceback.extract_stack(None)
        if stack:
            path, line, mod, code = list(stack)[-2]
            filename = os.path.basename(path)
            title = ' {}:{} | {}'.format(filename, line, code)
        else:
            title = ' [hexdump]'
    print(title)
    print(hexstr(*args, **kwargs))

def hexstr(*args, **kwargs):
    return '\n'.join(hexlines(*args, **kwargs))

def hexlines(data: bytes, addr: int=0, codec: Codec=None, color: bool=True, fancy: bool=True, width: int=100):
    if isinstance(data, str):
        data = data.encode('utf8')
    if codec is None:
        codec = Codec()

    bsz = codec.psz
    addr_width = bsz*2 + 4
    hex_fmt = '0x{:0%dx} ' % (bsz*2)
    pad_block = ' ' * (bsz*2)
    pad_tail  = ' ' * (bsz)

    block_count = ((width - addr_width) * 3 // 4) // ((bsz + 1) * 2)
    line_size = block_count * bsz

    lines = [] # type: List[List[str]]
    blocks = [''] * block_count
    tail   = [''] * block_count

    view = memoryview(data)
    for i in range(0, len(data), line_size):
        mem_line = view[i:]
        for j in range(block_count):
            if j * bsz < len(mem_line):
                end = (j + 1) * bsz
                block = bytes(mem_line[j*bsz:end])
                tail[j] = hex_clean(block).ljust(bsz, '.')
                blocks[j] = block.hex().ljust(bsz*2, '.')
            else:
                blocks[j] = pad_block
                tail[j] = pad_tail
        line_parts  = [' ', hex_fmt.format(addr + i)]
        line_parts += blocks
        line_parts += ['[', ' '.join(tail), ']']
        lines.append(line_parts)

    skip = False
    out = [] # type: List[str]
    if lines:
        first = lines[0]
        out.append(first[0] + ' '.join(first[1:]))
    for i, line in enumerate(lines[1:]):
        last = lines[i]
        if line[2:-3] == last[2:-3]:
            skip = True
        else:
            if skip:
                line[0] = '+'
            out.append(line[0] + ' '.join(line[1:]))
            skip = False
    return out

### End Data Objects ###

async def watch_file(path: str) -> None:
    try:
        old_st = os.stat(path)
    except FileNotFoundError:
        old_st = None
    while True:
        await asyncio.sleep(0.050)
        try:
            st = os.stat(path)
            if not old_st or (st.st_mtime, st.st_ino, st.st_size) != (old_st.st_mtime, old_st.st_ino, old_st.st_size):
                return
        except FileNotFoundError:
            pass

def safe_readf(path: str, *, default: str='') -> str:
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return default

async def autorun_main(argv: List[str]) -> None:
    script = argv[0]
    while True:
        print('[+] Launching: {} {}'.format(sys.executable, ' '.join(argv)))
        done = pending = ()
        p = await asyncio.create_subprocess_exec(sys.executable, *argv)
        f_task = asyncio.create_task(watch_file(script))
        p_task = asyncio.create_task(p.wait())
        pending = {f_task, p_task}
        while f_task in pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if not p_task.done():
            p.terminate()

async def gdb_main(argv: List[str]) -> None:
    script = argv[0]
    pidp     = '.{}.pid'.format(script)
    gdbinitp = '.{}.gdb.init'.format(script)
    gdbsyncp = '.{}.gdb.ready'.format(script)
    while True:
        pid = int(safe_readf(pidp) or '0') or None
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid = None
        if not pid:
            print('[-] waiting for target')
            await watch_file(pidp)
            continue
        print('[+] launching gdb')
        print('gdb got pid', pid)
        await asyncio.sleep(2)
        continue

class Manager:
    def __init__(self):
        self._codec = Codec()
        self.verbose = False
        self.hexdump = False

    def main(self, argv: List[str]):
        if len(argv) > 1:
            sub_argv = [sys.argv[0]] + sys.argv[2:]
            if argv[1] == 'auto':
                asyncio.run(autorun_main(sub_argv))
                sys.exit(0)
                return
            if argv[1] == 'gdb':
                asyncio.run(gdb_main(sub_argv))
                sys.exit(0)
                return
        parser = argparse.ArgumentParser()
        parser.add_argument('args', help='arguments to script', type=str, nargs='*')
        parser.add_argument('-v', help='print verbose output',  action='store_true')
        parser.add_argument('-x', help='hexdump debug output',  action='store_true')
        args = parser.parse_args()
        self.verbose = args.v
        self.hexdump = args.x
        sys.argv[:] = [sys.argv[0]] + args.args

    def register(self, stream: 'Stream') -> None:
        stream.sink(self.on_data)

    def unregister(self, stream: 'Stream') -> None:
        stream.unsink(self.on_data)

    def on_data(self, stream: 'Stream', data: bytes, outgoing: bool, name: str='') -> None:
        if not self.verbose:
            return
        if outgoing and stream.interacting:
            return
        if outgoing:
            title = ' -> send ({:#x}) bytes'.format(len(data))
        else:
            title = ' <- recv ({:#x}) bytes'.format(len(data))
        if name:
            title += ' ({})'.format(name)
        if self.hexdump:
            hexdump(data, title=title)
        else:
            try:
                text = data.decode('utf8', 'replace')
                print(title)
                for line in text.split('\n'):
                    print(' ', line)
            except UnicodeEncodeError:
                hexdump(data, title=title)

    @property
    def codec(self) -> Codec:
        return self._codec
    @codec.setter
    def codec(self, codec: str) -> None:
        if not (isinstance(codec, str) and len(codec) >= 2 and codec[1:].isdigit()):
            raise ValueError('codec must be a string such as "<64" or ">32" representing the byte order and bit size')
        order = codec[0]
        bits = int(codec[1:])
        self._codec = Codec(order, bits)

### Helpers ###

global_manager = mpwn = Manager()

def process(*argv: str, **kwargs) -> Process:
    return Process(*argv, **kwargs)

def remote(host: str, port: int) -> Socket:
    return Socket(host, port)

def blob(*args) -> Payload:
    b = Payload()
    for a in args:
        b << a
    return b

def  p8(value: int) -> bytes: return global_manager._codec. p8(value)
def p16(value: int) -> bytes: return global_manager._codec.p16(value)
def p32(value: int) -> bytes: return global_manager._codec.p32(value)
def p64(value: int) -> bytes: return global_manager._codec.p64(value)

def  u8(value: bytes) -> int: return global_manager._codec. u8(value)
def u16(value: bytes) -> int: return global_manager._codec.u16(value)
def u32(value: bytes) -> int: return global_manager._codec.u32(value)
def u64(value: bytes) -> int: return global_manager._codec.u64(value)

### END HELPERS ###

mpwn.main(sys.argv)
