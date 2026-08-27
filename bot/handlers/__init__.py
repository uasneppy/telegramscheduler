"""Telegram bot handlers, split into thematic modules.

The former 12k-line handlers.py monolith is partitioned into the submodules
below. Handler functions call each other densely across module boundaries;
instead of maintaining hundreds of cross-imports, this package merges every
module's names into one shared namespace and injects it into each submodule,
so call-time name resolution behaves exactly as it did in the single file.
New code should prefer explicit imports; the injection keeps the split
mechanical and behavior-preserving.
"""

import sys

from . import common
from . import dispatch
from . import recurring
from . import batches
from . import channels
from . import editing
from . import scheduling
from . import uploads
from . import menu
from . import misc

_modules = [common, dispatch, recurring, batches, channels, editing, scheduling, uploads, menu, misc]

# Merge all public (and single-underscore) names into one namespace...
_namespace = {}
for _m in _modules:
    for _k, _v in vars(_m).items():
        if not _k.startswith('__'):
            _namespace[_k] = _v

# ...inject it into every submodule so cross-module calls resolve...
for _m in _modules:
    for _k, _v in _namespace.items():
        if not hasattr(_m, _k):
            setattr(_m, _k, _v)

# ...and re-export everything at package level (from bot.handlers import X)
_pkg = sys.modules[__name__]
for _k, _v in _namespace.items():
    if not hasattr(_pkg, _k):
        setattr(_pkg, _k, _v)
