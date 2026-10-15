import ctypes
import sys
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", ctypes.c_wchar * 128),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
]
dxva2.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
dxva2.CapabilitiesRequestAndCapabilitiesReply.argtypes = [
    wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD
]

INPUT_SOURCE_NAMES = {
    0x01: "VGA-1 (Analog 1)",
    0x02: "VGA-2 (Analog 2)",
    0x03: "DVI-1 (Digital 1)",
    0x04: "DVI-2 (Digital 2)",
    0x05: "Composite-1",
    0x06: "Composite-2",
    0x07: "S-Video-1",
    0x08: "S-Video-2",
    0x09: "Tuner-1",
    0x0A: "Tuner-2",
    0x0B: "Tuner-3",
    0x0C: "Component-1",
    0x0D: "Component-2",
    0x0E: "Component-3",
    0x0F: "DisplayPort-1",
    0x10: "DisplayPort-2",
    0x11: "HDMI-1",
    0x12: "HDMI-2",
    0x13: "HDMI-3",
    0x14: "HDMI-4",
    0x1B: "USB-C / Type-C",
}

results = []
index = [0]


def read_vcp(handle, code):
    vt = wintypes.DWORD(0)
    cur = wintypes.DWORD(0)
    mx = wintypes.DWORD(0)
    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(handle, code, ctypes.byref(vt), ctypes.byref(cur), ctypes.byref(mx))
    if not ok:
        return None
    return cur.value, mx.value


def capability_string(handle):
    length = wintypes.DWORD(0)
    if not dxva2.GetCapabilitiesStringLength(handle, ctypes.byref(length)):
        return None, ctypes.get_last_error()
    if length.value == 0:
        return None, -1
    buf = ctypes.create_string_buffer(length.value + 1)
    if not dxva2.CapabilitiesRequestAndCapabilitiesReply(handle, buf, length.value):
        return None, ctypes.get_last_error()
    return buf.value.decode("ascii", "replace"), 0


def callback(hMon, hdc, lprc, data):
    n = wintypes.DWORD(0)
    if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hMon, ctypes.byref(n)) or n.value == 0:
        return True
    arr = (PHYSICAL_MONITOR * n.value)()
    if not dxva2.GetPhysicalMonitorsFromHMONITOR(hMon, n.value, arr):
        return True

    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if user32.GetMonitorInfoW(hMon, ctypes.byref(mi)):
        dev = mi.szDevice
        rect = "%dx%d @ (%d,%d)" % (
            mi.rcMonitor.right - mi.rcMonitor.left,
            mi.rcMonitor.bottom - mi.rcMonitor.top,
            mi.rcMonitor.left,
            mi.rcMonitor.top,
        )
    else:
        dev = "?"
        rect = "?"

    for i in range(n.value):
        pm = arr[i]
        index[0] += 1
        idx = index[0]
        print("=" * 62)
        print("DISPLAY #%d" % idx)
        print("  windows device : %s" % dev)
        print("  resolution     : %s" % rect)
        print("  ddc descriptor : %s" % pm.szPhysicalMonitorDescription)

        r = read_vcp(pm.hPhysicalMonitor, 0x60)
        if r:
            cur, mx = r
            print("  VCP 0x60 current : %d (0x%02X) %s" % (cur, cur, INPUT_SOURCE_NAMES.get(cur, "?")))
            print("  VCP 0x60 maxval  : %d" % mx)
        else:
            print("  VCP 0x60         : READ FAILED")

        b = read_vcp(pm.hPhysicalMonitor, 0x10)
        if b:
            print("  brightness       : %d/%d" % b)

        cap, err = capability_string(pm.hPhysicalMonitor)
        if cap:
            print("  --- MCCS capability string ---")
            print('  ' + cap)
        else:
            print("  capability string: FAILED err=%d" % err)
    return True


cb = MONITORENUMPROC(callback)
user32.EnumDisplayMonitors(None, None, cb, 0)
print("=" * 62)
print("total displays: %d" % index[0])
