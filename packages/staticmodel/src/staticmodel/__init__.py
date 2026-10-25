"""Static model: which points of a fixed lidar's frames are background (do not move)."""

from .model import StaticFilter, StaticModel, StaticParams, clear_pixels, model_path

__all__ = ["StaticFilter", "StaticModel", "StaticParams", "clear_pixels", "model_path"]
