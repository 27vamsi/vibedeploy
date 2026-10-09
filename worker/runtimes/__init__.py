"""Where a proved app is actually put. One module per target.

`local` is processes on this machine. M8 adds `ecs`, and the pipeline above it
should not be able to tell the difference: both take a checkout and a set of
secrets and give back an endpoint.
"""
