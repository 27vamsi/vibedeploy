"""The control plane: what the builder talks to. Readme.md sections 22 and 23.

It owns the record of apps, deployments, access models, verification results and
the job queue. It never runs app code and never stores a secret value; the
worker puts credentials in the secret store and only ever writes references
here.
"""
