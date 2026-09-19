#!/usr/bin/env python3
"""Monte Carlo input-referred offset from device mismatch.

Spectre varies mismatch through the `vary ... dist=gauss` blocks the SKY130 models carry
as comments; ngspice has no equivalent per-instance statistical parameter, so a naive
port either applies one identical "random" value to every device (which cancels in a
matched pair and reports zero offset) or requires editing the foundry models. Neither is
acceptable.

Instead the mismatch is injected where it physically acts: as a threshold shift referred
to each gate. The magnitudes come from the PDK's own Pelgrom coefficients, the same
numbers the Spectre flow would use --

    sigma(Vth) = vth0_slope / sqrt(W * L)          W, L in um

    nfet  vth0_slope = 3.356 mV*um
    pfet  vth0_slope = 5.856 mV*um

Each device gets an independent draw, the amplifier is closed in unity gain, and the
offset is read directly as vout - vin (exact to within 1/A0, i.e. ~160 ppm here).

Which devices matter, and why only these:
  M1/M2  input pair  -- contributes 1:1, dominant
  M3/M4  mirror load -- contributes scaled by gm3/gm1
  M5     tail        -- common to both branches, contributes only via finite CMRR
  M6/M7  stage 2     -- divided by the first stage's gain, negligible

Usage:
    python3 mc_offset.py --n 300 --seed 1
"""

import argparse
import json
import math
import pathlib
import random
import statistics
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
MODELS = ROOT / "models"
SIZING = ROOT / "ota" / "sizing.json"

# Pelgrom coefficients, V*um, read from the PDK's invariant.spice
A_VTH = {"n": 0.003356, "p": 0.005856}

VDD = 1.8
VCM = 1.2


def sigma_vth(kind, W_um, L_um, m):
    """Pelgrom: mismatch falls as 1/sqrt(area). Fingering multiplies the effective area."""
    return A_VTH[kind] / math.sqrt(W_um * L_um * m)


def build(sz, off, corner):
    """Flattened OTA in unity gain, with a gate-referred offset source per device."""
    def d(k):
        v = sz[k]
        mult = f" m={v['m']}" if v["m"] > 1 else ""
        return f"W={v['W_finger_um']:.4f}u L={v['L_um']:.3f}u{mult}"

    return f"""* Monte Carlo offset sample
.include "{MODELS}/sky130_18v_{corner}.spice"

Vdd vdd 0 {VDD}
Vin vin 0 {VCM}

* bias
Iref vdd vbias_n 10u
XM8 vbias_n vbias_n 0 0 sky130_fd_pr__nfet_01v8 {d('M8')}

* gate-referred threshold mismatch, one independent draw per device
Vo1 g1 vin  {off['M1']:.9f}
Vo2 g2 vout {off['M2']:.9f}
Vo3 g3 s1x  {off['M3']:.9f}
Vo4 g4 s1x  {off['M4']:.9f}

* input pair (unity gain: vinn = vout)
XM1 s1o g1 tail 0 sky130_fd_pr__nfet_01v8 {d('M1')}
XM2 s1x g2 tail 0 sky130_fd_pr__nfet_01v8 {d('M2')}

* mirror load -- M3 diode-connected, gate driven through its own offset
XM3 s1x g3 vdd vdd sky130_fd_pr__pfet_01v8 {d('M3')}
XM4 s1o g4 vdd vdd sky130_fd_pr__pfet_01v8 {d('M4')}

XM5 tail vbias_n 0 0 sky130_fd_pr__nfet_01v8 {d('M5')}

* second stage
XM6 vout s1o vdd vdd sky130_fd_pr__pfet_01v8 {d('M6')}
XM7 vout vbias_n 0 0 sky130_fd_pr__nfet_01v8 {d('M7')}

Rz s1o z {sz['_rz']:.1f}
Cc z vout {sz['_cc']*1e12:.4f}p
CL vout 0 5p

.control
op
print v(vout)
.endc
.end
"""


def run_one(sz, off, corner):
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "mc.spice"
        f.write_text(build(sz, off, corner))
        p = subprocess.run(["ngspice", "-b", str(f)],
                           capture_output=True, text=True, timeout=300)
        for line in p.stdout.splitlines():
            if line.strip().startswith("v(vout)"):
                try:
                    return float(line.split("=")[1])
                except (IndexError, ValueError):
                    return None
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--corner", default="tt")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    meta = json.loads(SIZING.read_text())
    sz = dict(meta["devices"])
    sz["_rz"] = meta["rz_ohm"]
    sz["_cc"] = meta["cc_f"]

    kind = {"M1": "n", "M2": "n", "M3": "p", "M4": "p"}
    sig = {k: sigma_vth(kind[k], sz[k]["W_finger_um"], sz[k]["L_um"], sz[k]["m"])
           for k in kind}

    print("per-device sigma(Vth) from PDK Pelgrom coefficients:")
    for k, v in sig.items():
        print(f"  {k}: W={sz[k]['W_total_um']:7.2f}um  sigma = {v*1e3:6.3f} mV")

    # Analytical prediction, for comparison with the simulated spread.
    gm1, gm3 = sz["M1"]["gm"], sz["M3"]["gm"]
    pred = math.sqrt(2 * sig["M1"] ** 2 + 2 * (sig["M3"] * gm3 / gm1) ** 2)
    print(f"\nanalytical prediction: sigma(Vos) = "
          f"sqrt(2*s1^2 + 2*(s3*gm3/gm1)^2) = {pred*1e3:.3f} mV  "
          f"(gm3/gm1 = {gm3/gm1:.3f})")

    rng = random.Random(args.seed)
    zero = {k: 0.0 for k in kind}
    systematic = run_one(sz, zero, args.corner)
    print(f"systematic offset (all mismatch zero): {(systematic - VCM)*1e6:+.1f} uV")

    vals = []
    for i in range(args.n):
        off = {k: rng.gauss(0.0, sig[k]) for k in kind}
        v = run_one(sz, off, args.corner)
        if v is not None:
            vals.append((v - VCM))

    if len(vals) < 10:
        raise SystemExit(f"only {len(vals)} of {args.n} runs converged")

    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    vals_sorted = sorted(vals)

    def pct(q):
        return vals_sorted[min(len(vals_sorted) - 1, int(q * len(vals_sorted)))]

    print(f"\nMonte Carlo, n={len(vals)}/{args.n} converged, corner={args.corner}, seed={args.seed}")
    print(f"  mean            = {mu*1e3:+.4f} mV")
    print(f"  sigma           = {sd*1e3:.4f} mV")
    print(f"  min / max       = {min(vals)*1e3:+.3f} / {max(vals)*1e3:+.3f} mV")
    print(f"  |Vos| < 3 sigma = {sum(1 for v in vals if abs(v-mu) < 3*sd)/len(vals)*100:.1f}%")
    print(f"  1/99 pct        = {pct(0.01)*1e3:+.3f} / {pct(0.99)*1e3:+.3f} mV")
    print(f"  sim/analytical  = {sd/pred:.3f}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({
            "n": len(vals), "seed": args.seed, "corner": args.corner,
            "sigma_v": sd, "mean_v": mu, "predicted_sigma_v": pred,
            "systematic_v": systematic - VCM,
            "per_device_sigma_vth_v": sig,
        }, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
