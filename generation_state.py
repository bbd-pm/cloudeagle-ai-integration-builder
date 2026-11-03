"""State helpers that prevent showing generated code for stale selections."""


def invalidate_stale_generated_code(session_state, current_signature):
    """Remove a deterministic artifact when API identity or checked operations change."""
    if "generated_code" not in session_state:
        return False
    if session_state.get("generated_selection_signature") == current_signature:
        return False
    for key in ("generated_code", "generated_type", "validation", "generated_selection_signature"):
        session_state.pop(key, None)
    return True

