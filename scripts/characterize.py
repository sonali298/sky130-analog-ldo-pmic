#!/usr/bin/env python3
"""Full-spec characterisation of the two-stage OTA across PVT.

Every figure of merit is computed here in Python from raw swept data rather than by
ngspice's `meas`. That is deliberate: `meas` cannot reference an earlier measurement
inside a later expression (so "the frequency where gain is 3 dB below its own DC value"
is awkward to express), and its phase output is in radians while every datasheet quotes
degrees -- a mismatch that silently turns a phase margin of 81 deg into a reported 178.
Parsing the sweep directly makes each definition explicit and identical across corners.

Analyses:
    ac     open-loop gain, unity-gain frequency, phase margin, gain margin, f_3dB
    tran   slew rate, rising and falling, from a large-signal step in unity gain
    icmr   input common-mode range: where a unity-gain buffer still tracks its input
    swing  output voltage swing under load
    cmrr   common-mode rejection = A_dm / A_cm
    psrr   supply rejection = A_dm / A_vdd
    noise  input-referred noise density at 1 kHz and integrated over the band

Usage:
    python3 characterize.py                        # tt, 27C
    python3 characterize.py --corners tt ff ss sf fs --temps -40 27 125
"""

import argparse
import json
import math
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OTA = ROOT / "ota"
MODELS = ROOT / "models"

VDD = 1.8
VCM = 1.2
CL = "5p"

# Shared preamble: model library, the OTA netlist, supplies and bias.
PREAMBLE = """.include "{models}/sky130_18v_{corner}.spice"
.include "{ota}/ota_core.spice"
Vdd vdd 0 {vdd}
XB  vdd 0 vbias_n otabias
"""


def deck(corner, body):
    return ("* auto-generated characterisation deck\n"
            + PREAMBLE.format(models=MODELS, ota=OTA, corner=corner, vdd=VDD)
            + body + "\n.end\n")


def run(src, temp, outfiles):
    """Write a deck, run ngspice, return the parsed contents of each requested output."""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        paths = {k: td / f"{k}.txt" for k in outfiles}
        text = src
        for k, p in paths.items():
            text = text.replace(f"${k}", str(p))
        text = text.replace("$TEMP", str(temp))
        f = td / "deck.spice"
        f.write_text(text)
        proc = subprocess.run(["ngspice", "-b", str(f)],
                              capture_output=True, text=True, timeout=1800)
        out = {}
        for k, p in paths.items():
            out[k] = _cols(p.read_text()) if p.exists() else None
        out["_log"] = proc.stdout + proc.stderr
        return out


def _cols(text):
    """ngspice wrdata emits an x column before every y column; keep x once."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue
        rows.append(vals)
    return rows


# ------------------------------------------------------------------ analyses

def ac_analysis(corner, temp):
    body = f"""Vcm vcm 0 {VCM}
Vin vinp vcm DC 0 AC 1
* DC-short / AC-open feedback so one deck gives both a valid bias point and an
* open-loop measurement (see ota/tb_ac.spice for the full explanation).
Lfb vout vinn 1T
Cfb vinn vcm  1T
XO  vinp vinn vout vdd 0 vbias_n ota
CL  vout 0 {CL}
.control
option temp=$TEMP
op
ac dec 200 0.1 1G
let mag = vdb(vout)
let ph  = 180/PI * vp(vout)
wrdata $ac mag ph
.endc
"""
    r = run(deck(corner, body), temp, ["ac"])
    rows = r["ac"]
    if not rows:
        return {"error": "ac sweep produced no data", "log": r["_log"][-800:]}

    f = [x[0] for x in rows]
    mag = [x[1] for x in rows]
    ph = [x[3] for x in rows]

    # ngspice unwraps phase past -180 by jumping to +180; undo that so the crossover
    # search sees a monotonically falling curve.
    unw, off = [], 0.0
    for i, p in enumerate(ph):
        if i and p - ph[i - 1] > 180:
            off -= 360
        elif i and p - ph[i - 1] < -180:
            off += 360
        unw.append(p + off)

    a0 = mag[0]

    def cross(ys, target):
        """First frequency where a falling curve crosses target, log-interpolated."""
        for i in range(1, len(ys)):
            if (ys[i - 1] - target) >= 0 >= (ys[i] - target):
                y0, y1 = ys[i - 1], ys[i]
                t = 0.0 if y1 == y0 else (y0 - target) / (y0 - y1)
                return math.exp(math.log(f[i - 1]) + t * (math.log(f[i]) - math.log(f[i - 1])))
        return None

    ugf = cross(mag, 0.0)
    f3db = cross(mag, a0 - 3.0)

    pm = None
    if ugf:
        for i in range(1, len(f)):
            if f[i - 1] <= ugf <= f[i]:
                t = (math.log(ugf) - math.log(f[i - 1])) / (math.log(f[i]) - math.log(f[i - 1]))
                pm = 180.0 + (unw[i - 1] + t * (unw[i] - unw[i - 1]))
                break

    # Gain margin: gain remaining where phase has fallen to -180.
    gm_db = None
    f180 = cross(unw, -180.0)
    if f180:
        for i in range(1, len(f)):
            if f[i - 1] <= f180 <= f[i]:
                t = (math.log(f180) - math.log(f[i - 1])) / (math.log(f[i]) - math.log(f[i - 1]))
                gm_db = -(mag[i - 1] + t * (mag[i] - mag[i - 1]))
                break

    return {"a0_db": a0, "f3db_hz": f3db, "ugf_hz": ugf, "pm_deg": pm, "gm_db": gm_db}


def tran_analysis(corner, temp):
    """Slew rate from a large step in unity-gain feedback."""
    body = f"""Vstep vin 0 PULSE({VCM-0.3} {VCM+0.3} 1u 10n 10n 5u 10u)
