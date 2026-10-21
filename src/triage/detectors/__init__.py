"""Detectors. Each consumes one service's time-ordered points and yields per-point signals."""
from .base import Detector
from .cusum import Cusum
from .ewma import EwmaResidual
from .multivariate import LoadDecoupling
from .seasonal import SeasonalRobustZ

DETECTORS: list[type[Detector]] = [SeasonalRobustZ, EwmaResidual, Cusum, LoadDecoupling]

__all__ = ["Detector", "DETECTORS", "SeasonalRobustZ", "EwmaResidual", "Cusum", "LoadDecoupling"]
