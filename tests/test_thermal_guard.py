"""The thermal guard's sensor resolution (MEMNG-354).

Every case here runs against a hwmon tree built in `tmp_path`, never `/sys`: the host this suite
runs on is also the production host, so reading the real machine would make these tests a function
of how warm it happened to be.

The defect being fixed was not that the guard was wrong about a temperature — it was that it read
a DIFFERENT SENSOR than the one it was named for, because it addressed it by index. hwmon indices
are assigned in probe order, so the tests build trees where the package sensor is NOT at index 0
and would be missed by any positional rule.
"""

from pathlib import Path

import pytest
from app.config import settings
from app.services import media


def _chip(root: Path, index: int, name: str, **inputs: tuple[str, str] | str) -> Path:
    """One hwmon chip directory: `name`, plus tempN_label/tempN_input pairs.

    A value of (label, reading) writes both files; a bare string writes only the input, which is
    what a driver that publishes no labels looks like (Saturn's k10temp).
    """
    chip = root / f"hwmon{index}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(f"{name}\n")
    for temp, spec in inputs.items():
        if isinstance(spec, tuple):
            label, reading = spec
            (chip / f"{temp}_label").write_text(f"{label}\n")
            (chip / f"{temp}_input").write_text(f"{reading}\n")
        else:
            (chip / f"{temp}_input").write_text(f"{spec}\n")
    return chip


@pytest.fixture
def hwmon(tmp_path, monkeypatch):
    """An empty hwmon tree wired into the guard, for a test to populate."""
    root = tmp_path / "hwmon"
    root.mkdir()
    monkeypatch.setattr(media, "HWMON_ROOT", root)
    monkeypatch.setattr(settings, "THERMAL_ZONE", "")
    return root


def test_the_package_sensor_is_found_by_chip_name_and_label_not_by_index(hwmon):
    """The whole ticket in one case: the package sits behind two other chips, and the one at index
    0 is the ambient zone the guard used to read. A positional rule picks acpitz and reports 28 °C
    for ever; the name rule picks the package."""
    _chip(hwmon, 0, "acpitz", temp1="28000")
    _chip(hwmon, 1, "nvme", temp1=("Composite", "41000"))
    _chip(hwmon, 5, "coretemp", temp1=("Package id 0", "87000"), temp2=("Core 0", "85000"))

    assert media.resolve_thermal_sensor() == hwmon / "hwmon5" / "temp1_input"
    assert media.read_temp_millideg() == 87000
    assert media.thermal_state() is media.ThermalState.COOL  # 87 °C, threshold 90
    assert media.too_hot() is False


def test_a_package_over_the_threshold_is_hot(hwmon):
    _chip(hwmon, 3, "coretemp", temp1=("Package id 0", "92000"))

    assert media.thermal_state() is media.ThermalState.HOT
    assert media.too_hot() is True


def test_a_package_label_on_another_chip_is_not_the_cpus_own_sensor(hwmon):
    """The CHIP name is load-bearing, not just the label. A board management controller or a
    wrapper driver can publish a mirrored `Package id 0` of its own, sampled on its own schedule
    and sometimes scaled differently; the guard is specified to read the CPU's own coretemp. The
    mirror here is sorted first and reads cool while the real package is at 95 °C, so a resolver
    that matched on the label alone would let a hot host through."""
    _chip(hwmon, 0, "ipmi_si", temp1=("Package id 0", "30000"))
    _chip(hwmon, 5, "coretemp", temp1=("Package id 0", "95000"))

    assert media.resolve_thermal_sensor() == hwmon / "hwmon5" / "temp1_input"
    assert media.thermal_state() is media.ThermalState.HOT


def test_the_core_sensors_of_the_right_chip_are_not_mistaken_for_the_package(hwmon):
    """coretemp publishes one input per core beside the package one, and `temp1` is not reliably
    the package — so the LABEL decides, not the position within the chip either."""
    _chip(
        hwmon,
        2,
        "coretemp",
        temp1=("Core 0", "99000"),
        temp2=("Core 1", "99000"),
        temp3=("Package id 0", "70000"),
    )

    assert media.resolve_thermal_sensor() == hwmon / "hwmon2" / "temp3_input"
    assert media.thermal_state() is media.ThermalState.COOL  # the package, not the hot core


