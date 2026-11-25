"""Transport-neutral bootstrap request dispatch and its daemon-socket wire host.

``dispatcher`` answers the ownerless-phase methods for any transport;
``wire`` serves them over the home's ``daemon.sock``; ``peer`` reads the
operating-system peer credential the wire host checks.  Importing the package
loads nothing: callers import the module they need.
"""
