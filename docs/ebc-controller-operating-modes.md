# Operating modes from EBC-controller

This is a source-derived catalog of the operating modes in the local
EBC-controller Lazarus project. A packet definition in the GUI source is
evidence of what the GUI sends, not a hardware capture or a guarantee that
every EBC model accepts the same command.

Sources inspected:

- /home/jay/src/EBC-controller/ebcc.init
- /home/jay/src/EBC-controller/main.pas
- /home/jay/src/EBC-controller/stepform.pas
- /home/jay/src/EBC-controller/stephelp.txt
- [upstream `ebcc.init`](https://raw.githubusercontent.com/JOGAsoft/EBC-controller/main/ebcc.init)

## Command frame

The GUI treats every command as a ten-byte frame:

~~~text
FA  opcode  p1-hi p1-lo  p2-hi p2-lo  timer-hi timer-lo  xor  F8
~~~

The Start, Adjust, and Cont values in ebcc.init are templates. Their payload
and checksum bytes are zero in the file; MakePacket2 fills in the payload and
SendData computes the checksum immediately before transmission.

The source checksum is the XOR of bytes 1 through 7 in zero-based terms
(Pascal string positions 2 through 8): opcode and the six payload bytes. The
FA prefix, checksum field, and F8 suffix are excluded.

The two-byte timer is encoded as:

~~~text
timer-hi = minutes div 240
timer-lo = minutes - (timer-hi * 240)
~~~

The GUI silently changes exactly 250 minutes to 249 because 250 is treated as
a forbidden device value. Values are not otherwise range-checked before the
Pascal Chr calls.

For current and voltage fields, the source uses the same packed representation
with different units:

~~~text
scaled = factor * value * 1000
hi = trunc(scaled / 2400)
lo = trunc(scaled - 2400 * hi) div 10

current decode = (2400 * hi + 10 * lo) / 1000 / IFactor
voltage decode = (2400 * hi + 10 * lo) / 10000 / UFactor
~~~

For charge modes, p1 is encoded current. For ordinary chemistry charging, p2
is 00, round(cells); the cell count is not encoded using the voltage
representation. For CCCV charging, p2 is encoded target voltage instead. For
all discharge modes, p2 is encoded cutoff voltage.

Constant-power discharge uses a separate source formula:

~~~text
hi = round(power / 240)
lo = trunc(power - 240 * hi)
~~~

The source declares a PFactor configuration key but never reads or applies
it. The power formula can produce a negative lo for some values between
roughly 120 W and 240 W, which is not a valid byte for Chr; a Python port
should validate this rather than blindly reproducing an invalid frame.

## Device modes

Section numbers below are .init configuration sections, not model IDs.
Opcode values are the second byte after the FA prefix. AutoOff is a status
state byte expected in responses when the device ends that mode; it is not part
of the command frame.

| Section | Command | Name | Type / parameter | Start | Adjust | Cont | AutoOff | Cell-voltage hint |
| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | c_NiMH | NiMH - Nickel-metal Hydride | Charge / current + cells | 21 | — | 28 | 16 | none |
| 2 | c_NiCd | NiCd - Nickel-cadmium | Charge / current + cells | 31 | — | 38 | 17 | none |
| 4 | c_LiPo | LiPo - Lithium Ion or Polymer | Charge / current + cells | 41 | — | 48 | 18 | 4.2 V/cell |
| 5 | c_LiFe | LiFe - Lithium Iron Phosphate | Charge / current + cells | 51 | — | 58 | 19 | 3.65 V/cell |
| 6 | c_Pb | PbAc - Lead Acid | Charge / current + cells | 61 | — | 68 | 1A | 2.4 V/cell |
| 7 | c_CCCV | CCCV - CC and CV charging | ChargeCV / current + target voltage | 71 | — | 78 | 1B | 5 default UI value |
| 9 | d_CC | CC - Constant Current | Discharge / current + cutoff voltage | 01 | 07 | 08 | 14 | — |
| 10 | d_CP | CP - Constant Power | Discharge / power + cutoff voltage | 11 | — | 18 | 15 | — |
| 11 | d_CR | CR - Constant Resistance (SW) | Discharge / resistance + cutoff voltage | 01 | 07 | 08 | FF | — |

Each opcode above expands to the common frame. For example, c_Pb starts with
FA 61 00 00 00 00 00 00 00 F8 and continues with FA 68 followed by the
payload and checksum.

### Software constant resistance

d_CR is not a distinct device command. It reuses the CC start, adjust, and
continue frames. The GUI computes a requested current from the latest measured
voltage:

~~~text
requested_current = latest_voltage / requested_resistance
~~~

It sends an Adjust frame every third valid status sample while in CR mode.
AutoOff=FF means there is no CR-specific device completion state; the GUI stops
on its generic zero-current condition (after the first few samples), an
explicit cutoff, or operator action. Starting CR also depends on a previously
received voltage sample (FLastU).

## Host program controls

Sections 12 and 13 are control steps and do not transmit device packets:

| Command | Behavior |
| --- | --- |
| Wait t | Host-side wait for t minutes; the GUI arms a timer and sends no command. |
| Loop n | Host-side restart of the step list, running it n more times. Loop 0 becomes 999999. |
| Loop CAPI | Continue cycling while latest discharge capacity is not lower than the previous cycle. |
| Loop ENEI | Parser flag for energy-increase looping; the corresponding runtime test is commented out in the source. |
| STOP / END | End the host program and issue the common stop frame. |

The GUI also has a non-program Monitor run state. It records status frames
without starting a profile and does not correspond to an .init command.

## Step-language and cutoff caveats

The documented step syntax is:

~~~text
c_[NiMH|NiCd|LiPo|LiFe|Ac] Cells=n Curr=I [CutA=I|Cap=C|Energy=W|Time=t]
d_[CC|CP|CR] [Curr=I|Power=P|Res=R] CutV=U [Cap=C|Energy=W|Time=t]
Wait t
Loop n
~~~

The implementation also recognizes VOLT= for c_CCCV, and CAPI and ENEI for
loops. The help text says CutC, but the parser checks for a token starting
with CAP; this spelling mismatch should not be copied unnoticed.

Time=t is placed into the device timer field for charge/discharge steps.
CutAT=minutes is a separate charge-only host dwell after current falls below
CutA. The source checks charge current and capacity cutoffs, but its energy
cutoff test is not active; discharge-side host cutoff logic is also much less
complete than the help text suggests. A CLI implementation should model these
as explicit host-side policy rather than implying that the tester enforces
every documented cutoff.

## Porting recommendation

The source supports six chemistry charge families, CC discharge, CP discharge,
and software CR. The safest CLI shape is to make each mode a typed profile with
an explicit payload strategy:

1. chemistry charge: current + cell count;
2. CCCV charge: current + target voltage;
3. constant-current discharge: current + cutoff voltage;
4. constant-power discharge: source-specific power + cutoff voltage, with
   strict byte-range validation;
5. constant-resistance discharge: feedback-controlled current + cutoff voltage,
   requiring status samples and periodic adjustment.

Wait, Loop, and END belong in a future host step-program layer, not in the
serial profile builder. The .init data provides no additional model IDs:
model identity and scaling remain the separate EBC-A05/EBC-A10H registry
concern.

The Python port now implements the catalog and all five payload strategies as
checksum-finalized, read-only builders in ``ebc_cli.modes``.  ``ebc modes``
lists the source catalog and ``ebc plan-mode`` exercises those builders without
opening a serial device.  Live transmission remains limited to the existing
validated A10H profiles until captures justify expanding that gate.
