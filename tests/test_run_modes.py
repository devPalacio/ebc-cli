"""Exercise the live command path without opening a physical tester."""

import contextlib
import unittest
from io import StringIO
from unittest.mock import patch

from ebc_cli import commands, session
from ebc_cli.capabilities import A10H_CAPABILITIES
from ebc_cli.cli import main
from ebc_cli.device import DeviceError
from ebc_cli.modes import build_mode_packet
from ebc_cli.protocol import checksum, parse_status
from ebc_cli.resources import CommandResources


def status(state=0x68, current=0.5):
    raw = bytearray.fromhex("FA68003235AF000000000000000000000600F8")
    raw[1] = state
    raw[2:4] = bytes((0, round(current * 100)))
    raw[17] = checksum(bytes(raw), 17)
    return parse_status(bytes(raw))


class Tester:
    def __init__(self, samples):
        self.samples = iter(samples)
        self.frames = []
        self.stop_calls = 0

    def begin_mode(self, command):
        baseline = status()
        if command.mode.command == "d_CR":
            command = command.with_observed_voltage(baseline.voltage_volts)
        self.frames.append(build_mode_packet(command, capabilities=A10H_CAPABILITIES))
        return baseline

    def next_status(self, **kwargs):
        sample = next(self.samples)
        if isinstance(sample, Exception):
            raise sample
        return sample

    def status(self, **kwargs):
        raise DeviceError("poll failed")

    def adjust_mode(self, command):
        self.frames.append(build_mode_packet(command, capabilities=A10H_CAPABILITIES))

    def stop(self):
        self.stop_calls += 1


class RunModeTests(unittest.TestCase):
    def run_mode(self, mode_args, samples):
        tester = Tester(samples)
        resources = CommandResources(tester=tester, logger=None, log_path=None)
        output = StringIO()
        errors = StringIO()
        real_run = session.run_active_session

        def run_without_wait(**kwargs):
            return real_run(**kwargs, clock=lambda: 0.0, sleeper=lambda seconds: None)

        with (
            patch.object(
                commands, "_open_resources", return_value=contextlib.nullcontext(resources)
            ),
            patch.object(commands, "run_active_session", side_effect=run_without_wait),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(["run-mode", *mode_args, "--execute", "--i-am-present", "--no-log"])
        return code, tester, output.getvalue(), errors.getvalue()

    def test_auto_off_finishes_each_charge_and_direct_discharge_mode(self):
        cases = (
            (["c_NiMH", "--current", "0.5", "--cells", "4"], 0x21, 0x16),
            (["c_NiCd", "--current", "0.5", "--cells", "4"], 0x31, 0x17),
            (["c_LiPo", "--current", "0.5", "--cells", "2"], 0x41, 0x18),
            (["c_LiFe", "--current", "0.5", "--cells", "4"], 0x51, 0x19),
            (["c_Pb", "--current", "0.5", "--cells", "6"], 0x61, 0x1A),
            (["c_CCCV", "--current", "0.5", "--voltage", "13.7"], 0x71, 0x1B),
            (["d_CC", "--current", "0.5", "--cutoff-voltage", "10.5"], 0x01, 0x14),
            (["d_CP", "--power", "10", "--cutoff-voltage", "10.5"], 0x11, 0x15),
        )
        for args, opcode, auto_off in cases:
            with self.subTest(mode=args[0]):
                code, tester, output, _ = self.run_mode(args, [status(auto_off)])
                self.assertEqual(code, 0)
                self.assertEqual(tester.frames[0][1], opcode)
                self.assertEqual(tester.stop_calls, 1)
                self.assertIn("auto-off reached", output)

    def test_cr_adjusts_after_three_samples_and_stops_on_fourth_zero_sample(self):
        code, tester, output, _ = self.run_mode(
            ["d_CR", "--resistance", "10", "--cutoff-voltage", "10.5"],
            [status(0x08), status(0x08), status(0x08), status(0x08, current=0)],
        )
        self.assertEqual(code, 0)
        self.assertEqual([frame[1] for frame in tester.frames], [0x01, 0x07])
        self.assertEqual(tester.frames[0][2:4], bytes((0, 128)))
        self.assertEqual(tester.stop_calls, 1)
        self.assertIn("current reached zero", output)

    def test_serial_failure_attempts_stop_and_returns_error(self):
        code, tester, _, error = self.run_mode(
            ["d_CC", "--current", "0.5", "--cutoff-voltage", "10.5"],
            [DeviceError("stream failed")],
        )
        self.assertEqual(code, 2)
        self.assertEqual(tester.stop_calls, 1)
        self.assertIn("poll failed", error)

    def test_invalid_settings_are_rejected_before_opening_serial(self):
        cases = (
            ["c_Pb", "--current", "6", "--cells", "6"],
            ["c_Pb", "--current", "0.5", "--cells", "10"],
            ["c_CCCV", "--current", "0.5", "--voltage", "23"],
            ["d_CC", "--current", "0.5", "--cutoff-voltage", "31"],
            ["d_CP", "--power", "121", "--cutoff-voltage", "10.5"],
            ["d_CP", "--power", "0.5", "--cutoff-voltage", "10.5"],
            ["d_CP", "--power", "10.5", "--cutoff-voltage", "10.5"],
            ["d_CR", "--resistance", "nan", "--cutoff-voltage", "10.5"],
            ["d_CR", "--resistance", "0", "--cutoff-voltage", "10.5"],
            ["d_CR", "--resistance", "10", "--cutoff-voltage", "nan"],
            ["c_Pb", "--current", "0.5", "--cells", "6", "--timeout-minutes", "0"],
            ["c_Pb", "--current", "0.5", "--cells", "6", "--timeout-minutes", "61440"],
            ["c_Pb", "--current", "0.5", "--cells", "6", "--voltage", "13.7"],
            ["Wait"],
        )
        for args in cases:
            with (
                self.subTest(args=args),
                patch.object(commands, "_open_resources") as open_resources,
                contextlib.redirect_stderr(StringIO()),
            ):
                self.assertEqual(main(["run-mode", *args, "--execute", "--i-am-present"]), 2)
                open_resources.assert_not_called()

    def test_run_mode_requires_both_acknowledgements(self):
        for flags in ([], ["--execute"], ["--i-am-present"]):
            with (
                self.subTest(flags=flags),
                patch.object(commands, "_open_resources") as open_resources,
                contextlib.redirect_stderr(StringIO()),
            ):
                self.assertEqual(main(["run-mode", "c_Pb", *flags]), 2)
                open_resources.assert_not_called()

    def test_monitor_rejects_nonfinite_intervals_before_open(self):
        for interval in ("nan", "inf", "-inf"):
            with (
                self.subTest(interval=interval),
                patch.object(commands, "_open_resources") as open_resources,
                contextlib.redirect_stderr(StringIO()),
            ):
                self.assertEqual(main(["monitor", f"--interval={interval}"]), 2)
                open_resources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
