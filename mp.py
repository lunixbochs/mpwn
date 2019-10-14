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

from typing import Any, Callable, Optional, Iterator
import contextlib
import io
import os
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
            while self.write_pos - self.read_pos < size and not self.closed:
                self.condwait(timeout=tout.time_left())
            return self.flush(size)

    def __len__(self):
        with self.cond:
            return self.write_pos - self.read_pos

def _queue_read_loop(self: 'Reader'):
    try:
        while not self.done:
            data = self._read(0x1000)
            if not data:
                print(' <- received EOF')
                break
            hexdump(data, title=' <- received ({:#x}) bytes (buflen={})'.format(len(data), len(self.buf)))
            self.buf.write(data)

            with self.lock:
                sinks = self.sinks.copy()
            for sink in sinks:
                sink(data)
    except ReferenceError: # will be triggered when Reader falls out of scope
        pass
    try:
        self.close()
    except ReferenceError:
        pass

class QueueReader:
    def __init__(self, _read: Callable[[int], bytes]):
        self._read = _read
        self.started = False
        self.done = False
        self.buf = ByteFIFO()
        self.lock = threading.RLock()
        self.start()
        self.sinks = []

    def sink(self, cb: Callable[[bytes], None]) -> None:
        with self.lock:
            self.sinks.append(cb)

    def unsink(self, cb: Callable[[bytes], None]) -> None:
        with self.lock:
            try: self.sinks.remove(cb)
            except ValueError: pass

    def close(self):
        self.done = True
        self.buf.close()

    def start(self) -> None:
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

class Stream:
    reader = None # type: QueueReader
    def __init__(self, codec: 'Codec'=None):
        if codec is None:
            codec = Codec()
        self.codec = codec
        self.interacting = False
        self._data = None # type: Optional[bytes]

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
        if not self.interacting:
            with self.reader.buf.cond: # kinda gross to use this lock but /shrug
                hexdump(data, title=' -> sending ({:#x}) bytes'.format(len(data)))
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
                self._data = self.reader.read(pos)
                return self
            self.reader.wait(tout.time_left())
        raise EOFError('reader closed before expect() found')

    def recvline(self, timeout: float=None) -> bytes:
        self.expect(b'\n', timeout=timeout)
        return self.value

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
    def __init__(self, host: str, port: int, timeout: float=None):
        super().__init__()
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.reader = QueueReader(self.s.recv)

    def _write(self, b: bytes) -> int:
        return self.s.send(b)

    def close(self) -> None:
        try: self.s.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: self.s.close()
        except Exception: pass

class Process(Stream):
    def __init__(self, *args, **kwargs):
        super().__init__()
        for name in ('stdin', 'stdout', 'stderr', 'bufsize'):
            kwargs.pop(name, None)
        self.p = subprocess.Popen(args,
                                  bufsize=0,
                                  stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  **kwargs)
        # TODO: wrap read() with a LoggingReader('process-stdout', args[0], ...?)
        # instead of logging from QueueReader
        self.stdout = QueueReader(self.p.stdout.read)
        self.stderr = QueueReader(self.p.stderr.read)
        self.reader = self.stdout

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
        last = lines[i-1]
        if line[2:-3] == last[2:-3]:
            skip = True
        else:
            if skip:
                line[0] = '+'
            out.append(line[0] + ' '.join(line[1:]))
            skip = False
    return out

### End Data Objects ###

### Helpers ###

codec = Codec()

def process(*argv: str, **kwargs) -> Process:
    return Process(*argv, **kwargs)

def remote(host: str, port: int) -> Socket:
    return Socket(host, port)

def blob(*args) -> Payload:
    b = Payload()
    for a in args:
        b << a
    return b

def  p8(value: int) -> bytes: return  codec.p8(value)
def p16(value: int) -> bytes: return codec.p16(value)
def p32(value: int) -> bytes: return codec.p32(value)
def p64(value: int) -> bytes: return codec.p64(value)

def  u8(value: bytes) -> int: return  codec.u8(value)
def u16(value: bytes) -> int: return codec.u16(value)
def u32(value: bytes) -> int: return codec.u32(value)
def u64(value: bytes) -> int: return codec.u64(value)

### END HELPERS ###
