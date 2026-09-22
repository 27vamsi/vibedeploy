"""Delivery. Readme.md section 14: "a sitecustomize.py on PYTHONPATH".

Python imports `sitecustomize` automatically at interpreter startup, so the
app does not have to import anything. `inject_shim.py` (M6) adds a final image
layer holding this directory and the `vibedeploy_shim` package, and puts both
on PYTHONPATH.

This directory holds nothing but this file: putting it on PYTHONPATH must not
shadow anything the app imports.
"""

import vibedeploy_shim

vibedeploy_shim.install()
