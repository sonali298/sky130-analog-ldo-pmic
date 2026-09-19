# Replicating the OTA in Cadence Virtuoso

A lab session plan. Target: schematic entry, a working DC operating point, and an AC
response you can compare against the numbers below.

**Why this is worth your lab time.** Everything in this repo runs on ngspice against an
open-source PDK, which is a legitimate flow but not the one TI uses. Reproducing this in
Virtuoso + Spectre is what makes "Cadence Virtuoso, Spectre" true on your CV, and TI is a
Cadence shop. Two hours here is worth more than another week of simulation elsewhere.

---

## First: find out which PDK the lab has

This matters more than anything else on this page. Ask a TA or check the library manager
for one of:

| PDK | node | what to expect |
|---|---|---|
| `gpdk180` | 180 nm | most common in university labs |
| `gpdk045` | 45 nm | less common |
| SCL 180 nm | 180 nm | some Indian institutions |
| TSMC / UMC via CUP | varies | under NDA, ask before assuming |

**The widths below will NOT transfer directly to a different process.** Thresholds,
mobility and current density all change. What *does* transfer is everything that matters:
the topology, the currents, the gm targets, and the gm/Id inversion levels. Re-derive the
widths in the lab PDK using the same method — that is the point of the method.

---

## Design intent (transfers to any process)

| device | role | current | gm/Id | gm |
|---|---|---:|---:|---:|
| M1, M2 | NMOS input pair | 10 µA each | 12.57 | 126 µS |
| M3, M4 | PMOS mirror load | 10 µA each | 14.0 | 140 µS |
| M5 | NMOS tail | 20 µA | 10.0 | 200 µS |
| M6 | PMOS second stage | 54.3 µA | 14.0 | 760 µS |
| M7 | NMOS second-stage sink | 54.3 µA | 10.0 | 543 µS |
| M8 | NMOS bias diode | 10 µA (I_ref) | 10.0 | 100 µS |

Compensation: **Cc = 2 pF**, **Rz = 4.6 kΩ**, load **CL = 5 pF**.

Two constraints that are not optional, whatever the process:

**1. gm1 is fixed by the bandwidth target.** `GBW = gm1 / Cc`, so 10 MHz into 2 pF
demands gm1 = 126 µS. With 10 µA per input device that forces gm/Id = 12.57.

**2. M6 and M7 widths are derived, never chosen.** M6's gate sits on the M3/M4 diode
voltage, so its current is mirrored from M4 and its inversion level equals theirs. The
Allen–Holberg systematic offset condition must hold:

```
W6 / W4  =  2 · W7 / W5
```

Violating this is what put the output at 97 mV in the first version of this design, with
M7 in triode. Set W6 = W4 · (I6/I1) and W7 = W5 · (I6/I_tail) and it holds by
construction.

---

## SKY130 widths (reference point, L = 1 µm unless noted)

| device | W total | fingers | W per finger |
|---|---:|---:|---:|
| M1, M2 | 3.47 µm | 1 | 3.47 µm |
| M3, M4 | 25.68 µm | 1 | 25.68 µm |
| M5 | 4.12 µm | 1 | 4.12 µm |
| M6 | 139.43 µm | **3** | 46.48 µm |
| M7 | 11.18 µm | 1 | 11.18 µm |
| M8 | 2.06 µm | 1 | 2.06 µm |

LDO pass device: W = 45 µm, **L = 0.15 µm**, m = 4.

---

## Re-sizing in the lab PDK (30 minutes, do this first)

1. Place one NMOS, W = 10 µm, L = 1 µm. Source and bulk to ground, drain at VDD/2.
2. DC sweep Vgs from 0 to VDD.
3. Plot **gm/Id vs Vgs** and **Id/W vs Vgs**. In ADE: `OP("/M1" "gm") / OP("/M1" "id")`.
4. For each device above, read `Id/W` at its target gm/Id, then `W = I_target / (Id/W)`.
5. Repeat for PMOS.

That is `scripts/size_ota.py` done by hand, and it is the part worth being able to
explain in an interview.

---

## Schematic

