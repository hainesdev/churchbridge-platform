import numpy as np

from tests.benchmark.degradations import (
    DegradationSpec,
    apply_degradation,
    sanitize_case_label,
    standard_matrix,
)


def test_clean_degradation_is_passthrough():
    samples = np.linspace(-0.5, 0.5, 3200, dtype=np.float32)
    spec = DegradationSpec("clean", seed=11)

    degraded = apply_degradation(samples, 16000, spec)

    assert np.allclose(degraded, samples)


def test_noise_degradation_is_deterministic_for_same_seed():
    samples = np.ones(4000, dtype=np.float32) * 0.1
    spec = DegradationSpec("noise", noise_type="hvac", snr_db=10.0, seed=3)

    first = apply_degradation(samples, 16000, spec)
    second = apply_degradation(samples, 16000, spec)

    assert np.allclose(first, second)
    assert not np.allclose(first, samples)


def test_echo_degradation_preserves_length_and_changes_waveform():
    samples = np.zeros(5000, dtype=np.float32)
    samples[100] = 1.0
    spec = DegradationSpec("echo", echo_profile="medium", seed=7)

    degraded = apply_degradation(samples, 16000, spec)

    assert len(degraded) == len(samples)
    assert np.count_nonzero(degraded) > 1


def test_standard_matrix_covers_multiple_conditions_and_noise_types():
    suite = standard_matrix(seed=7, preset="standard")

    case_ids = [spec.case_id() for spec in suite]
    noise_types = {spec.noise_type for spec in suite if spec.noise_type}
    conditions = {spec.condition for spec in suite}

    assert "clean" in case_ids[0]
    assert {"clean", "echo", "noise", "echo_noise"} <= conditions
    assert {"hvac", "crowd", "street", "pink"} <= noise_types


def test_sanitize_case_label_includes_source_and_degradation():
    spec = DegradationSpec("echo_noise", echo_profile="medium", noise_type="crowd", snr_db=10.0)

    label = sanitize_case_label("tests/audio/2", spec)

    assert label == "2__echo_noise_medium_crowd_10db"


def test_build_single_spec_discards_irrelevant_defaults_for_clean():
    from tests.benchmark.degradations import build_single_spec

    spec = build_single_spec(
        condition="clean",
        echo_profile="medium",
        noise_type="hvac",
        snr_db=10.0,
        seed=7,
    )

    assert spec.case_id() == "clean"