def test_nothing_found_is_unknown_and_never_cool(hwmon):
    """A host with no coretemp at all — a dev machine, a container without /sys, an AMD box. The
    guard has measured nothing, so it may not answer "cool"; it answers UNKNOWN, and the worker
    keeps draining (too_hot is False) because a queue that stops on every unmeasured host is worse
    than one that runs unprotected on a machine nobody asked to protect."""
    _chip(hwmon, 0, "acpitz", temp1="28000")
    _chip(hwmon, 1, "k10temp", temp1="45000")  # AMD: a real CPU sensor, but it publishes no labels

    assert media.resolve_thermal_sensor() is None
    assert media.read_temp_millideg() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN
    assert media.too_hot() is False


def test_an_absent_hwmon_directory_is_unknown(tmp_path, monkeypatch):
    """Not an empty tree — no tree. A container without /sys mounted reaches this branch."""
    monkeypatch.setattr(media, "HWMON_ROOT", tmp_path / "there-is-no-hwmon-here")
    monkeypatch.setattr(settings, "THERMAL_ZONE", "")

    assert media.resolve_thermal_sensor() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN


def test_the_right_chip_with_no_labels_at_all_is_unknown_not_a_guess(hwmon):
    """coretemp is there but publishes no temp*_label. The guard does NOT fall back to temp1: that
    is the positional guess this change exists to remove, and a wrong input reads as a permanently
    cool machine. UNKNOWN sends the operator to THERMAL_ZONE instead."""
    _chip(hwmon, 0, "coretemp", temp1="88000", temp2="86000")

    assert media.resolve_thermal_sensor() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN


def test_a_labelled_input_whose_reading_file_is_missing_is_unknown(hwmon):
    """The label says Package id 0 but there is no matching _input — a sysfs shape the resolver
    must not return a path for, or every read afterwards fails on a file that was never there."""
    chip = _chip(hwmon, 0, "coretemp")
    (chip / "temp1_label").write_text("Package id 0\n")

    assert media.resolve_thermal_sensor() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN


def test_an_unreadable_sensor_is_unknown_not_cool(hwmon):
    """Resolution succeeded and the READ failed — permissions, a vanished file, a driver returning
    something that is not a number. The distinction matters because this is the state a monitoring
    change could most easily turn into a silent "cool"."""
    _chip(hwmon, 0, "coretemp", temp1=("Package id 0", "not-a-number"))

    assert media.resolve_thermal_sensor() is not None  # control: the path WAS resolved
    assert media.read_temp_millideg() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN
    assert media.too_hot() is False


def test_an_explicit_thermal_zone_wins_over_name_resolution(hwmon, tmp_path, monkeypatch):
    """The override is how a host the resolver cannot name its way to is configured — Saturn's
    k10temp, which publishes no labels. So it must be taken unresolved, even when a perfectly good
    coretemp package sensor is sitting right there saying something else."""
    _chip(hwmon, 5, "coretemp", temp1=("Package id 0", "50000"))
    zone = tmp_path / "k10temp-tctl"
    zone.write_text("95000\n")
    monkeypatch.setattr(settings, "THERMAL_ZONE", str(zone))

    assert media.thermal_sensor() == zone
    assert media.read_temp_millideg() == 95000
    assert media.thermal_state() is media.ThermalState.HOT  # the override's number, not 50 °C


def test_an_explicit_thermal_zone_that_cannot_be_read_is_unknown_not_cool(
    hwmon, tmp_path, monkeypatch
):
    """A misconfigured override — a path that moved, a typo — must not read as a cool host. This
    is the exact failure the guard shipped with: a configured path that silently read nothing."""
    _chip(hwmon, 5, "coretemp", temp1=("Package id 0", "95000"))
    monkeypatch.setattr(settings, "THERMAL_ZONE", str(tmp_path / "no-such-sensor"))

    assert media.read_temp_millideg() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN
    assert media.too_hot() is False  # UNKNOWN does not defer...
    # ...and the resolver was genuinely bypassed: the coretemp package above reads 95 °C, so a
    # guard that had fallen back to name resolution would have said HOT here.


