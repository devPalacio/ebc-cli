# ZKETECH EBC-A10H operating limits

These are the **EBC-A10H** tester's documented setting ranges and power
limits. They are
not recommended charge settings for a particular battery.

## Documented ranges

| Function | Minimum | Maximum | Step / qualification |
| --- | ---: | ---: | --- |
| Charge current | 0.05 A | 5.00 A | 0.01 A; current is reduced automatically when power limits are exceeded |
| Discharge current | 0.05 A | 10.00 A | 0.01 A; the manual also says the maximum current is constrained by power-current limits |
| Voltage setting / discharge cutoff | 0.00 V | 30.00 V | 0.01 V |
| Charge voltage | 0.00 V setting floor | 22.00 V | 0.01 V; AC-powered A10H variant |

The AC-powered A10H manual variant gives a fixed 0–22 V charging range. Its
discharge setting range remains 0–30 V. The same voltage field is used for
discharge cutoffs and for the CCCV charge target, while ordinary
NiMH/NiCd/LiPo/LiFe/Pb charging selects a cell count rather than an arbitrary
voltage.

## Power envelope

ZKETECH's current site presents the multifunction tester class as
0–30 V / 10 A / 150 W. Those figures are not simultaneous independent maxima:
30 V at 10 A would be 300 W. Treat 150 W as the active power/thermal ceiling
when using the load, subject to the particular unit's firmware and supply.
For a conservative constant-current discharge envelope, that implies

```text
I <= min(10 A, 150 W / V)
```

for positive battery voltage: 10 A is within 150 W up to 15 V, while 30 V
would be limited to about 5 A. The old manual does not state the 150 W number
in its specification table, so this envelope is a product-page rating plus a
power calculation, not a captured firmware rule.

The manual's phrase “maximum current should be power current - 0.5 A” is
ambiguous in the discharge-current bullet. Its precaution clarifies the
intended safety rule for **charging batteries above 10 V**: charging current
should be below the external supply current minus 0.5 A. In all cases, the
tester can reduce current when its power limit is exceeded.

## What this means for the CLI

The local implementation exposes these values as the `A10H_CAPABILITIES`
model envelope. The A10H control profile builders and validated live profiles
check the manual's current and voltage ranges and 0.01-unit resolution before
a frame can be built. For this AC-powered unit, the envelope uses the fixed
22 V charge ceiling documented for the AC variant.

The envelope applies the product site's advertised 150 W class rating as a
conservative constant-power ceiling for CLI requests. It is an electrical
product envelope, not a claim about a particular firmware revision's internal
protection behavior.

## Sources

1. [ZKETECH EBC-A10H product page](https://www.zketech.com/en/pd.jsp?id=17) —
   manufacturer product identity and current product documentation entry.
2. [ZKETECH product site](https://www.zketech.com/en/) — manufacturer site
   summary advertising the multifunction tester class as 0–30 V / 10 A / 150 W.
3. [EBC-A10H User Manual (PDF)](https://base.sato-power.com/thtimages/Suoer_Charger220V/EBC_A10H_manual.pdf)
   — manufacturer-authored manual (ZKETECH copyright, release 2015-06-05),
   hosted as a PDF mirror. See PDF page 2 for the ranges and PDF page 7 for
   the charge-voltage and charge-current precautions.
4. [EBC-controller `ebcc.init`](https://raw.githubusercontent.com/JOGAsoft/EBC-controller/main/ebcc.init)
   and [protocol implementation](../src/ebc_cli/protocol.py) — model IDs,
   scaling, opcodes, and packet representation; neither is a substitute for
   the manufacturer's electrical ratings.
5. [Seeed Studio's AC EBC-A10H listing](https://www.seeedstudio.com/ZKETECH-EBC-A10H-AC-Electronic-Load-Battery-Capacity-Tester-Powe-Supply-Tester-30V-10A-150W-p-4522.html)
   — the AC-powered product variant and its 30 V / 10 A / 150 W product
   designation.
6. [AC-variant EBC-A10H manual summary](https://manuals.plus/asin/B08CDC6BVL)
   — the 100–240 V AC input and fixed 0–22 V charging range used by this
   model envelope.

The manual mirror is used because the manufacturer's site exposes the product
page and downloads dynamically; the PDF itself identifies ZKETECH as the
copyright holder and links to `zketech.com`.
