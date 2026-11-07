# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Jay (Python CLI contributions)
# See NOTICE for upstream attribution and port details.

"""Read-only serial permission and advisory-lock diagnostics."""

import grp
import os
import pwd
import shlex
import stat
from pathlib import Path


def permission_hint(path: str | Path) -> str:
    """Explain permissions using the device's group and this process's identity."""

    try:
        gid = Path(path).stat().st_gid
        group = grp.getgrgid(gid).gr_name
        user = pwd.getpwuid(os.geteuid()).pw_name
        account_groups = os.getgrouplist(user, pwd.getpwuid(os.geteuid()).pw_gid)
    except OSError, KeyError:
        return "cannot determine device group; check device permissions with ebc doctor"
    if gid in {*os.getgroups(), os.getegid()}:
        return (
            f"this process already has group {group}; check device mode, ACLs, "
            "or sandbox restrictions with ebc doctor"
        )
    if gid in account_groups:
        return (
            f"account {user} belongs to {group}, but this process does not; "
            f"start a fresh login session or run `newgrp {shlex.quote(group)}` "
            "and retry in that shell"
        )
    return (
        f"account {user} is not in device group {group}; ask an administrator to run "
        f"`sudo usermod -aG {shlex.quote(group)} {shlex.quote(user)}`, then log out and back in"
    )


def diagnose_device(path: str | Path) -> tuple[str, bool]:
    """Inspect metadata and Linux locks without opening or configuring the port."""

    path = Path(path)
    lines = [f"Device: {path}"]
    try:
        info = path.stat()
    except OSError as exc:
        return "\n".join([*lines, f"Unavailable: {exc.strerror}"]), False
    if not stat.S_ISCHR(info.st_mode):
        return "\n".join([*lines, "Not a character device."]), False
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = str(info.st_gid)
    lines.append(f"Permissions: {stat.filemode(info.st_mode)}; group: {group}")
    accessible = os.access(path, os.R_OK | os.W_OK, effective_ids=True)
    lines.append(f"Read/write access: {'yes' if accessible else 'no'}")
    if not accessible:
        lines.append(permission_hint(path))
    busy = False
    try:
        locks = Path("/proc/locks").read_text().splitlines()
        for lock in locks:
            fields = lock.split()
            if len(fields) < 6 or fields[1] == "->":
                continue
            parts = fields[5].split(":")
            if len(parts) != 3:
                continue
            major, minor, inode = int(parts[0], 16), int(parts[1], 16), int(parts[2])
            if (major, minor, inode) == (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino):
                lines.append(f"Advisory lock: {fields[1]} held by PID {fields[4]}")
                busy = True
        if not busy:
            lines.append("Advisory lock: none visible (does not exclude other serial users)")
    except OSError, ValueError:
        lines.append("Advisory lock: unavailable in this environment")
    lines.append("No port opened or tester commands sent.")
    return "\n".join(lines), accessible and not busy
