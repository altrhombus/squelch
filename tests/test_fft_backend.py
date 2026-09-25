"""pyfftw, when installed, must actually become the scipy.fft backend."""

import subprocess
import sys

import pytest


def test_pyfftw_backend_is_active_after_import():
    pytest.importorskip("pyfftw")
    # Fresh interpreter: the backend is process-global, set at import time.
    code = (
        "import numpy as np, scipy.fft as sf\n"
        "import pyfftw.interfaces.scipy_fft as pf\n"
        "import backend.sdr.fm\n"
        "calls = []\n"
        "orig = pf.__ua_function__\n"
        "pf.__ua_function__ = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]\n"
        "sf.rfft(np.ones(1024))\n"
        "assert calls, 'scipy.fft did not dispatch to pyfftw'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
