# analog-pmic-sky130

A two-stage Miller-compensated OTA and the LDO regulator built around it, in SKY130
130 nm. Every transistor is sized from measured gm/Id data, every specification is
measured rather than asserted, and the design is verified across 15 PVT corners and a
300-sample Monte Carlo.

## Results

### OTA, typical corner, 27 °C

| specification | target | measured |
|---|---|---|
| DC gain | ≥ 60 dB | **76.06 dB** |
| unity-gain frequency | 10 MHz | **9.28 MHz** |
| phase margin | ≥ 60° | **81.15°** |
| gain margin | — | 24.53 dB |
| slew rate | 10 V/µs | 8.72 V/µs |
| input common-mode range | — | 0.94 – 1.38 V |
| CMRR | — | 56.19 dB |
| PSRR | — | 53.47 dB |
| input-referred noise @ 1 kHz | — | 701 nV/√Hz |
| systematic offset | — | −398 µV |
| supply current | — | 96.2 µA (173 µW) |

### Across 15 PVT corners — 5 process × 3 temperatures, all passing

| specification | worst case | corner |
|---|---|---|
| DC gain | **66.61 dB** | sf, −40 °C |
| unity-gain frequency | 7.49 MHz | ss, 125 °C |
| phase margin | **76.40°** | sf, 125 °C |
| gain margin | 22.20 dB | ff, −40 °C |
| slew rate | 7.84 V/µs | sf, 125 °C |
| ICMR width | 370 mV | sf, −40 °C |
| CMRR | 35.96 dB | sf, −40 °C |

`sf` — slow nfet, fast pfet — is the worst corner on every gain-related specification,
and that is exactly what the topology predicts: the NMOS input pair weakens at the same
time the PMOS mirror load strengthens, so first-stage gain is squeezed from both sides.
DC gain drops 9.5 dB and CMRR drops 20 dB there. `fs` is the best corner for the mirror
image of the same reason. Phase margin never falls below 76°, so the design is
overcompensated by roughly 16° everywhere — real bandwidth left on the table, and a
deliberate choice rather than an accident.

Reproduce with `python3 scripts/characterize.py --corners tt ff ss sf fs --temps -40 27 125`.

### Monte Carlo offset, 300 samples

| | |
|---|---|
| σ(V_os), simulated | **3.26 mV** |
| σ(V_os), hand calculation | 3.13 mV |
| agreement | 4 % |
| mean | −0.17 mV |
| systematic (zero mismatch) | −398 µV |
| converged | 300 / 300 |

The two numbers are independent routes to the same answer — one from 300 SPICE solves,
one from Pelgrom coefficients and a gm ratio on paper — which is the point of computing
both.

### LDO, 1.8 V → 1.2 V

| specification | measured |
|---|---|
| output voltage | 1.2003 V |
| load regulation, 0.1 → 20 mA | **18 µV/mA** (0.36 mV total) |
| dropout onset | ≈ 22 mA |
| PSRR @ 1 kHz | −58.3 dB |
| DC loop gain | 94.1 dB |
| loop UGF | 47 kHz (0.1 mA) → 206 kHz (10 mA) |

**The loop needs output-capacitor ESR to be stable, and light load is the binding
corner.** With an ideal 1 µF capacitor the phase margin is 1–9 % of what it needs to be:

| ESR | PM @ 0.1 mA | PM @ 10 mA |
|---:|---:|---:|
| 0.05 Ω | 2.7° | 4.8° |
| 0.2 Ω | 5.2° | 15.9° |
| 0.5 Ω | 10.3° | 36.7° |
| 1 Ω | 18.6° | 63.1° |
| 2 Ω | 34.5° | 82.7° |

This is the classic LDO stability problem rather than a defect in this particular design:
94 dB of loop gain through an error amplifier *and* a pass device, against an output pole
that moves with load. The ESR zero is what pulls the phase back, which is why every
commercial LDO datasheet publishes a stable-ESR window instead of a single capacitor
value. Light load is hardest because the output resistance is highest there, putting the
output pole at its lowest frequency. Stated plainly: as it stands this regulator is
specified *with* its capacitor, not independently of it.

## Design decisions, and the measurements behind them

**NMOS input pair, PMOS load.** Not a preference — a consequence. The measured SKY130
thresholds are strongly asymmetric: nfet 0.577 V against pfet **1.03 V**. On a 1.8 V rail
a PMOS input pair spends over half the supply turning on and its common-mode range
collapses. The pfets go where their ~2x higher intrinsic gain pays and their headroom
cost does not: the mirror load and the second stage.

**Inversion levels chosen for headroom, not gain.** `GM_ID_LOAD = 14` and
`GM_ID_TAIL = 10` are set high deliberately. Every 100 mV of load overdrive comes
straight out of the input common-mode range, so running M3/M4 weaker drops |Vgs3| toward
|Vth| and buys ICMR back at the top, while a weaker tail drops Vdsat5 and buys it back at
the bottom. The first version used gm/Id = 10 and 6 and produced an ICMR only ~100 mV
wide.

**Pass device at L = 0.15 µm** while the OTA uses 1 µm everywhere. Output resistance is
irrelevant for a pass device — it delivers current, it is not a gain stage — and
short-channel current density is what keeps its width manageable.

