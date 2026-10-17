"""Local-only, conservative speaker verification; never an authentication mechanism."""
from .core import cosine, decide, validate_profile, windows
__all__ = ['cosine', 'decide', 'validate_profile', 'windows']
