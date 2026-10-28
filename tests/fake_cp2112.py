"""
Fake CP2112 + register-level chip models, used by the tests in place of the
real 'hid' module. Tests put chip models into DEVICES[address].
"""
import struct
def crc8(data, init=0xFF):
    c=init
    for b in data:
        c^=b
        for _ in range(8): c=((c<<1)^0x31)&0xFF if c&0x80 else (c<<1)&0xFF
    return c
def sw(*words):
    out=[]
    for w in words: out+=[w>>8,w&0xFF,crc8([w>>8,w&0xFF])]
    return out
class Reg:
    """pointer-based register chip; regs[ptr] = list of bytes returned from that pointer"""
    def __init__(s, regs): s.regs={k:list(v) for k,v in regs.items()}; s.p=0; s.log=[]
    def write(s,d):
        s.log.append(list(d)); s.p=d[0]
        if len(d)>1: s.on_write(d[0], list(d[1:]))
        return True
    def on_write(s,reg,data): s.regs[reg]=data+s.regs.get(reg,[])[len(data):]
    def read(s,n):
        v=s.regs.get(s.p,[0]*n); return (v+[0]*n)[:n]
class AHT(Reg):
    def __init__(s): super().__init__({}); 
    def write(s,d): return True
    def read(s,n):
        rh, rt = 524288, 393216   # 50 %, 25 C
        return [0x18, rh>>12, (rh>>4)&0xFF, ((rh&0xF)<<4)|(rt>>16), (rt>>8)&0xFF, rt&0xFF, 0][:n]
class SHT3x(Reg):
    def __init__(s): super().__init__({}); s.cmd=None
    def write(s,d): s.cmd=tuple(d); return True
    def read(s,n):
        if s.cmd!=(0x24,0x00): return None
        t=round((22.5+45)*65535/175); h=round(55.0*65535/100)
        return sw(t,h)[:n]
class HTU(Reg):
    def __init__(s): super().__init__({}); s.cmd=None
    def write(s,d): s.cmd=d[0]; return True
    def read(s,n):
        if s.cmd==0xF3: w=round((21.0+46.85)*65536/175.72)&0xFFFC
        elif s.cmd==0xF5: w=round((40.0+6)*65536/125)&0xFFFC
        else: return [0x02]*n
        return [w>>8,w&0xFF,crc8([w>>8,w&0xFF],0x00)][:n]
class PCF(Reg):
    def __init__(s): super().__init__({}); s.port=0xFF
    def write(s,d): s.port=d[-1]; return True
    def read(s,n): return [s.port]*n
class TCA(PCF):
    pass


class EEP(Reg):
    def __init__(s): super().__init__({}); s.mem=list(b"Hello EEPROM 24C32"+bytes(100)); s.a=0
    def write(s,d): s.a=(d[0]<<8)|d[1] if len(d)>=2 else d[0]; return True
    def read(s,n): return s.mem[s.a:s.a+n]
class BMP180(Reg):
    def __init__(s):
        cal=struct.pack(">hhhHHHhhhhh",408,-72,-14383,32741,32757,23153,6190,4,-32768,-8711,2868)
        super().__init__({0xAA:list(cal),0xD0:[0x55]})
    def on_write(s,reg,data):
        if reg==0xF4:
            s.regs[0xF6]=[27898>>8,27898&0xFF] if data[0]==0x2E else [23843>>8,23843&0xFF,0]
class BMP280(Reg):
    def __init__(s):
        cal=struct.pack("<HhhHhhhhhhhh",27504,26435,-1000,36477,-10685,3024,2855,140,-7,15500,-14600,6000)
        r={0x88+i:list(cal[i:]) for i in range(24)}; r[0xD0]=[0x58]; r[0xF3]=[0]
        aP,aT=415148,519888
        r[0xF7]=[aP>>12,(aP>>4)&0xFF,(aP&0xF)<<4,aT>>12,(aT>>4)&0xFF,(aT&0xF)<<4]
        super().__init__(r)
class ADS(Reg):
    V=[1.0,2.5,0.0,3.3]
    def __init__(s): super().__init__({0x01:[0x85,0x83],0x00:[0,0]})
    def on_write(s,reg,data):
        if reg==0x01:
            cfg=(data[0]<<8)|data[1]; ch=((cfg>>12)&7)-4; raw=int(s.V[ch]/4.096*32768)
            s.regs[0x00]=[(raw>>8)&0xFF,raw&0xFF]; s.regs[0x01]=[data[0]|0x80,data[1]]
class SBS(Reg):
    def __init__(s):
        w=lambda v: [v&0xFF,(v>>8)&0xFF]
        blk=lambda t: [len(t)]+list(t.encode())
        super().__init__({0x09:w(16200),0x0A:w(0xFFFF-499),0x0D:w(87),0x08:w(2981),0x0F:w(1740),
            0x10:w(2000),0x18:w(2500),0x17:w(143),0x20:blk("SANYO"),0x21:blk("DELL 4P894"),0x22:blk("LION")})
class SCD(Reg):
    def __init__(s): super().__init__({}); s.cmd=None; s.started=False
    def write(s,d):
        s.cmd=tuple(d)
        if s.cmd==(0x21,0xB1): s.started=True
        return True
    def read(s,n):
        c=s.cmd; s.cmd=None
        if c==(0x36,0x82): return sw(0x1234,0x5678,0x9ABC)
        if c==(0xE4,0xB8): return sw(0x8006 if s.started else 0x8000)
        if c==(0xEC,0x05): return sw(650, int((21.0+45)*65536/175), int(45.0*65536/100))
        return None
def u16(v): return [v>>8&0xFF,v&0xFF]
DEVICES = {}


class device:
    def open(s,v,p): s.q=[]
    def close(s): pass
    def send_feature_report(s,r): pass
    def get_feature_report(s,i,n): return [i,0x0C,2][:n] if i==5 else [i]+[0]*(n-1)
    def write(s,d):
        rid=d[0]
        if rid in (0x10,0x11,0x14):
            a=d[1]>>1; m=DEVICES.get(a); s.data=None; s.ok=False
            if m is not None:
                if rid==0x14: s.ok=m.write(list(d[3:3+d[2]]))
                elif rid==0x11:
                    m.write(list(d[5:5+d[4]])); n=(d[2]<<8)|d[3]; r=m.read(n); s.ok=r is not None; s.data=r
                else:
                    n=(d[2]<<8)|d[3]; r=m.read(n); s.ok=r is not None; s.data=r
        elif rid==0x15:
            s.q.append([0x16,0x02 if s.ok else 0x03,0x05 if s.ok else 0x00,0,0,0,1])
        elif rid==0x12:
            n=(d[1]<<8)|d[2]; data=(s.data or [])[:61]
            s.q.append([0x13,0x02,len(data)]+data+[0]*(61-len(data)))
            s.data=(s.data or [])[61:]
    def read(s,n,t): return s.q.pop(0) if s.q else []
