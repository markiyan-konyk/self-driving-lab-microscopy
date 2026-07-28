"""Vendored, editable instrument driver classes owned by the SCOPIO nodes.

Each file here is a self-contained plain-python driver (ros2_ws/ builds into a
container and must not import from outside itself). dg1022z.py is a copy of the
repo-root DG1022Z.py; TC10LAB.py lives only here -- edit it in place.
A node wraps one of these classes and publishes EVERY public method of it over a
single generic `InstrumentCall` service:

    drivers/dg1022z.py  DG1022Z  <- galvo_node        srv /scopio/awg/call
    drivers/TC10LAB.py  TC10LAB  <- temperature_node  srv /scopio/temperature/call

So the ROS layer never decides which instrument features are "supported" -- the
class does, and each client app picks the subset it needs. Add a method to the
driver and it is callable the same instant. See dispatch.py.
"""
