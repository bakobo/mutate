"""Mutation-test the modules a repo changed, and file a tick per surviving mutant.

``bakobo-mutate run`` (``mutate``) drives the target repo's own mutmut over the modules its
default branch changed and writes a report; ``bakobo-mutate ticks`` (``ticks``) files a tick
per surviving mutant in such a report. ``shared`` holds what both read.
"""