XO  vin vout vout vdd 0 vbias_n ota
CL  vout 0 {CL}
.control
option temp=$TEMP
tran 2n 12u
wrdata $tr v(vout) v(vin)
.endc
"""
    r = run(deck(corner, body), temp, ["tr"])
    rows = r["tr"]
    if not rows:
        return {"error": "transient produced no data"}
    t = [x[0] for x in rows]
    vo = [x[1] for x in rows]

    def slew(lo_frac, hi_frac, rising):
        """dV/dt measured between 30% and 70% of the step, avoiding the settling tails."""
        seg = [(ti, vi) for ti, vi in zip(t, vo)]
        lo = min(vo)
        hi = max(vo)
        span = hi - lo
        a, b = lo + lo_frac * span, lo + hi_frac * span
        ta = tb = None
        prev = None
        for ti, vi in seg:
            if prev is not None:
                if rising and prev < a <= vi and ta is None:
                    ta = ti
                if rising and prev < b <= vi and ta is not None and tb is None:
                    tb = ti
                if not rising and prev > a >= vi and ta is None:
                    ta = ti
                if not rising and prev > b >= vi and ta is not None and tb is None:
                    tb = ti
            prev = vi
        if ta is None or tb is None or tb == ta:
            return None
        return abs((b - a) / (tb - ta))

    return {"sr_rise_v_per_us": (slew(0.3, 0.7, True) or 0) / 1e6,
            "sr_fall_v_per_us": (slew(0.7, 0.3, False) or 0) / 1e6}


def icmr_swing(corner, temp):
    """Input common-mode range, defined by device saturation rather than by tracking.

    The obvious test -- sweep a unity-gain buffer and keep the range where vout follows
    vin -- has a trap at the bottom of the sweep: with vin at 0 the input pair is off,
    vout sits at 0 too, and the buffer "tracks" perfectly while amplifying nothing. Taking
    min/max over all tracking points then reports an ICMR starting at 0 V.

    What actually bounds the common-mode range is device saturation: M5 (the tail) is
    squeezed out of saturation as vcm falls, and M1 leaves saturation as vcm rises toward
    the mirror's gate voltage. So the range is measured directly as the widest contiguous
    span where both Vds - Vdsat stay positive.
    """
    n1 = "m.xo.xm1.msky130_fd_pr__nfet_01v8"
    n5 = "m.xo.xm5.msky130_fd_pr__nfet_01v8"
    body = f"""Vin vin 0 0
XO  vin vout vout vdd 0 vbias_n ota
CL  vout 0 {CL}
.control
option temp=$TEMP
save all @{n1}[vds] @{n1}[vdsat] @{n5}[vds] @{n5}[vdsat]
dc Vin 0 {VDD} 0.005
let ov1 = @{n1}[vds] - @{n1}[vdsat]
let ov5 = @{n5}[vds] - @{n5}[vdsat]
wrdata $dc v(vout) ov1 ov5
.endc
"""
    r = run(deck(corner, body), temp, ["dc"])
    rows = r["dc"]
    if not rows:
        return {"error": "icmr sweep produced no data", "log": r["_log"][-600:]}

    vin = [x[0] for x in rows]
    vout = [x[1] for x in rows]
    ov1 = [x[3] for x in rows]
    ov5 = [x[5] for x in rows]

    # widest contiguous span with both input-stage devices saturated
    best = cur = None
    for i, v in enumerate(vin):
        if ov1[i] > 0 and ov5[i] > 0:
            cur = (v, v) if cur is None else (cur[0], v)
            if best is None or (cur[1] - cur[0]) > (best[1] - best[0]):
                best = cur
        else:
            cur = None

    out = {"vout_min": min(vout), "vout_max": max(vout)}
    if best:
        out.update({"icmr_lo": best[0], "icmr_hi": best[1],
                    "icmr_range": best[1] - best[0]})
    else:
        out.update({"icmr_lo": None, "icmr_hi": None, "icmr_range": None})
    return out


def cmrr_psrr(corner, temp):
    """A_dm from the open-loop deck; A_cm and A_vdd from the same bias point."""
    common = f"""Lfb vout vinn 1T
