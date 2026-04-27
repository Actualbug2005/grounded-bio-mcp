"""numpy random-state deserialisation-compat shim for CRISPOR's Azimuth-2.0 model.

Drop into a CRISPOR venv's ``site-packages/sitecustomize.py`` so it
auto-loads on every Python invocation from that venv.

CRISPOR ships a pre-trained Azimuth GradientBoostingRegressor saved
with numpy <1.21, where ``numpy.random._pickle.__randomstate_ctor``
took two positional args ``(name, format)``. numpy 1.21+ simplified
the signature to ``(name=None)``. Loading the bundled model under
newer numpy raises::

    TypeError: __randomstate_ctor() takes from 0 to 1 positional
    arguments but 2 were given

This shim restores the permissive signature on import. The discarded
``format`` arg is informational metadata about the random-state
representation; the random state itself is bookkeeping on the
regressor and is not used at predict-time, so dropping the second
arg is safe.

See ``docs/crispor_install.md`` for the full install procedure this
shim accompanies. Captured during Session 8b deployment debugging
as v3.0.1 spec errata #11.
"""

try:
    from numpy.random import _pickle as _np_pickle

    _orig_ctor = _np_pickle.__randomstate_ctor

    def _compat_ctor(*args, **kwargs):
        # Old serialised models call __randomstate_ctor(name, format).
        # New numpy expects __randomstate_ctor(name=None).
        # Discard the second positional arg if present.
        if args:
            return _orig_ctor(args[0])
        return _orig_ctor(**kwargs)

    _np_pickle.__randomstate_ctor = _compat_ctor
except (ImportError, AttributeError):
    # Either numpy isn't installed (the file ended up in a non-CRISPOR
    # venv by accident), or the internal API has moved again — fail
    # silent rather than break the import path of every Python program
    # that runs from this venv.
    pass
