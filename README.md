mpwn
====

Basic usage:

```python
from mp import *

# p = remote('host', port)
p = process('./exe_name')

# basic io:
p.sendline('hello world')
print(p.recvline())
p.send('hi')
print(p.recv(5))

# directly connect your tty input/output to the target
p.interact()

# wait for a prompt and send a command:
p.expect('$ ')
p.sendline('command')

# concise syntax
# p << 'foo' # is p.send('foo')
# p >> 'bar' # is p.expect('bar')
# after you p >> 'bar', the received text up to your marker is available (once) as `p.data`
p << 'foo'
p >> 'bar'
data = p.data

p >> '$ ' # expect a prompt
p << 'send some text\n'

# chained syntax
p >> '$ ' << 'ls\n'
p >> '$ ' << 'cat flag\n'

# these all send '0', usually no fiddling with python3 bytes encoding/decoding
p << 0 << '0' << str(0) << b'0'

u64(b'\x00' * 8) == 0          # unpack a 64-bit int (can u8, u16, u32, u64)
p64(0x00000000) == b'\x00' * 8 # pack a 64-bit int   (can p8, p16, p32, p64)
```
