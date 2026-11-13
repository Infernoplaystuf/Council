"""
physics_kit.antennas — wire antennas from a thin-wire method-of-moments
solver (the same family of method as NEC, smaller: parallel straight wires in
free space).

    wavelength(f)                       m
    Wire(x, z_center, length, radius)   one straight element along z, at x
    solve(wires, f, feed=0)             -> Solution: current on every segment,
                                           input impedance, directivity, pattern
    dipole(f, length, radius)           one centre-fed element
    resonant_dipole_length(f, radius)   the length whose reactance is zero
    monopole(f, length, radius)         over a perfect ground plane (image theory)
    yagi(f, lengths, positions, radius) a reflector, driven element, directors
    yagi_design(f, elements, radius)    a starting design, driven element trimmed
                                        to resonance
    swr(z, z0), swr_bandwidth(...)      matching to the feed line
    quarter_wave_stub(f, velocity_factor)

HOW IT WORKS (Harrington, Field Computation by Moment Methods, 1968, ch. 4)
Each wire is cut into segments; the current is sampled at the nodes
between them (zero at the ends), the charge on each segment follows from
the current's step across it, and the tangential electric field
is made zero at every segment's centre except the feed (a 1 V delta gap).
The integrals of the reduced kernel e^{-jkR}/(4 pi R) are done with the 1/R
part exact and the rest by Gauss-Legendre, so short segments next to each
other stay accurate. Checked against the textbook half-wave dipole (about
73 + j42 ohm, 2.15 dBi) in the tests.

LIMITS
Free space (no ground, except the monopole's perfect plane), perfectly
conducting, straight parallel wires, thin (radius well under a segment).
A real antenna near the ground, a mast or the feed line differs; NEC2 is
the next tool for that.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

C = 299_792_458.0
ETA0 = 376.730313668          # ohm, impedance of free space
_GL_X, _GL_W = np.polynomial.legendre.leggauss(8)


def wavelength(f: float) -> float:
    return C / f


@dataclass
class Wire:
    x: float                   # position across the boom (m)
    z_center: float            # centre along the element axis (m)
    length: float              # m
    radius: float              # m
    segments: int = 0          # 0: chosen from the wavelength (odd, so there is a centre segment)


@dataclass
class Solution:
    frequency: float
    impedance: complex
    currents: np.ndarray       # per segment, A, for 1 V at the feed
    seg_x: np.ndarray
    seg_z: np.ndarray
    seg_len: np.ndarray
    wire_of: np.ndarray
    directivity_dbi: float = 0.0
    max_direction: Tuple[float, float] = (90.0, 0.0)   # theta, phi in degrees
    pattern_phi: np.ndarray = field(default_factory=lambda: np.zeros(0))       # dBi at theta=90
    pattern_theta: np.ndarray = field(default_factory=lambda: np.zeros(0))     # dBi at phi=0
    front_to_back_db: Optional[float] = None


def _segments_for(w: Wire, lam: float) -> int:
    """Segments for a wire: even (the feed is the centre node), about 100 per
    wavelength, and each segment well longer than the radius."""
    n = int(w.segments) if w.segments else max(12, int(math.ceil(w.length / lam * 100)))
    if n % 2:
        n += 1
    while n > 12 and w.length / n < 4 * w.radius:
        n -= 2
    return n


def _gint(zp, xp, zq, xq, dq, aq, k):
    """∫ over the interval of length dq centred at zq of e^{-jkR}/(4 pi R) dz',
    R = sqrt((zp - z')² + rho²), rho² = (xp - xq)² + aq². Broadcasts."""
    rho = np.sqrt((xp - xq) ** 2 + aq ** 2)
    u1 = (zq - dq / 2) - zp
    u2 = (zq + dq / 2) - zp
    static = np.arcsinh(u2 / rho) - np.arcsinh(u1 / rho)
    half = (u2 - u1) / 2
    mid = (u2 + u1) / 2
    acc = 0.0
    for xg, wg in zip(_GL_X, _GL_W):
        u = mid + half * xg
        r = np.sqrt(u * u + rho * rho)
        acc = acc + wg * (np.expm1(-1j * k * r) / r)     # (e^{-jkR} - 1)/R, smooth
    return (static + half * acc) / (4 * math.pi)


def solve(wires: Sequence[Wire], f: float, feed: int = 0, pattern: bool = True) -> Solution:
    """Currents and input impedance for 1 V at the centre of ``wires[feed]``."""
    lam = wavelength(f)
    k = 2 * math.pi / lam
    xs, zs, ds, rs, wid = [], [], [], [], []
    for i, w in enumerate(wires):
        n = _segments_for(w, lam)
        d = w.length / n
        # the unknown currents sit on the n - 1 inner nodes, so the current is
        # zero at both ends and every charge pulse lies on the wire
        zc = w.z_center - w.length / 2 + d * np.arange(1, n)
        xs += [w.x] * (n - 1)
        zs += list(zc)
        ds += [d] * (n - 1)
        rs += [w.radius] * (n - 1)
        wid += [i] * (n - 1)
    x, z, d, a, wire_of = (np.array(v, dtype=float) for v in (xs, zs, ds, rs, wid))
    wire_of = wire_of.astype(int)
    N = len(z)

    XP, XQ, ZP, ZQ = x[:, None], x[None, :], z[:, None], z[None, :]
    DQ, AQ, DP = d[None, :], a[None, :], d[:, None]
    g0 = _gint(ZP, XP, ZQ, XQ, DQ, AQ, k)
    gpp = _gint(ZP + DP / 2, XP, ZQ + DQ / 2, XQ, DQ, AQ, k)
    gpm = _gint(ZP + DP / 2, XP, ZQ - DQ / 2, XQ, DQ, AQ, k)
    gmp = _gint(ZP - DP / 2, XP, ZQ + DQ / 2, XQ, DQ, AQ, k)
    gmm = _gint(ZP - DP / 2, XP, ZQ - DQ / 2, XQ, DQ, AQ, k)
    # V_m = j k eta Δ ∫G I + (Φ(m+½) - Φ(m-½)), charge from the current's steps
    Z = 1j * k * ETA0 * DQ * g0 - 1j * ETA0 / (k * DQ) * (gpp - gpm - gmp + gmm)
    V = np.zeros(N, dtype=complex)
    feed_idx = np.where(wire_of == feed)[0]
    centre = int(feed_idx[len(feed_idx) // 2])
    V[centre] = 1.0
    I = np.linalg.solve(Z, V)
    zin = 1.0 / I[centre]
    sol = Solution(f, complex(zin), I, x, z, d, wire_of)
    if pattern:
        _pattern(sol, k)
    return sol


def _field(sol: Solution, k: float, theta: np.ndarray, phi: np.ndarray) -> np.ndarray:
    """|F|² (relative) for z-directed currents, E_theta ∝ sin(theta) Σ I Δ e^{jk r·r̂}."""
    st, ct = np.sin(theta), np.cos(theta)
    phase = k * (sol.seg_x[None, :] * (st * np.cos(phi))[:, None] + sol.seg_z[None, :] * ct[:, None])
    af = (sol.currents * sol.seg_len)[None, :] * np.exp(1j * phase)
    s = af.sum(axis=1)
    return np.abs(st * s) ** 2


def _pattern(sol: Solution, k: float) -> None:
    nt, nphi = 91, 144
    th = np.linspace(0, math.pi, nt)
    ph = np.linspace(0, 2 * math.pi, nphi, endpoint=False)
    T, P = np.meshgrid(th, ph, indexing="ij")
    p = _field(sol, k, T.ravel(), P.ravel()).reshape(T.shape)
    dth = th[1] - th[0]
    dph = ph[1] - ph[0]
    w = np.sin(T) * dth * dph
    w[0, :] *= 0.5
    w[-1, :] *= 0.5
    total = float((p * w).sum())
    if total <= 0:
        return
    D = 4 * math.pi * p / total
    i = np.unravel_index(int(np.argmax(D)), D.shape)
    sol.directivity_dbi = 10 * math.log10(float(D[i]))
    sol.max_direction = (math.degrees(th[i[0]]), math.degrees(ph[i[1]]))
    phis = np.radians(np.arange(0, 360, 2.0))
    hp = _field(sol, k, np.full(len(phis), math.pi / 2), phis) * 4 * math.pi / total
    sol.pattern_phi = 10 * np.log10(np.maximum(hp, 1e-12))
    thetas = np.radians(np.arange(0, 181, 2.0))
    ep = _field(sol, k, thetas, np.zeros(len(thetas))) * 4 * math.pi / total
    sol.pattern_theta = 10 * np.log10(np.maximum(ep, 1e-12))
    fwd, back = hp[0], hp[len(hp) // 2]
    if back > 0:
        sol.front_to_back_db = 10 * math.log10(fwd / back)


# ── designs ──────────────────────────────────────────────────────────────
def dipole(f: float, length: float, radius: float, segments: int = 0) -> Solution:
    return solve([Wire(0.0, 0.0, length, radius, segments)], f)


def resonant_dipole_length(f: float, radius: float, segments: int = 0) -> float:
    """The centre-fed element length with zero input reactance (m)."""
    lam = wavelength(f)

    def x(L):
        return solve([Wire(0.0, 0.0, L, radius, segments)], f, pattern=False).impedance.imag

    return _root(x, 0.45 * lam, 0.50 * lam)


def monopole(f: float, length: float, radius: float, segments: int = 0) -> Solution:
    """A vertical element of ``length`` fed at a perfect ground plane: solved
    as the dipole of twice the length (image theory), so Z is half the
    dipole's and the gain 3 dB more (all the power goes into half the space)."""
    sol = solve([Wire(0.0, 0.0, 2 * length, radius, segments)], f)
    sol.impedance = sol.impedance / 2
    sol.directivity_dbi += 10 * math.log10(2)
    sol.pattern_phi = sol.pattern_phi + 10 * math.log10(2)
    half = len(sol.pattern_theta) // 2 + 1
    sol.pattern_theta = sol.pattern_theta[:half] + 10 * math.log10(2)
    return sol


def yagi(f: float, lengths: Sequence[float], positions: Sequence[float], radius: float,
         driven: int = 1, segments: int = 0) -> Solution:
    """Parallel elements along z at boom positions ``positions`` (m, along +x,
    the forward direction). ``driven`` is the fed element's index."""
    wires = [Wire(p, 0.0, L, radius, segments) for L, p in zip(lengths, positions)]
    return solve(wires, f, feed=driven)


def yagi_design(f: float, elements: int = 3, radius: float = 0.003,
                segments: int = 0) -> Tuple[List[float], List[float], Solution]:
    """A starting Yagi-Uda: reflector 0.5 lambda at -0.2 lambda, directors
    0.44 lambda (a little shorter each) every 0.25 lambda, and the driven
    element trimmed until its reactance is zero. Returns (lengths, positions,
    solution)."""
    if elements < 2:
        raise ValueError("A Yagi needs at least a reflector and a driven element.")
    lam = wavelength(f)
    lengths = [0.50 * lam, 0.47 * lam]
    positions = [-0.20 * lam, 0.0]
    for i in range(elements - 2):
        lengths.append((0.44 - 0.005 * i) * lam)
        positions.append(0.25 * lam * (i + 1))

    def x(L):
        ls = list(lengths)
        ls[1] = L
        return yagi(f, ls, positions, radius, segments=segments).impedance.imag

    try:
        lengths[1] = _root(x, 0.42 * lam, 0.50 * lam)
    except ValueError:
        pass
    return lengths, positions, yagi(f, lengths, positions, radius, segments=segments)


def _root(fn, a: float, b: float, tol: float = 1e-6, iters: int = 40) -> float:
    fa, fb = fn(a), fn(b)
    if fa * fb > 0:
        raise ValueError("No resonance in the searched range.")
    for _ in range(iters):
        m = b - fb * (b - a) / (fb - fa)          # regula falsi (Illinois)
        fm = fn(m)
        if abs(fm) < 1e-3 or abs(b - a) < tol * max(abs(a), 1.0):
            return m
        if fm * fb < 0:
            a, fa = b, fb
        else:
            fa /= 2
        b, fb = m, fm
    return b


# ── matching ─────────────────────────────────────────────────────────────
def reflection(z: complex, z0: float = 50.0) -> complex:
    return (z - z0) / (z + z0)


def swr(z: complex, z0: float = 50.0) -> float:
    g = abs(reflection(z, z0))
    return float("inf") if g >= 1 else (1 + g) / (1 - g)


def swr_sweep(build, f0: float, span: float = 0.1, points: int = 21,
              z0: float = 50.0) -> List[Tuple[float, float, complex]]:
    """(frequency, SWR, Z) from f0(1 - span/2) to f0(1 + span/2); ``build(f)``
    returns a Solution at f with the antenna's fixed dimensions."""
    out = []
    for f in np.linspace(f0 * (1 - span / 2), f0 * (1 + span / 2), points):
        z = build(float(f)).impedance
        out.append((float(f), swr(z, z0), z))
    return out


def swr_bandwidth(sweep: Sequence[Tuple[float, float, complex]], limit: float = 2.0
                  ) -> Optional[Tuple[float, float]]:
    """The frequency range around the best point where SWR <= ``limit``."""
    if not sweep:
        return None
    best = min(range(len(sweep)), key=lambda i: sweep[i][1])
    if sweep[best][1] > limit:
        return None
    lo = hi = best
    while lo > 0 and sweep[lo - 1][1] <= limit:
        lo -= 1
    while hi < len(sweep) - 1 and sweep[hi + 1][1] <= limit:
        hi += 1
    return sweep[lo][0], sweep[hi][0]


def quarter_wave_stub(f: float, velocity_factor: float = 0.66) -> float:
    """Length of a quarter-wave section of feed line (m); 0.66 is solid-PE coax."""
    return velocity_factor * wavelength(f) / 4