```
                VDD
     ┌───────────┬────────────┬──────────────┐
     │           │            │              │
    M3 ├──gate──┤ M4         M6              │
   (diode)      │            │               │
     │          │            │               │
    s1x        s1o──────────┤gate            │
     │          │            │               │
     ├────┬─────┘            ├─── vout ──┬───┴── CL (5 pF)
     │    │                  │           │
    M2   M1                Rz─Cc         M7
  gate:  gate:             (s1o→vout)    gate: vbias_n
  vinn   vinp                            │
     └────┬─────┘                       GND
        tail
          │
         M5  gate: vbias_n
          │
         GND

Bias:  I_ref (10 µA) from VDD into M8 (diode-connected NMOS), M8 gate = vbias_n,
       driving the gates of M5 and M7.
```

Nets: `vinp vinn vout vdd vss vbias_n`, internal `tail s1x s1o z`.
`Rz` sits between `s1o` and `z`; `Cc` between `z` and `vout`.

---

## Testbenches, in order

### 1. DC operating point — do not skip this
Unity-gain feedback: tie `vinn` to `vout`, drive `vinp` with 1.2 V DC (or VDD/2 · 1.33 in
another process). Run a DC op point and **check every device for Vds > Vdsat**.

Expected (SKY130): every device saturated, `vout` = 1.1996 V against a 1.2 V input — a
−398 µV systematic offset.

If the output is stuck near a rail, the offset condition is violated. Go back and check
W6/W4 = 2·W7/W5.

### 2. AC open-loop — the headline measurement
Breaking the loop while keeping DC bias needs the standard trick:

- **Lfb = 1 TH** from `vout` to `vinn` — short at DC, open at AC
- **Cfb = 1 TF** from `vinn` to the common-mode reference — open at DC, short at AC
- AC source of 1 V on `vinp`

Sweep 0.1 Hz to 1 GHz, 200 points/decade. Plot dB20 and phase.

| | SKY130 result |
|---|---|
| DC gain | 76.06 dB |
| f_3dB | ≈ 1.5 kHz |
| UGF | 9.28 MHz |
| phase margin | 81.15° |
| gain margin | 24.53 dB |

**Spectre reports phase in degrees; ngspice reports radians.** If your phase margin comes
out near 178°, you are reading radians.

### 3. Transient slew rate
Unity gain, input pulse 0.9 V → 1.5 V, 10 ns edges, 5 µs width. Measure dV/dt between 30%
and 70% of the step, both edges.

Expected: **8.72 V/µs** both directions.

### 4. Corners, if time allows
ADE XL, five process corners × −40/27/125 °C. SKY130 gave worst-case 66.61 dB gain and
76.40° PM at slow-nfet/fast-pfet, −40 °C.

**Expect `sf` to be your worst corner too.** With an NMOS input pair and a PMOS mirror
load, slow-n/fast-p weakens the input pair exactly as the load strengthens, squeezing
first-stage gain from both sides. If some other corner is worst, something differs — and
finding out what is a better interview story than matching.

---

## What to capture

- DC op point annotated on the schematic (Virtuoso can display it directly)
- AC magnitude and phase plot with UGF and PM marked
- Transient step showing both slew edges
- The gm/Id curves from the re-sizing step
- The PDK name and Spectre version string

Enough to answer "what did you actually run this on" precisely.

---

## Talking points these results support

- **Why NMOS input, not PMOS** — measured thresholds: nfet 0.577 V, pfet 1.03 V against
  1.8 V. A PMOS pair's common-mode range collapses.
- **Why phase margin is 81° and not 60°** — deliberately overcompensated; the
  non-dominant pole sits at 2.42× GBW when 2.2× is the 60° condition. There is bandwidth
  available by shrinking Cc, at the cost of margin across corners.
- **Why the ICMR is only 440 mV** — bounded below by the tail's Vdsat, above by the input
  pair leaving saturation as vcm approaches the mirror gate voltage. Directly traceable to
  the pfet threshold.
- **Why the LDO has no feedback divider** — a 0.6 V feedback node would sit below the
  amplifier's ICMR.
