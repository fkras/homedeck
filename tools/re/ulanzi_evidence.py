#!/usr/bin/env python3
"""Re-derive the D200 protocol evidence from UlanziDeck.exe.

    pip install pefile capstone
    python tools/re/ulanzi_evidence.py "C:\\Program Files (x86)\\Ulanzi Studio\\UlanziDeck.exe"

Static analysis only - nothing is run or patched. Functions are found by the
strings they reference (MSVC __FUNCSIG__ log strings, log messages), not by
hard-coded addresses, so this keeps working across nearby versions. Prints:

  1. the frame builder and HID writer (magic, header, padding, report ID)
  2. the reader loop (read size, timeout, magic check)
  3. every call that sends a frame, with its command code and protocol ID
  4. how received frames are dispatched
  5. the firmware sleep-time choices

guides/d200-protocol.md walks through the output for Ulanzi Studio 3.3.9.0.
"""
import bisect
import hashlib
import re
import struct
import sys

try:
    import pefile
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs
except ImportError:
    sys.exit('pip install pefile capstone')


class Binary:
    LEA_RIP = re.compile(rb'[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]', re.S)

    def __init__(self, path):
        self.path = path
        self.data = open(path, 'rb').read()
        self.pe = pefile.PE(data=self.data, fast_load=True)
        self.pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXCEPTION'],
            pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
            pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE'],
        ])
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        text = next(s for s in self.pe.sections if s.Name.startswith(b'.text'))
        self.text = text.get_data()
        self.text_va = self.base + text.VirtualAddress
        self.funcs = sorted((self.base + f.struct.BeginAddress, self.base + f.struct.EndAddress)
                            for f in getattr(self.pe, 'DIRECTORY_ENTRY_EXCEPTION', []))
        self.starts = [f[0] for f in self.funcs]
        self.imports = {}
        for dll in getattr(self.pe, 'DIRECTORY_ENTRY_IMPORT', []):
            for imp in dll.imports:
                self.imports[imp.address] = (imp.name or b'?').decode()
        self.cs = Cs(CS_ARCH_X86, CS_MODE_64)
        self._lea = None
        self.names = {}

    def version(self):
        for info in getattr(self.pe, 'FileInfo', None) or []:
            for entry in info:
                for table in getattr(entry, 'StringTable', []):
                    version = table.entries.get(b'ProductVersion')
                    if version:
                        return version.decode()
        return '?'

    def off_to_va(self, off):
        for s in self.pe.sections:
            if s.PointerToRawData <= off < s.PointerToRawData + s.SizeOfRawData:
                return self.base + s.VirtualAddress + off - s.PointerToRawData

    def read(self, va, size):
        return self.pe.get_data(va - self.base, size)

    def cstr(self, va):
        raw = self.read(va, 200)
        return raw.split(b'\0', 1)[0].decode('latin1')

    def lea_index(self):
        if self._lea is None:
            self._lea = {}
            for m in self.LEA_RIP.finditer(self.text):
                src = self.text_va + m.start()
                target = src + 7 + struct.unpack_from('<i', self.text, m.start() + 3)[0]
                self._lea.setdefault(target, []).append(src)
        return self._lea

    def xrefs(self, string):
        index = self.lea_index()
        hits = []
        for m in re.finditer(re.escape(string.encode()), self.data):
            va = self.off_to_va(m.start())
            if va is not None:
                hits += index.get(va, [])
        return sorted(hits)

    def func_of(self, va):
        i = bisect.bisect_right(self.starts, va) - 1
        if i >= 0 and self.funcs[i][0] <= va < self.funcs[i][1]:
            return self.funcs[i]

    def callers(self, target):
        out = []
        i = self.text.find(b'\xe8')
        while i != -1:
            if i + 5 <= len(self.text) and struct.unpack_from('<i', self.text, i + 1)[0] == target - (self.text_va + i + 5):
                out.append(self.text_va + i)
            i = self.text.find(b'\xe8', i + 1)
        return out

    def disasm(self, start, end):
        return list(self.cs.disasm(self.read(start, end - start), start))

    def annotate(self, ins):
        m = re.search(r'\[rip ([+-]) 0x([0-9a-f]+)\]', ins.op_str)
        if m:
            target = ins.address + ins.size + int(m.group(2), 16) * (1 if m.group(1) == '+' else -1)
            if target in self.imports:
                return f'  ; {self.imports[target]}'
            try:
                text = self.cstr(target)
            except Exception:
                return ''
            if len(text) >= 3 and text.isprintable():
                return f'  ; "{text[:70]}"'
        if ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
            name = self.names.get(int(ins.op_str, 16))
            if name:
                return f'  ; -> {name}'
        return ''

    def name_functions(self):
        ''' Name each function after the __FUNCSIG__ string it logs. '''
        sig = re.compile(rb'(?:[\w:<>,\* &]+? )?__cdecl ([\w:~<>`\'\-\{\} ]+)\([^\0]{0,300}?\)(?: const)?\0')
        index = self.lea_index()
        for m in sig.finditer(self.data):
            va = self.off_to_va(m.start())
            for src in index.get(va, []):
                f = self.func_of(src)
                if f:
                    self.names.setdefault(f[0], m.group(1).decode('latin1'))

    def find(self, name):
        return next((va for va, n in self.names.items() if n == name), None)