def test_the_threshold_is_the_one_the_owner_named_for_this_host():
    """90 °C on the package, decided 2026-09-18 against measured peaks of 85 °C at one encoder
    thread and 92 °C at eight, critical 100 °C. Pinned because the number that stood here before
    (70) was carried over from a different host and a different sensor, and silently stalls every
    media job on this one."""
    defaults = type(settings).model_fields
    assert defaults["THERMAL_MAX_C"].default == 90
    # and the shipped default resolves by name — a path here would be an explicit override
    assert defaults["THERMAL_ZONE"].default == ""


# --- reads that are not text, and numbers that are not temperatures (fold round) --------------


def test_a_chip_name_that_is_not_utf8_is_skipped_not_raised(hwmon):
    """A chip whose `name` is not valid UTF-8 raises UnicodeDecodeError — a ValueError, not an
    OSError. Unguarded it escapes the resolver, and the resolver's first caller is the worker's
    startup line, before the schema gate: the worker would not start at all. So the chip is skipped
    and the scan goes on, which a second, valid coretemp behind it proves."""
    bad = hwmon / "hwmon0"
    bad.mkdir()
    (bad / "name").write_bytes(b"\xff\xfe not text\n")
    _chip(hwmon, 1, "coretemp", temp1=("Package id 0", "88000"))

    assert media.resolve_thermal_sensor() == hwmon / "hwmon1" / "temp1_input"
    assert media.thermal_state() is media.ThermalState.COOL


def test_a_label_that_is_not_utf8_is_skipped_not_raised(hwmon):
    """Same failure one level down: the chip reads fine and one of its labels does not."""
    chip = _chip(hwmon, 0, "coretemp", temp2=("Package id 0", "95000"))
    (chip / "temp1_label").write_bytes(b"\xff\xfe\n")
    (chip / "temp1_input").write_text("50000\n")

    assert media.resolve_thermal_sensor() == chip / "temp2_input"
    assert media.thermal_state() is media.ThermalState.HOT


def test_an_undecodable_name_with_nothing_else_is_unknown(hwmon):
    """And with no good chip behind it, the answer is UNKNOWN — never an exception, never cool."""
    bad = hwmon / "hwmon0"
    bad.mkdir()
    (bad / "name").write_bytes(b"\xff\xfe\n")

    assert media.resolve_thermal_sensor() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN


@pytest.mark.parametrize("reading", ["0", "-1", "-40000", "999000", "150001"])
def test_a_reading_that_is_not_a_temperature_is_unknown(hwmon, reading):
    """Both ends are failure modes that would otherwise read as a decision. 0 and negatives are
    what a disconnected or misread sensor reports, and they sit below every threshold — "cool"
    about a host nothing measured, which is the exact bug this guard exists to stop. An absurd
    positive is above every threshold for ever, and backfill_location waits in `while too_hot()`,
    so it would never finish."""
    _chip(hwmon, 0, "coretemp", temp1=("Package id 0", reading))

    assert media.read_temp_millideg() is None
    assert media.thermal_state() is media.ThermalState.UNKNOWN
    assert media.too_hot() is False


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        ("1", "COOL"),  # the lowest plausible reading is still a measurement
        ("150000", "HOT"),  # the highest one is still a measurement, and it is hot
        ("90000", "COOL"),  # exactly at the threshold is not above it
        ("90001", "HOT"),
    ],
)
def test_the_edges_of_the_plausible_range_and_of_the_threshold_still_classify(
    hwmon, reading, expected
):
    """The bound must reject nonsense without swallowing the extremes of the real range, and the
    threshold comparison stays strictly-greater-than."""
    _chip(hwmon, 0, "coretemp", temp1=("Package id 0", reading))

    assert media.read_temp_millideg() == int(reading)
    assert media.thermal_state() is getattr(media.ThermalState, expected)
