import struct, pathlib

def dump_sections(fpath):
    if not fpath.exists():
        print(f'  NOT FOUND: {fpath.name}')
        return
    data = fpath.read_bytes()
    hdr_len = struct.unpack_from('>I', data, 4)[0]
    total   = struct.unpack_from('>I', data, 8)[0]
    print(f'  {fpath.name}  disk={len(data)}  declared={total}')
    pos = hdr_len
    while pos < len(data) - 12:
        tag = data[pos:pos+4]
        if tag == b'\x00\x00\x00\x00':
            break
        tlen = struct.unpack_from('>I', data, pos+8)[0]
        if tlen < 12:
            break
        extra = ''
        if tag in (b'PCOB', b'PCO2'):
            typ = struct.unpack_from('>I', data, pos+12)[0]
            cnt_raw = struct.unpack_from('>I', data, pos+16)[0]
            cnt = cnt_raw if tag == b'PCOB' else cnt_raw >> 16
            extra = f'  type={typ} count={cnt}'
        tag_str = tag.decode('ascii', errors='?')
        print(f'    +{pos:05d}  {tag_str:6s}  len={tlen}{extra}')
        pos += tlen

# Spectrum - first track in the playlist run
base = pathlib.Path(r'C:\Users\baris\AppData\Roaming\Pioneer\rekordbox\share\PIONEER\USBANLZ\f33\62014-8149-44d8-997a-c4b546a3f116\ANLZ0000')
print('=== CURRENT ===')
dump_sections(base.with_suffix('.DAT'))
dump_sections(base.with_suffix('.EXT'))
print()
print('=== BACKUP ===')
dump_sections(base.with_suffix('.DAT.bak'))
dump_sections(base.with_suffix('.EXT.bak'))
