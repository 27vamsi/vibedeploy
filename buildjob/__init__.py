"""Everything that runs inside the isolated build job. Readme.md section 6.

Nothing in here is ever imported by the worker or the control plane: this is the
only place untrusted app code is allowed near (Readme.md section 3 rule 11).
"""