def show(b, lines, keep=None, limit=60):
    shown = 0
    for ins in lines:
        note = b.annotate(ins)
        text = f'    {ins.address:x}: {ins.mnemonic:6} {ins.op_str}{note}'
        if keep is None or re.search(keep, text):
            print(text)
            shown += 1
            if shown >= limit:
                print('    ...')
                return


def immediates_before(b, call_va):
    ''' (edx, r8d) immediates set up for a call, if constant. '''
    f = b.func_of(call_va)
    ins = b.disasm(f[0], call_va + 5)[-25:]
    regs = {}
    for i in ins:
        m = re.match(r'(dl|edx|r8b|r8d), (0x[0-9a-f]+|\d+)$', i.op_str)
        if i.mnemonic == 'mov' and m:
            regs['edx' if m.group(1) in ('dl', 'edx') else 'r8d'] = int(m.group(2), 0)
        m = re.match(r'(edx|r8d), (edx|r8d)$', i.op_str)
        if i.mnemonic == 'xor' and m and m.group(1) == m.group(2):
            regs[m.group(1)] = 0
    return regs.get('edx'), regs.get('r8d')


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    b = Binary(sys.argv[1])
    print(f'UlanziDeck.exe {b.version()}  sha256 {hashlib.sha256(b.data).hexdigest()}')
    b.name_functions()
    print(f'{len(b.names)} functions named from their log strings\n')

    print('== 1. Frame builder and HID writer ==')
    writer = b.func_of(b.xrefs('Failed to hid_write=')[0])
    builder = b.func_of(b.callers(writer[0])[0])
    print(f'  builder {builder[0]:x}: header = 7c7c, commandCode, protocolID, u32 length')
    show(b, b.disasm(*builder), keep=r'0x7c7c|byte ptr \[rsp \+ 0x2[23]\]|dword ptr \[rsp \+ 0x24\]|malloc')
    print(f'  writer {writer[0]:x}: len + 10 rounded up to 1024, report ID byte, 1024-byte chunks')
    show(b, b.disasm(*writer), keep=r'0xa\]|0x3ff|0x400|0xff$|word ptr \[rcx \+ 8\]|byte ptr \[rsp \+ 0xd0\], 0|cmp +sil, 6|hid_write|\[rbx \+ 1\]|"')

    print('\n== 2. Reader loop ==')
    reader = b.func_of(b.xrefs('readLoop bytesRead=')[0])
    show(b, b.disasm(*reader), keep=r'0x64$|0x400$|0x7c|0xb[0-4]\]|0x6400000|msleep')

    print('\n== 3. Frames sent (commandCode, protocolID) ==')
    for name in ('UlanziDeck::SendDataToDeck', 'UlanziDeck::SendSmallWinInfoToDeck'):
        target = b.find(name)
        print(f'  {name} @ {target:x}')
        for site in b.callers(target):
            cc, pid = immediates_before(b, site)
            caller = b.names.get(b.func_of(site)[0], '(unnamed)')
            fmt = lambda v: '  ?' if v is None else f'0x{v:02x}'
            print(f'    {site:x}  cc={fmt(cc)} pid={fmt(pid)}  in {caller}')
    screen = b.find('UlanziDeck::SendScreenData')
    print(f'  UlanziDeck::SendScreenData @ {screen:x}: protocolID = (bool ^ 1) + 4')
    show(b, b.disasm(screen, screen + 0x1b9), keep=r'xor +r8b, 1|add +r8b, 4')

    print('\n== 4. Received frames ==')
    receive = b.find('UlanziDeck::OnDeckFrameReceived')
    lines = b.disasm(*b.func_of(receive))
    show(b, lines, keep=r'cmp +r15b|cmp +byte ptr \[rdx \+ [23]\]|Keytap|GETBASE|r8b, 0x(a|1a)$|COMMAND_CODE')

    print('\n== 5. Firmware sleep time choices (QButtonGroup ids) ==')
    setting = b.func_of(b.xrefs('rBtn_sleepTime1')[0])
    add_button = [a for a, n in b.imports.items() if 'addButton@QButtonGroup' in n]
    for f in b.funcs:
        if f[1] - f[0] < 0x800:
            continue
        lines = b.disasm(*f)
        ids = []
        for k, ins in enumerate(lines):
            m = re.match(r'qword ptr \[rip \+ (0x[0-9a-f]+)\]', ins.op_str)
            if ins.mnemonic == 'call' and m and ins.address + ins.size + int(m.group(1), 16) in add_button:
                prev = [p.op_str for p in lines[max(0, k - 4):k] if p.op_str.startswith('r8d,')]
                ids.append(prev[-1].split(', ')[1] if prev else '?')
        if any(v in ids for v in ('0x78',)) and '0xf' in ids:
            print(f'    in {f[0]:x}: ids {ids}')
            break
    print(f'    (rBtn_sleepTime1..5 are set up in {setting[0]:x}; ids 5,15,30,60,120 and 0 for rBtn_timeClose)')


if __name__ == '__main__':
    main()
