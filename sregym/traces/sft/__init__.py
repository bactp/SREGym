"""SREGym -> SFT training-data pipeline.

Turns oracle-verified ATIF trajectories (``sregym.traces.store``) into
trajectory-derived SFT examples, per the design confirmed in session
2026-08-15 ("Du lieu fine-tuning cho K8s operational model"): milestone-based
segmentation (content-based detectors, not hard-coded step indices), metadata
kept outside ``messages``, and the runtime tool schema left unchanged.

Modules:

* ``detectors`` -- the ``StepView``/``Milestone``/registry primitives and
  ``assign_milestones()``, generic across every problem.
* ``problems`` -- one module per ``problem_id`` registering its own milestone
  list + detector function. Importing ``sregym.traces.sft.problems`` runs all
  registrations.
* ``cut`` -- turns one oracle-verified trajectory + its milestone assignment
  into the SFT example set (tool_selection, state_interpretation,
  action_selection, verification, negative_invalid_action, full_trajectory).
"""
