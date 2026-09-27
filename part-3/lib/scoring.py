"""Pure probability-clamping helper — no I/O, no dependencies."""


def clamp01(p: float, eps: float = 0.0) -> float:
    """Clamp `p` into [eps, 1 - eps].

    With eps=0 this just guards against tiny out-of-range drift (a price of
    1.0000001).
    """
    lo, hi = eps, 1.0 - eps
    return lo if p < lo else hi if p > hi else p
