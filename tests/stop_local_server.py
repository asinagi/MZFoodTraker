"""Send Ctrl+C only to an isolated console containing the verified server PID."""
import argparse
import ctypes
import json
import os
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pid', type=int)
    parser.add_argument('--console-parent', type=int, help='Verified run.ps1 wrapper sharing this console')
    args = parser.parse_args()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(args.pid):
        raise SystemExit('Cannot attach server console: '+str(ctypes.get_last_error()))
    try:
        members = (ctypes.c_ulong * 64)()
        count = kernel.GetConsoleProcessList(members, 64)
        allowed = {args.pid, os.getpid()}
        if args.console_parent:
            allowed.add(args.console_parent)
        if not count or count > 64 or set(members[:count]) - allowed:
            raise SystemExit('Refusing Ctrl+C in a shared console')
        kernel.SetConsoleCtrlHandler(None, True)
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise SystemExit('Ctrl+C failed: '+str(ctypes.get_last_error()))
        time.sleep(1)
        print(json.dumps({'pid': args.pid, 'ctrl_c_sent': True}))
    finally:
        kernel.FreeConsole()


if __name__ == '__main__':
    main()
