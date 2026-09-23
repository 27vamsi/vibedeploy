"""The login gate. Readme.md section 16.

The sidecar is the only thing the outside world can reach. The app it fronts
listens on `127.0.0.1:3000` inside the same network namespace, so there is no
route to the app that does not pass through here.

Its whole job is to turn a browser session into a short-lived signed statement
of who the request is for, and to make sure no one else can make that statement.
"""
