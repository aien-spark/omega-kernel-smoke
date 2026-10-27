"""CPU reference for kernel.om, in plain Python.

Every fn in kernel.om needs a function here with the same name and parameters.
Return the exact mathematical answer; the check fails if the kernel disagrees.
Raise Trap(n) where the kernel executes `trap n` (Trap is provided by the check).
"""


def avg(a, b):
    return (a + b) // 2


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def clamp(x, lo, hi):
    if lo > hi:
        raise Trap(3)  # noqa: F821 - provided by tools/cases.py
    return min(max(x, lo), hi)


def popc(x):
    return bin(x).count("1")
