# Legacy shim kept for imports in old scripts.
import sys

def warn():
    print("deprecated", file=sys.stderr)
