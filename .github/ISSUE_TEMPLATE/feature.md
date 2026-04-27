---
name: Feature request
about: Suggest a new command, flag, or change
labels: enhancement
---

**Use case**

What you're trying to achieve. Concrete scenarios beat abstract wishes —
"I want to know which payouts hit OUT_OF_ENERGY in the last week" is
actionable; "more reporting" is not.

**Proposed shape**

If you have a CLI design in mind, sketch the invocation:

    skr-crypto <verb> [options] [args]

**Why not an existing command**

Is this missing in ``skr-crypto help``? Could it be a new flag on an
existing command instead of a new top-level verb?

**Money safety**

Reminder: this CLI is intentionally read-only on the money path. Any
proposal that would let the CLI move funds will be redirected to the
HTTP API instead.
