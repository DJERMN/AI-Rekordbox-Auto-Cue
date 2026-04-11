import struct, pathlib

def dump_header(label, fpath):
    if not fpath.exists():
        print(f'  NOT FOUND: {fpath.name}')
        return
    data = fpath.read_bytes()
    hdr_len = struct.unpack_from('>I', data, 4)[0]
    total   = struct.unpack_from('>I', data, 8)[0]
    print(f'\n{label}  {fpath.name}  hdr_len={hdr_len}  total={total}')
    print(f'  Header hex: {data[:hdr_len].hex()}')
    # Parse header fields
    print(f'  Bytes 12-15: {data[12:16].hex()}')
    print(f'  Bytes 16-19: {data[16:20].hex()}')
    print(f'  Bytes 20-23: {data[20:24].hex()}')
    print(f'  Bytes 24-27: {data[24:28].hex()}')

base = pathlib.Path(r'C:\Users\baris\AppData\Roaming\Pioneer\rekordbox\share\PIONEER\USBANLZ\f33\62014-8149-44d8-997a-c4b546a3f116\ANLZ0000')
dump_header('CURRENT EXT', base.with_suffix('.EXT'))
dump_header('BACKUP  EXT', base.with_suffix('.EXT.bak'))
dump_header('CURRENT DAT', base.with_suffix('.DAT'))
dump_header('BACKUP  DAT', base.with_suffix('.DAT.bak'))
