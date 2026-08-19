"""Shared clear-block helper for pending UI blocks (permission, plan, question)."""
from __future__ import annotations

from typing import Dict, Optional

from .keys import CMD_REPLACE


def clear_pending_block(
    view,
    block_region_key: str,
    button_prefix: str,
    button_keys: Dict[str, tuple],
    fallback_region_end: Optional[int] = None,
    replacement: str = "",
    extra_region_keys: tuple = (),
) -> Optional[tuple]:
    """Erase a pending UI block from the view.

    Returns the (begin, end) of the cleared region, or None if nothing was cleared.
    Viewless: no-op, returns None.
    """
    if not view:
        return None
    for btn_type in button_keys:
        view.erase_regions("%s%s" % (button_prefix, btn_type))

    cleared = None  # type: Optional[tuple]
    regions = view.get_regions(block_region_key)
    if regions and regions[0].size() > 0:
        r = regions[0]
        cleared = (r.begin(), r.end())
        view.set_read_only(False)
        view.run_command(CMD_REPLACE, {
            "start": r.begin(), "end": r.end(), "text": replacement,
        })
        view.set_read_only(True)
    elif fallback_region_end is not None and view.size() > fallback_region_end:
        cleared = (fallback_region_end, view.size())
        view.set_read_only(False)
        view.run_command(CMD_REPLACE, {
            "start": fallback_region_end, "end": view.size(), "text": replacement,
        })
        view.set_read_only(True)

    view.erase_regions(block_region_key)
    for key in extra_region_keys:
        view.erase_regions(key)
    return cleared
