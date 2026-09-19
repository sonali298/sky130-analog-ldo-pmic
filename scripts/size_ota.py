#!/usr/bin/env python3
"""Size the two-stage Miller OTA from measured gm/Id data and emit its netlist.

Nothing here is a guessed width. Each device is specified by the pair (target current,
target gm/Id); the current density Id/W that corresponds to that inversion level is
interpolated from the characterisation sweep, and the width follows as

    W = I_target / (Id/W)

Topology (Allen-Holberg two-stage, NMOS input):

    VDD ---+-------------+---------------+-------------+
           |             |               |             |
          M3            M4              M6            (M7 is the NMOS sink below)
        (diode)      (mirror)      (2nd stage PMOS)
           |             |               |
           +-----+-------+               +---- vout ---+---- CL
                 |       |               |             |
              d(M1)   d(M2)==stage1 --Cc-Rz--+        M7
                 |       |                                (gate = vbias_n)
       vinp --| M1     M2 |-- vinn                        |
                 +---+---+                               GND
                     |
                    M5  (tail, gate = vbias_n)
                     |
                    GND

The input pair is NMOS rather than PMOS because the measured SKY130 thresholds are
strongly asymmetric -- nfet Vth = 0.577 V but pfet |Vth| = 1.03 V. Against a 1.8 V rail
a PMOS pair spends more than half the supply just turning on, and its input common-mode
range collapses. The pfets go where their 2x higher intrinsic gain pays and their
headroom cost does not: the mirror load and the second stage.
"""

import argparse
import csv
import json
import math
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# ---------------------------------------------------------------- design targets
VDD = 1.8
CL = 5e-12          # load capacitance
CC = 2e-12          # Miller compensation capacitance
GBW = 10e6          # target unity-gain bandwidth
I_TAIL = 20e-6      # first-stage tail current -> SR = I_TAIL/CC = 10 V/us

# Inversion levels for the load and current-source devices. Both are set by headroom,
# not by gain: SKY130's pfet |Vth| is 1.03 V against a 1.8 V rail, so every 100 mV of
# load overdrive comes straight out of the input common-mode range. Running M3/M4
# weaker (higher gm/Id) drops |Vgs3| toward |Vth| and buys back ICMR at the top; running
# the tail weaker drops Vdsat5 and buys it back at the bottom.
GM_ID_LOAD = 14.0   # M3/M4 -- also fixes M6, which shares their gate voltage
GM_ID_TAIL = 10.0   # M5/M7/M8 current sources

# The SKY130 model bins stop at W = 100 um; a wider single device matches no bin and
# ngspice rejects it with "could not find a valid modelname". Real layouts never draw
# one enormous finger anyway -- gate resistance and drain capacitance both argue for
# splitting it -- so any device wider than this is emitted as m parallel fingers.
W_MAX_FINGER = 50.0

TWO_PI = 6.283185307179586


def load(csv_path):
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            rows.append({k: float(v) for k, v in r.items()})
    # gm/Id falls monotonically with Vgs above threshold; keep that branch only so the
    # interpolation is single-valued. Below threshold Id is ~0 and Id/W is meaningless.
    rows = [r for r in rows if r["id"] > 1e-9]
    return rows


def id_per_width(rows, target_gm_id):
    """Interpolate current density (A/um) at a target gm/Id."""
    # walk the descending-gm/Id branch
    best = None
    for a, b in zip(rows, rows[1:]):
        lo, hi = sorted((a["gm_id"], b["gm_id"]))
        if lo <= target_gm_id <= hi:
            span = b["gm_id"] - a["gm_id"]
            t = 0.0 if span == 0 else (target_gm_id - a["gm_id"]) / span
            best = a["id_w"] + t * (b["id_w"] - a["id_w"])
            # prefer the strong-inversion crossing (later in the sweep)
    if best is None:
        raise ValueError(f"gm/Id={target_gm_id} outside measured range")
    return best


