# `pyt` Design Phase fixture

This fixture validates offline Python dependency caches across the Design Phase
to Evaluation Phase cutover. The Design Phase library depends on cached `PyYAML`
and must not use `more-itertools`.
