"""L5 feature modules. May import sublime. Builds on core/ + ui/ ports."""
from __future__ import annotations


def wire_session(session):
    """Attach goal harness, scheduler, and context manager to a Session."""
    from features.goals.loop import attach_goal_harness
    from features.scheduler import attach_scheduler
    from features.context import ContextManager
    attach_goal_harness(session)
    attach_scheduler(session)
    from features.resume import attach_resume_preview, transcript_cwd
    attach_resume_preview(session)
    # Where a cwd-scoped backend filed this session's transcript (grok).
    session._transcript_cwd = transcript_cwd
    if getattr(session, "context", None) is None:
        session.context = ContextManager(session)
    return session
