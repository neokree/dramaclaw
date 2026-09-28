"""Local CE generation credit quote implementation."""

from __future__ import annotations

import logging
import math

from novelvideo.ports.credit_quote import CreditQuote

logger = logging.getLogger(__name__)


async def _image_credits(model: str, params: dict) -> int:
    """Higgsfield's own price (`higgsfield generate cost`); Draw Things is free."""
    from novelvideo.engines.image import is_selection, quote_credits

    selection = model if is_selection(model) else f"higgsfield:{model}"
    try:
        credits = await quote_credits(
            selection,
            image_size=str(params.get("size") or "") or None,
            quality=str(params.get("quality") or "") or None,
        )
    except Exception as exc:  # noqa: BLE001 - an offline engine quotes 0, never 500s
        logger.info("image quote unavailable for %s: %s", selection, exc)
        return 0
    return math.ceil(credits)


class LocalCreditQuote:
    async def generation_credit_quote(
        self,
        *,
        kind: str,
        model: str,
        params: dict,
        quantity: int,
        product_surface: str,
        user_id: str = "",
    ) -> CreditQuote:
        del product_surface, user_id
        unit_cost = 0
        if kind == "image" and model and model != "drawthings":
            unit_cost = await _image_credits(model, params or {})
        total = unit_cost * max(int(quantity or 1), 1)
        return CreditQuote(
            total_cost=total,
            display=str(total),
            unit="call",
            unit_cost=unit_cost,
            quantity=1,
            params={},
        )
