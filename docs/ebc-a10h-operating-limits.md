# ZKETECH EBC-A10H operating limits

This note answers the “EBC-10” question for the **EBC-A10H** model. These are
the tester's documented setting ranges and hardware/power caveats; they are
not recommended charge settings for a particular battery.

## Documented ranges

| Function | Minimum | Maximum | Step / qualification |
| --- | ---: | ---: | --- |
| Charge current | 0.05 A | 5.00 A | 0.01 A; current is reduced automatically when power limits are exceeded |
| Discharge current | 0.05 A | 10.00 A | 0.01 A; the manual also says the maximum current is constrained by power-current limits |
| Voltage setting / discharge cutoff | 0.00 V | 30.00 V | 0.01 V |
| Charge voltage | 0.00 V setting floor | `min(30.00 V, supply voltage − 2 V)` | 0.01 V; the charge ceiling is supply-dependent |

The EBC-A10H manual gives the current and voltage ranges on its specification
page and repeats the charge-voltage rule in its precautions. Its examples
describe discharge as stopping when the measured voltage falls below the
configured cutoff. The same voltage field is used for discharge cutoffs and
for the CCCV charge target, while ordinary NiMH/NiCd/LiPo/LiFe/Pb charging
selects a cell count rather than an arbitrary voltage.

For the common 24 V-powered A10H, the documented charge ceiling is therefore
22 V. Verify the actual supply/nameplate before relying on that number. The
manual is internally inconsistent: its specification section says DC 19–24 V,
while the connection section says DC 12–19 V. The unambiguous rule is the
relative one: charging must stay at least 2 V below the tester's supply.

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

The values above are not currently encoded as a complete model capability
table. The protocol's two-byte encoder can represent values outside the
A10H's physical range, and `ebcc.init` supplies opcodes/scaling rather than
numeric safety limits. Live commands should therefore validate against the
documented A10H limits before transmission; packet encoding alone is not
evidence that a setpoint is safe or supported.

The local implementation currently validates its profile-specific policy
ranges, but does not yet expose a general A10H capability object. Adding that
model-level envelope is a useful follow-up before enabling additional source
modes.

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

The manual mirror is used because the manufacturer's site exposes the product
page and downloads dynamically; the PDF itself identifies ZKETECH as the
copyright holder and links to `zketech.com`.