**No feedback divider in the LDO.** A conventional LDO divides V_OUT down to a 0.6–0.8 V
reference so one reference serves many outputs. The error amplifier's ICMR is
0.94–1.38 V, so a 0.6 V feedback node sits outside it and the loop simply would not
regulate. V_OUT is therefore fed back directly with V_REF at 1.2 V, mid-ICMR. The cost is
that V_OUT is no longer programmable without moving V_REF. Widening the ICMR — a
rail-to-rail input stage, or a folded cascode with a PMOS pair for the low end — is what
would buy the divider back.

## Four defects found during design

Each is recorded because *how it presented* is the useful part; none announced itself.

**1. Systematic offset condition violated — the output sat at 97 mV.** M6 and M7 were
sized independently from their own gm/Id targets. But M6's gate is tied to the M3/M4
diode voltage, so its current is *mirrored from M4* and is not a free parameter. M6
delivered 54.9 µA while M7 was sized to sink 95 µA; the output collapsed until M7 left
saturation (V_ds = 97 mV against V_dsat = 286 mV). The fix is the Allen–Holberg
condition

```
W6/W4 = 2 · W7/W5
```

with W6 and W7 *derived* from the mirror ratios rather than chosen. Enforcing it gave a
−398 µV systematic offset and dropped power from 225 µW to 173 µW.

**2. PDK parameter/subcircuit name collision.** `critical.spice` contains
`.param sky130_fd_pr__pfet_01v8 = 0.0` — the exact name of the pfet subcircuit. ngspice
binds the name to the parameter, so every pfet instantiation fails with `unknown subckt`
while nfet works perfectly, because no equivalent line exists for nfet. That asymmetry is
what made it hard to find.

**3. Model bins stop at W = 100 µm.** M6 wanted 139 µm and matched no bin, so ngspice
rejected it outright. Wide devices are now emitted as `m` parallel fingers, which is what
a layout would draw regardless.

**4. LDO feedback returned to the wrong input.** A PMOS pass device in common-source is
itself an inverting stage, so the loop already contains one inversion. Routing feedback
to the amplifier's inverting input adds a second and makes the loop *positive*: the
regulator sat with the pass device fully on, V_OUT pinned near V_IN and drooping with
load instead of regulating. Feedback belongs on the non-inverting input here.

### Three measurement errors, which matter just as much

A wrong measurement is indistinguishable from a wrong design until you check it.

- **Phase in radians, reported as degrees.** ngspice's `vp()` returns radians. Taken at
  face value, a phase margin of 81° reads as 178° — a number that should be impossible
  and is therefore a useful tell.
- **ICMR measured by "does the buffer track its input".** At V_in = 0 the input pair is
  off, V_out sits at 0 too, and the buffer "tracks" perfectly while amplifying nothing.
  Taking min/max over all tracking points reported an ICMR starting at 0 V. It is now
  measured as the widest contiguous span where the input-stage devices stay saturated.
- **LDO loop gain that ignored its own load.** Breaking the loop with a large inductor
  also ties V_OUT to the ideal reference at DC, so the load current flowed out through
  that source instead of the pass device and every load reported an identical loop gain.
  Series voltage injection leaves the DC path intact and fixes it.

## Layout

```
scripts/gmid data    nfet/pfet characterisation curves (from gmid-char-sky130)
scripts/size_ota.py  gm/Id sizing -> ota/ota_core.spice + ota/sizing.json
scripts/characterize.py  full spec extraction across corners and temperature
scripts/mc_offset.py     Monte Carlo offset from PDK Pelgrom coefficients

ota/ota_core.spice   the amplifier, generated -- edit the script, not this
ota/tb_op.spice      operating point and saturation check
ota/tb_ac.spice      open-loop gain, UGF, phase margin
ota/sizing.json      machine-readable device sizes

ldo/ldo.spice        regulator: pass device + OTA error amplifier
ldo/tb_ldo_dc.spice  load regulation
ldo/tb_ldo_ac.spice  line regulation and PSRR
ldo/tb_ldo_loop.spice  loop gain vs load and vs output-cap ESR

models/              minimal per-corner SKY130 1.8 V libraries
```

## Reproducing

```bash
python3 scripts/size_ota.py                        # sizing + netlist
ngspice -b ota/tb_op.spice                         # saturation check
python3 scripts/characterize.py                    # tt/27C spec table
python3 scripts/characterize.py --corners tt ff ss sf fs --temps -40 27 125
python3 scripts/mc_offset.py --n 300 --seed 1      # offset distribution
ngspice -b ldo/tb_ldo_dc.spice                     # load regulation
ngspice -b ldo/tb_ldo_loop.spice                   # loop stability vs ESR
```

Requires `ngspice` and Python 3. No commercial tools, no licences.

## Scope

Schematic-level only: no layout, so no extracted parasitics and no post-layout
verification — the numbers above are pre-layout and will degrade. No bandgap reference
yet, so the LDO runs from an ideal V_REF; the SKY130 parasitic PNP needed to build one is
available and that is the natural next block. The LDO is specified with its output
capacitor's ESR rather than independently of it. Each of these is a stated limitation
rather than an omission.
