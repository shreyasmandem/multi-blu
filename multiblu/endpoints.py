"""Windows Core Audio endpoint control.

Enumerates render endpoints, reads/sets the system default output device, and
reads/sets per-endpoint mute. Setting the default device is not exposed by any
public API, so it goes through the undocumented IPolicyConfig COM interface
(present and stable from Windows 7 through 11).
"""

from __future__ import annotations

import ctypes
import difflib
import unicodedata
from ctypes import POINTER, byref, c_int, c_long, c_ulong, c_void_p, c_wchar_p
from dataclasses import dataclass

import comtypes
from comtypes import CLSCTX_ALL, CLSCTX_INPROC_SERVER, GUID
from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
from pycaw.constants import CLSID_MMDeviceEnumerator
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

E_RENDER = 0
DEVICE_STATE_ACTIVE = 0x1

ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS = 0, 1, 2
ALL_ROLES = (ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS)

CLSID_POLICY_CONFIG = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
IID_POLICY_CONFIG = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
# IPolicyConfig vtable: 3 IUnknown slots, then 10 format/period/share/property
# methods, so SetDefaultEndpoint(LPCWSTR deviceId, ERole role) lands at 13.
_SLOT_SET_DEFAULT_ENDPOINT = 13

_BUILTIN_HINTS = ("realtek", "speakers", "display audio", "intel", "nvidia", "amd", "high definition")


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str

    @property
    def looks_builtin(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in _BUILTIN_HINTS)


def com_init() -> None:
    """Initialise COM for the calling thread. Safe to call repeatedly."""
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def _enumerator():
    return comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_INPROC_SERVER
    )


def list_render_endpoints() -> list[Endpoint]:
    """Every active audio output endpoint, in system order."""
    coll = _enumerator().EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
    found = []
    for i in range(coll.GetCount()):
        try:
            info = AudioUtilities.CreateDevice(coll.Item(i))
            if info.id:
                found.append(Endpoint(id=info.id, name=info.FriendlyName or info.id))
        except Exception:
            continue
    return found


def get_default_id(role: int = ROLE_CONSOLE) -> str | None:
    try:
        dev = _enumerator().GetDefaultAudioEndpoint(E_RENDER, role)
        return AudioUtilities.CreateDevice(dev).id
    except Exception:
        return None


def get_default_ids() -> dict[int, str | None]:
    return {role: get_default_id(role) for role in ALL_ROLES}


def set_default_id(device_id: str, roles=ALL_ROLES) -> bool:
    """Make `device_id` the system default output for the given roles."""
    ptr = c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        byref(CLSID_POLICY_CONFIG), None, CLSCTX_ALL, byref(IID_POLICY_CONFIG), byref(ptr)
    )
    if hr != 0 or not ptr:
        return False
    try:
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
        set_default = ctypes.WINFUNCTYPE(c_long, c_void_p, c_wchar_p, c_int)(
            vtbl[_SLOT_SET_DEFAULT_ENDPOINT]
        )
        return all(set_default(ptr, device_id, role) == 0 for role in roles)
    except Exception:
        return False
    finally:
        try:
            vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
            ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(ptr)  # IUnknown::Release
        except Exception:
            pass


def restore_defaults(ids: dict[int, str | None]) -> None:
    for role, dev_id in ids.items():
        if dev_id:
            set_default_id(dev_id, roles=(role,))


def _volume_iface(device_id: str):
    enum = _enumerator()
    try:
        dev = enum.GetDevice(device_id)
    except Exception:
        dev = None
        coll = enum.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        for i in range(coll.GetCount()):
            item = coll.Item(i)
            if AudioUtilities.CreateDevice(item).id == device_id:
                dev = item
                break
        if dev is None:
            raise LookupError(device_id)
    return dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(
        IAudioEndpointVolume
    )


def get_mute(device_id: str) -> bool | None:
    try:
        return bool(_volume_iface(device_id).GetMute())
    except Exception:
        return None


def set_mute(device_id: str, muted: bool) -> bool:
    try:
        _volume_iface(device_id).SetMute(1 if muted else 0, None)
        return True
    except Exception:
        return False


def _normalise(name: str) -> str:
    name = name.replace(" [Loopback]", "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(name.replace("?", "").split()).lower()


def match_index(endpoint_name: str, candidates: dict[int, str]) -> int | None:
    """Map an endpoint friendly name onto a PortAudio device index.

    PortAudio reports the same friendly names but mangles non-ASCII characters
    and truncates long ones, so fall back from exact to prefix to fuzzy match.
    """
    want = _normalise(endpoint_name)
    if not want:
        return None
    norm = {idx: _normalise(nm) for idx, nm in candidates.items()}

    for idx, got in norm.items():
        if got == want:
            return idx
    for idx, got in norm.items():
        if got and (got.startswith(want) or want.startswith(got)):
            return idx

    best, best_score = None, 0.0
    for idx, got in norm.items():
        score = difflib.SequenceMatcher(None, want, got).ratio()
        if score > best_score:
            best, best_score = idx, score
    return best if best_score >= 0.80 else None