def size(rows, current, gm_id, name):
    idw = id_per_width(rows, gm_id)
    W = current / idw
    return dict(name=name, I=current, gm_id=gm_id, gm=gm_id * current, W=W, idw=idw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nfet-csv", default=str(HERE / "nfet_L1.csv"))
    ap.add_argument("--pfet-csv", default=str(HERE / "pfet_L1.csv"))
    ap.add_argument("--out", default=str(HERE.parent / "ota" / "ota_core.spice"))
    args = ap.parse_args()

    nfet = load(args.nfet_csv)
    pfet = load(args.pfet_csv)

    # ---- first stage -----------------------------------------------------------
    # GBW = gm1 / Cc  =>  gm1 fixes the input pair's inversion level at 10 uA.
    gm1 = TWO_PI * GBW * CC
    i1 = I_TAIL / 2
    gm_id1 = gm1 / i1

    # ---- second stage ----------------------------------------------------------
    # The non-dominant pole sits at gm6/CL. Placing it >= 2.2x GBW is the standard
    # condition for ~60 deg phase margin once the RHP zero is nulled by Rz.
    gm6_min = 2.2 * TWO_PI * GBW * CL
    gm6 = 1.10 * gm6_min          # 10% margin against corner spread

    # M6's gm/Id is NOT a free choice. Its gate is tied to the M3/M4 diode voltage, so
    # it sees exactly the same Vgs as the load devices and therefore sits at exactly
    # their inversion level. Picking gm/Id for M6 independently is what breaks the
    # systematic offset condition below.
    i6 = gm6 / GM_ID_LOAD

    # Widths for M6 and M7 are *derived*, not chosen, so that the stage-2 currents
    # balance. With the inputs balanced, s1o settles at the M3/M4 diode voltage, so
    #     Id6 = i1 * (W6/W4)      (M6 mirrors M4: same Vgs, same device type)
    #     Id7 = I_TAIL * (W7/W5)  (M7 mirrors M5)
    # and Id6 must equal Id7. Substituting I_TAIL = 2*i1 gives the Allen-Holberg
    # systematic offset condition  W6/W4 = 2 * W7/W5.
    #
    # Sizing M6 and M7 independently violates it: the sink demands more current than
    # the source delivers, the output slams to the rail, and M7 drops into triode.
    # That is exactly what the first version of this script produced (M7 Vds = 97 mV
    # against Vdsat = 286 mV, vout stuck at 97 mV instead of mid-rail).
    m4 = size(pfet, i1, GM_ID_LOAD, "M4 mirror load (p)")
    m5 = size(nfet, I_TAIL, GM_ID_TAIL, "M5 tail source (n)")
    w6 = m4["W"] * (i6 / i1)
    w7 = m5["W"] * (i6 / I_TAIL)

    devices = [
        size(nfet, i1,     gm_id1,      "M1 input pair (n)"),
        size(nfet, i1,     gm_id1,      "M2 input pair (n)"),
        size(pfet, i1,     GM_ID_LOAD,  "M3 mirror load, diode (p)"),
        m4,
        m5,
        dict(name="M6 second stage (p)",   I=i6, gm_id=GM_ID_LOAD, gm=gm6,
             W=w6, idw=i6 / w6),
        dict(name="M7 second-stage sink (n)", I=i6, gm_id=GM_ID_TAIL,
             gm=GM_ID_TAIL * i6, W=w7, idw=i6 / w7),
        size(nfet, 10e-6,  GM_ID_TAIL,  "M8 bias diode (n)"),
    ]
    by = {d["name"].split()[0]: d for d in devices}

    ratio_lhs = w6 / m4["W"]
    ratio_rhs = 2 * w7 / m5["W"]

    # Nulling resistor: cancels the RHP zero created by Cc feeding forward through M6.
    rz = (1.0 / gm6) * (1.0 + CL / CC)

    print(f"{'device':<28} {'I(uA)':>8} {'gm/Id':>7} {'gm(uS)':>9} "
          f"{'Id/W(uA/um)':>12} {'W(um)':>12}")
    for d in devices:
        m, we = (max(1, math.ceil(d['W'] / W_MAX_FINGER)), None)
        we = d['W'] / m
        fin = f"{m} x {we:.2f}" if m > 1 else f"{we:.2f}"
        print(f"{d['name']:<28} {d['I']*1e6:8.2f} {d['gm_id']:7.2f} {d['gm']*1e6:9.1f} "
              f"{d['idw']*1e6:12.4f} {fin:>12}")

    itot = I_TAIL + i6 + 10e-6
    print(f"\nCc = {CC*1e12:.2f} pF   Rz = {rz:.0f} ohm   CL = {CL*1e12:.1f} pF")
    print(f"predicted SR   = {I_TAIL/CC/1e6:.2f} V/us")
    print(f"predicted GBW  = {gm1/CC/TWO_PI/1e6:.2f} MHz")
    print(f"p2/GBW ratio   = {(gm6/CL)/(gm1/CC):.2f}  (>=2.2 for ~60 deg PM)")
    print(f"offset cond.   = W6/W4 {ratio_lhs:.3f} vs 2*W7/W5 {ratio_rhs:.3f}  "
          f"(must match for zero systematic offset)")
    print(f"total current  = {itot*1e6:.1f} uA  ->  {itot*VDD*1e6:.0f} uW at {VDD} V")

    # ---- netlist ---------------------------------------------------------------
    def fingers(W):
        """Split a width into m parallel fingers, each within the model bin range."""
        m = max(1, math.ceil(W / W_MAX_FINGER))
        return m, W / m

    def dev(n):
        """Emit 'W=..u L=1u' plus an m= multiplier when the device needs fingering."""
        m, we = fingers(by[n]["W"])
        return f"W={we:.3f}u L=1u" + (f" m={m}" if m > 1 else "")

    netlist = f"""* Two-stage Miller-compensated OTA, SKY130 1.8V
*
* Every width below was produced by scripts/size_ota.py from measured gm/Id curves:
* each device is specified as (target current, target gm/Id) and the width follows
* from the interpolated current density. Re-run that script to change the design.
*
* Targets: GBW {GBW/1e6:.0f} MHz, PM >= 60 deg, SR {I_TAIL/CC/1e6:.0f} V/us into CL = {CL*1e12:.0f} pF.
*
* Ports: vinp vinn vout vdd vss vbias_n
.subckt ota vinp vinn vout vdd vss vbias_n

* ---- first stage: NMOS pair, PMOS mirror load
XM1 s1o  vinp tail vss  sky130_fd_pr__nfet_01v8 {dev('M1')}
XM2 s1x  vinn tail vss  sky130_fd_pr__nfet_01v8 {dev('M2')}
XM3 s1x  s1x  vdd  vdd  sky130_fd_pr__pfet_01v8 {dev('M3')}
XM4 s1o  s1x  vdd  vdd  sky130_fd_pr__pfet_01v8 {dev('M4')}
XM5 tail vbias_n vss vss sky130_fd_pr__nfet_01v8 {dev('M5')}

* ---- second stage: PMOS common source with NMOS current sink
XM6 vout s1o  vdd  vdd  sky130_fd_pr__pfet_01v8 {dev('M6')}
XM7 vout vbias_n vss vss sky130_fd_pr__nfet_01v8 {dev('M7')}

* ---- Miller compensation with nulling resistor
* Cc alone creates a right-half-plane zero at gm6/Cc: the feedforward path through Cc
* adds phase lag exactly where the loop needs margin. Rz pushes that zero to
* gm6/Cc * 1/(1 - gm6*Rz); at Rz = (1/gm6)(1 + CL/Cc) it lands on the non-dominant
* pole and cancels it instead of fighting it.
Rz  s1o  z  {rz:.0f}
Cc  z    vout {CC*1e12:.3f}p

.ends ota

* ---- bias generator: Iref into a diode-connected NMOS sets vbias_n for M5 and M7
.subckt otabias vdd vss vbias_n
Iref vdd vbias_n 10u
XM8 vbias_n vbias_n vss vss sky130_fd_pr__nfet_01v8 {dev('M8')}
.ends otabias
"""
    # Machine-readable sizing, so downstream tools (Monte Carlo, layout hand-off)
    # consume the same numbers the netlist was built from rather than re-deriving them.
    sizes = {}
    for d in devices:
        key = d["name"].split()[0]
        m = max(1, math.ceil(d["W"] / W_MAX_FINGER))
        sizes[key] = {"I": d["I"], "gm": d["gm"], "gm_id": d["gm_id"],
                      "W_total_um": d["W"], "m": m, "W_finger_um": d["W"] / m,
                      "L_um": 1.0}
    meta = {"devices": sizes, "cc_f": CC, "rz_ohm": rz, "cl_f": CL,
            "vdd": VDD, "i_total_a": itot,
            "targets": {"gbw_hz": GBW, "sr_v_per_s": I_TAIL / CC}}
    (pathlib.Path(args.out).parent / "sizing.json").write_text(json.dumps(meta, indent=2))

    outp = pathlib.Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(netlist)
    print(f"\nnetlist -> {outp}")


if __name__ == "__main__":
    main()
