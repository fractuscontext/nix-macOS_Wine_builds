# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""Let prebuilt DXMT v0.80 create swapchains for windows of other processes.

DXMT's CreateSwapChain (src/d3d11/d3d11_swapchain.cpp) refuses an HWND owned by
another process:

    GetWindowThreadProcessId(hWnd, &window_process_id);
    if (GetProcessId(GetCurrentProcess()) != window_process_id) {
      ERR("CreateSwapChain: cross-process swapchain not supported yet");
      return E_FAIL;
    }

Chromium/CEF (Steam's UI) renders in a GPU process into the browser process's
window, so it always hits this. With patches/wine-latest/0002 winemac can present
there, so the `jne` into the error branch is replaced by a NOP of the same length,
which is what the source-level fix in https://github.com/3Shain/dxmt/issues/141
does. Every file is checked against its known hash and the bytes around the branch;
anything else (a DXMT bump) fails the build instead of corrupting a DLL.

Usage: dxmt-cross-process.py <dxmt-dir>   (patched in place)
"""

import hashlib
import sys
from pathlib import Path

NOP6 = bytes.fromhex("660f1f440000")  # nopw 0x0(%rax,%rax,1)

PATCHES = {
    # path: (sha256 of the v0.80 file, file offset of the jne, bytes before it, the jne)
    "x86_64-windows/d3d11.dll": (
        "7ca382af0eb32d8a432f6efb14d594fefb45673663be1f7e6682254bff885c47",
        0x1A4FF,
        "39442444",  # cmp %eax, 0x44(%rsp)
        "0f85e3000000",  # jne <error branch>
    ),
    "i386-windows/d3d11.dll": (
        "9afc2b3419818618c4c87274435a28935b0df002caa4b9a8d3a88d3dc846b17d",
        0x23CDB,
        "398510ffffff",  # cmp %eax, -0xf0(%ebp)
        "0f8567010000",  # jne <error branch>
    ),
}


def main(root: Path) -> None:
    for rel, (sha, offset, before, jne) in PATCHES.items():
        path = root / rel
        data = bytearray(path.read_bytes())
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            sys.exit(f"{rel}: sha256 {got} is not DXMT v0.80; update {__file__}")
        ctx = bytes.fromhex(before)
        old = bytes.fromhex(jne)
        if data[offset - len(ctx) : offset] != ctx or data[offset : offset + 6] != old:
            sys.exit(f"{rel}: unexpected bytes at {offset:#x}")
        data[offset : offset + 6] = NOP6
        path.write_bytes(data)
        print(f"{rel}: cross-process swapchain check removed at {offset:#x}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