Cfb vinn vcm  1T
XO  vinp vinn vout vdd 0 vbias_n ota
CL  vout 0 {CL}
"""
    dm = f"""Vcm vcm 0 {VCM}
Vin vinp vcm DC 0 AC 1
{common}
.control
option temp=$TEMP
op
ac dec 50 1 1Meg
let m = vdb(vout)
wrdata $dm m
.endc
"""
    # Common mode: drive both inputs together by moving the reference itself.
    cm = f"""Vcm vcm 0 DC {VCM} AC 1
Vin vinp vcm DC 0 AC 0
{common}
.control
option temp=$TEMP
op
ac dec 50 1 1Meg
let m = vdb(vout)
wrdata $cm m
.endc
"""
    ps = f"""Vcm vcm 0 {VCM}
Vin vinp vcm DC 0 AC 0
{common}
.control
option temp=$TEMP
alter Vdd ac=1
op
ac dec 50 1 1Meg
let m = vdb(vout)
wrdata $ps m
.endc
"""
    out = {}
    a_dm = run(deck(corner, dm), temp, ["dm"])["dm"]
    a_cm = run(deck(corner, cm), temp, ["cm"])["cm"]
    a_ps = run(deck(corner, ps), temp, ["ps"])["ps"]
    if a_dm and a_cm:
        out["cmrr_db"] = a_dm[0][1] - a_cm[0][1]
    if a_dm and a_ps:
        out["psrr_db"] = a_dm[0][1] - a_ps[0][1]
    return out


def noise_analysis(corner, temp):
    body = f"""Vcm vcm 0 {VCM}
Vin vinp vcm DC 0 AC 1
Lfb vout vinn 1T
Cfb vinn vcm  1T
XO  vinp vinn vout vdd 0 vbias_n ota
CL  vout 0 {CL}
.control
option temp=$TEMP
op
noise v(vout) Vin dec 20 1 10Meg
setplot noise1
wrdata $nz inoise_spectrum
.endc
"""
    r = run(deck(corner, body), temp, ["nz"])
    rows = r["nz"]
    if not rows:
        return {"error": "noise produced no data", "log": r["_log"][-600:]}
    f = [x[0] for x in rows]
    inz = [x[1] for x in rows]
    at1k = None
    for i in range(1, len(f)):
        if f[i - 1] <= 1e3 <= f[i]:
            at1k = inz[i]
            break
    # integrate the input-referred spectrum over the swept band
    total = 0.0
    for i in range(1, len(f)):
        total += 0.5 * (inz[i] ** 2 + inz[i - 1] ** 2) * (f[i] - f[i - 1])
    return {"in_1khz_nv_rthz": (at1k or 0) * 1e9,
            "in_integrated_uvrms": math.sqrt(total) * 1e6}


def characterize(corner, temp):
    res = {"corner": corner, "temp_c": temp}
    res.update(ac_analysis(corner, temp))
    res.update(tran_analysis(corner, temp))
    res.update(icmr_swing(corner, temp))
    res.update(cmrr_psrr(corner, temp))
    res.update(noise_analysis(corner, temp))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corners", nargs="+", default=["tt"])
    ap.add_argument("--temps", nargs="+", type=float, default=[27])
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    results = []
    for c in args.corners:
        for t in args.temps:
            r = characterize(c, t)
            results.append(r)
            err = r.get("error")
            if err:
                print(f"{c} {t}C  ERROR: {err}", file=sys.stderr)
                if "log" in r:
                    print(r["log"], file=sys.stderr)

    hdr = (f"{'corner':>6} {'T(C)':>6} {'A0(dB)':>8} {'UGF(MHz)':>9} {'PM(deg)':>8} "
           f"{'GM(dB)':>7} {'SR+(V/us)':>10} {'SR-(V/us)':>10} {'ICMR(V)':>16} "
           f"{'CMRR(dB)':>9} {'PSRR(dB)':>9} {'noise@1k':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        def g(k, f="{:.2f}", scale=1.0):
            v = r.get(k)
            return f.format(v * scale) if isinstance(v, (int, float)) else "  --  "
        icmr = ("{:.2f}-{:.2f}".format(r["icmr_lo"], r["icmr_hi"])
                if r.get("icmr_lo") is not None else "  --  ")
        print(f"{r['corner']:>6} {r['temp_c']:>6.0f} {g('a0_db'):>8} "
              f"{g('ugf_hz', scale=1e-6):>9} {g('pm_deg'):>8} {g('gm_db'):>7} "
              f"{g('sr_rise_v_per_us'):>10} {g('sr_fall_v_per_us'):>10} {icmr:>16} "
              f"{g('cmrr_db'):>9} {g('psrr_db'):>9} {g('in_1khz_nv_rthz'):>9}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
