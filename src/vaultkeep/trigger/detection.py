"""Adapt a Trigger instance to shared application process identification."""

from vaultkeep.application import DetectionResult, ProcessProvider, detect_spec
from vaultkeep.trigger.config import InstanceConfig


def detect(instance: InstanceConfig, provider: ProcessProvider) -> DetectionResult:
    """Return the instance observation using the shared detector."""
    return detect_spec(
        app=instance.app,
        data_dir=str(instance.data_dir),
        matcher=instance.matcher,
        provider=provider,
    )
