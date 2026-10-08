"""Deterministic visualization layer: SQL task result -> validated chart specification -> render model.

No LLM is involved. A specification only REFERENCES columns of a recorded task result and never contains values, so a chart cannot show a
number that the SQL result does not contain. Run everything from the delivery_agent folder.
"""
