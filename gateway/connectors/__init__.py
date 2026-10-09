"""Connectors. One per system an agent can reach, all the same shape.

The shape is contract 7.7 and it is frozen. Every connector has to be able to
say what it *would* do before it does it, check that what it did is what it
said, and put it back. A system that cannot do those three things does not get a
connector; it gets a stub and a paragraph explaining why (Readme.md 20.3).
"""
