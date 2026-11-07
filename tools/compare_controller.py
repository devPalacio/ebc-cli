"""Compare CLI protocol metadata with an explicit controller checkout."""

import argparse
import configparser
from pathlib import Path

from ebc_cli.modes import SOURCE_OPERATING_MODES
from ebc_cli.protocol import MODEL_REGISTRY


def compare(source: Path) -> list[str]:
    ini = configparser.ConfigParser(interpolation=None)
    with source.open(encoding="utf-8") as handle:
        ini.read_file(handle)
    errors = []

    def check(label, actual, expected):
        if actual != expected:
            errors.append(f"{label}: CLI {actual!r}, controller {expected!r}")

    sections = {section for section in ini.sections() if section.isdecimal()}
    check("mode sections", {str(mode.section) for mode in SOURCE_OPERATING_MODES}, sections)
    for mode in SOURCE_OPERATING_MODES:
        section = str(mode.section)
        if section not in ini:
            continue
        row = ini[section]
        check(f"{section} command", mode.command, row["Command"])
        check(f"{section} type", mode.mode_type.value, row["Type"])
        for field, opcode in (
            ("Start", mode.start_opcode),
            ("Cont", mode.cont_opcode),
            ("Adjust", mode.adjust_opcode),
        ):
            template = bytes.fromhex(row.get(field, ""))
            expected = (
                bytes((0xFA, opcode, 0, 0, 0, 0, 0, 0, 0, 0xF8)) if opcode is not None else b""
            )
            check(f"{section} {field}", expected, template)
        auto_off = row.get("AutoOff")
        check(f"{section} auto-off", mode.auto_off_code, int(auto_off, 16) if auto_off else None)
        voltage = row.get("CellVoltageInfo")
        check(
            f"{section} cell voltage", mode.cell_voltage_volts, float(voltage) if voltage else None
        )
        if "TestVal" in row:
            check(f"{section} value kind", mode.test_value, row["TestVal"])

    model_names = set(ini["Models"].values())
    check("model names", {model.name for model in MODEL_REGISTRY.values()}, model_names)
    for model in MODEL_REGISTRY.values():
        if model.name not in ini:
            errors.append(f"missing model section {model.name}")
            continue
        row = ini[model.name]
        check(f"{model.name} ID", model.model_id, int(row["Ident"], 16))
        check(f"{model.name} current factor", model.current_factor, float(row["IFactor"]))
        check(f"{model.name} voltage factor", model.voltage_factor, float(row["UFactor"]))
        for field, packet in (
            ("Connect", model.connect_packet),
            ("Disconnect", model.disconnect_packet),
            ("Stop", model.stop_packet),
        ):
            check(f"{model.name} {field}", packet, bytes.fromhex(row[field]))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="controller ebcc.init path")
    args = parser.parse_args()
    errors = compare(args.source)
    if errors:
        print("\n".join(errors))
        return 1
    print(
        f"Matched {len(SOURCE_OPERATING_MODES)} mode entries and {len(MODEL_REGISTRY)} model definitions."
    )
    print("Metadata equality only. This does not validate firmware or licensing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
